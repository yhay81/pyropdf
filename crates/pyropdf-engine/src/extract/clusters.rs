//! Bounded `O(n log n)` spatial clustering of painted vector paths in display
//! space (DESIGN.md sections 1, 6, and 10).
//!
//! Each painted path's geometry box is widened by half its stroke width (keeping
//! stroked hairlines of positive length and dropping zero-area fills and
//! non-finite geometry). When configured and the page's display size is known,
//! axis-aligned background rectangles covering at least 95% of both the visible
//! page width and height are excluded before clustering.
//!
//! Connected components within `(x_tolerance, y_tolerance)` are computed via an
//! `O(n log n)` x-sweep over a coordinate-compressed Y segment tree with lazy
//! subtree flushing and a disjoint-set union forest. Because a cluster's union
//! box can bridge paths or other clusters that none of its individual paths
//! reached, the same `O(k log k)` sweep repeats on cluster boxes until no more
//! merges occur, charging every working buffer to `LimitCode::MemorySize` and
//! each sweep pass to `LimitCode::InterpretationFuel` (F6).

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};

use super::Error;
use super::model::{BBox, PathTable};

/// Fraction of visible page width and height above which an axis-aligned
/// rectangle is treated as a full-page background.
const BACKGROUND_COVERAGE: f64 = 0.95;

/// Maximum recursion depth of the power-of-two Y segment tree (enough for up
/// to `2^31` unique compressed Y coordinates, far above `u32` path counts).
const MAX_TREE_DEPTH: u8 = 32;

/// How often the sweep loop polls the budget for deadline or cancellation.
const POLL_MASK: usize = 255;

/// Options controlling spatial clustering of painted vector paths.
#[derive(Clone, Copy, Debug)]
pub struct ClusterOptions {
    /// Maximum horizontal gap in display-space points between merged boxes.
    pub x_tolerance: f64,
    /// Maximum vertical gap in display-space points between merged boxes.
    pub y_tolerance: f64,
    /// Exclude full-page axis-aligned background rectangles when the page's
    /// display size is known.
    pub exclude_background: bool,
    /// Refuse with `ContentElementCount` if more than this many clusters result.
    pub max_clusters: Option<usize>,
}

/// One clustered region: its display-space bounding box and the ascending
/// zero-based indices of its member paths.
pub type PathClusterTuple = (BBox, Vec<u32>);

/// One path's geometry summary for callers outside [`PathTable`].
#[derive(Clone, Copy, Debug)]
pub struct ClusterInput {
    pub bbox: BBox,
    pub has_fill: bool,
    pub has_stroke: bool,
    pub stroke_width: f64,
    pub is_rect: bool,
}

/// Clusters rows `start..stop` of `table` in display space under `budget`.
///
/// Returned path indices are zero-based relative to `start`, so sliced `Paths`
/// views address their own rows directly.
pub fn cluster_path_table(
    table: &PathTable,
    start: usize,
    stop: usize,
    options: ClusterOptions,
    budget: &Budget,
) -> Result<Vec<PathClusterTuple>, Error> {
    validate_options(&options)?;
    let count = stop.saturating_sub(start);
    let page_size = (table.page_size.0.is_finite()
        && table.page_size.1.is_finite()
        && table.page_size.0 > 0.0
        && table.page_size.1 > 0.0)
        .then_some(table.page_size);

    let mut boxes = BudgetVec::new(budget);
    let mut members = BudgetVec::new(budget);
    for (offset, row) in (start..stop).enumerate() {
        if (offset & POLL_MASK) == 0 {
            budget.poll()?;
        }
        let bbox = (table.x0[row], table.y0[row], table.x1[row], table.y1[row]);
        let has_fill = table.fill.present[row] != 0;
        let has_stroke = table.stroke.present[row] != 0;
        let stroke_width = if has_stroke {
            table.stroke_width[row]
        } else {
            0.0
        };
        let Some(painted) = painted_box(bbox, has_fill, has_stroke, stroke_width) else {
            continue;
        };
        if options.exclude_background
            && is_full_page_background(painted, page_size, || {
                row_is_axis_aligned_rect(table, row, has_fill)
            })
        {
            continue;
        }
        let index = u32::try_from(offset).map_err(|_| {
            Error::Allocation(format!(
                "path index {offset} of {count} exceeds u32 cluster index range"
            ))
        })?;
        boxes.try_push(painted)?;
        members.try_push(index)?;
    }

    cluster_prepared(boxes, members, options, budget)
}

/// Clusters externally supplied path inputs (for example a constructed or
/// unpickled `Content` without a backing [`PathTable`]).
pub fn cluster_path_inputs(
    inputs: &[ClusterInput],
    page_size: Option<(f64, f64)>,
    options: ClusterOptions,
    budget: &Budget,
) -> Result<Vec<PathClusterTuple>, Error> {
    validate_options(&options)?;
    let page_size =
        page_size.filter(|&(w, h)| w.is_finite() && h.is_finite() && w > 0.0 && h > 0.0);

    let mut boxes = BudgetVec::new(budget);
    let mut members = BudgetVec::new(budget);
    for (offset, input) in inputs.iter().enumerate() {
        if (offset & POLL_MASK) == 0 {
            budget.poll()?;
        }
        let Some(painted) = painted_box(
            input.bbox,
            input.has_fill,
            input.has_stroke,
            input.stroke_width,
        ) else {
            continue;
        };
        if options.exclude_background
            && is_full_page_background(painted, page_size, || input.is_rect)
        {
            continue;
        }
        let index = u32::try_from(offset).map_err(|_| {
            Error::Allocation(format!(
                "path index {offset} exceeds u32 cluster index range"
            ))
        })?;
        boxes.try_push(painted)?;
        members.try_push(index)?;
    }

    cluster_prepared(boxes, members, options, budget)
}

fn validate_options(options: &ClusterOptions) -> Result<(), Error> {
    if !options.x_tolerance.is_finite() || options.x_tolerance < 0.0 {
        return Err(Error::Allocation(format!(
            "x_tolerance must be finite and non-negative: {}",
            options.x_tolerance
        )));
    }
    if !options.y_tolerance.is_finite() || options.y_tolerance < 0.0 {
        return Err(Error::Allocation(format!(
            "y_tolerance must be finite and non-negative: {}",
            options.y_tolerance
        )));
    }
    Ok(())
}

/// Returns the painted bounding box of one path, widened by half its stroke
/// width, or `None` when the path paints no visible geometry.
fn painted_box(bbox: BBox, has_fill: bool, has_stroke: bool, stroke_width: f64) -> Option<BBox> {
    if !has_fill && !has_stroke {
        return None;
    }
    let (mut x0, mut y0, mut x1, mut y1) = bbox;
    if !(x0.is_finite() && y0.is_finite() && x1.is_finite() && y1.is_finite()) {
        return None;
    }
    if x1 < x0 || y1 < y0 {
        return None;
    }
    let mut hairline = false;
    if has_stroke {
        if !stroke_width.is_finite() || stroke_width < 0.0 {
            return None;
        }
        if stroke_width > 0.0 {
            let half = stroke_width * 0.5;
            x0 -= half;
            y0 -= half;
            x1 += half;
            y1 += half;
            if !(x0.is_finite() && y0.is_finite() && x1.is_finite() && y1.is_finite()) {
                return None;
            }
        } else {
            hairline = true;
        }
    }
    if (x1 > x0 && y1 > y0) || (hairline && (x1 > x0 || y1 > y0)) {
        Some((x0, y0, x1, y1))
    } else {
        None
    }
}

/// Returns whether a painted box covers at least `BACKGROUND_COVERAGE` of the
/// visible page in both axes and `is_rect()` holds.
fn is_full_page_background(
    painted: BBox,
    page_size: Option<(f64, f64)>,
    is_rect: impl FnOnce() -> bool,
) -> bool {
    let Some((pw, ph)) = page_size else {
        return false;
    };
    let cov_w = (painted.2.min(pw) - painted.0.max(0.0)).max(0.0);
    let cov_h = (painted.3.min(ph) - painted.1.max(0.0)).max(0.0);
    if cov_w < BACKGROUND_COVERAGE * pw || cov_h < BACKGROUND_COVERAGE * ph {
        return false;
    }
    is_rect()
}

/// Perimeter-side bit flags for [`row_is_axis_aligned_rect`].
const EDGE_TOP: u8 = 1 << 0;
const EDGE_BOTTOM: u8 = 1 << 1;
const EDGE_LEFT: u8 = 1 << 2;
const EDGE_RIGHT: u8 = 1 << 3;
const ALL_RECT_EDGES: u8 = EDGE_TOP | EDGE_BOTTOM | EDGE_LEFT | EDGE_RIGHT;

/// Returns whether `row` of `table` draws all four axis-aligned perimeter
/// sides of its geometry bounding box with straight segments and corner
/// vertices only.
///
/// Per ISO 32000-2 §8.5.3.3, fill operators implicitly close open subpaths
/// whereas stroke-only operators do not, so implicit closure from the end of a
/// subpath back to its `m` start is counted only when `has_fill` is `true`.
/// This prevents multi-subpath parallel rules (such as a header + footer line)
/// or open 3-sided stroked brackets from being misclassified as full-page
/// background rectangles.
fn row_is_axis_aligned_rect(table: &PathTable, row: usize, has_fill: bool) -> bool {
    let (bx0, by0, bx1, by1) = (table.x0[row], table.y0[row], table.x1[row], table.y1[row]);
    if !(bx1 > bx0 && by1 > by0) {
        return false;
    }
    let eps = 1e-3_f64.max(1e-6 * (bx1 - bx0).max(by1 - by0));
    let is_corner = |x: f64, y: f64| -> bool {
        let on_x = (x - bx0).abs() <= eps || (x - bx1).abs() <= eps;
        let on_y = (y - by0).abs() <= eps || (y - by1).abs() <= eps;
        on_x && on_y
    };
    let record_edge = |cx: f64, cy: f64, tx: f64, ty: f64, edges: &mut u8| -> bool {
        let dx = (tx - cx).abs();
        let dy = (ty - cy).abs();
        if dx <= eps && dy <= eps {
            return true;
        }
        if dx > eps && dy > eps {
            return false;
        }
        if dy <= eps {
            if (ty - by0).abs() <= eps {
                *edges |= EDGE_TOP;
                true
            } else if (ty - by1).abs() <= eps {
                *edges |= EDGE_BOTTOM;
                true
            } else {
                false
            }
        } else if (tx - bx0).abs() <= eps {
            *edges |= EDGE_LEFT;
            true
        } else if (tx - bx1).abs() <= eps {
            *edges |= EDGE_RIGHT;
            true
        } else {
            false
        }
    };

    let mut edges = 0u8;
    let mut subpath_start: Option<(f64, f64)> = None;
    let mut current: Option<(f64, f64)> = None;
    for seg in table.segments_of(row) {
        let verb = table.verb[seg];
        let tx = table.to_x[seg];
        let ty = table.to_y[seg];
        match verb {
            b'm' => {
                if has_fill
                    && let (Some((sx, sy)), Some((cx, cy))) = (subpath_start, current)
                    && !record_edge(cx, cy, sx, sy, &mut edges)
                {
                    return false;
                }
                if !is_corner(tx, ty) {
                    return false;
                }
                subpath_start = Some((tx, ty));
                current = Some((tx, ty));
            }
            b'l' | b'h' => {
                if !is_corner(tx, ty) {
                    return false;
                }
                let Some((cx, cy)) = current else {
                    return false;
                };
                if !record_edge(cx, cy, tx, ty, &mut edges) {
                    return false;
                }
                current = Some((tx, ty));
            }
            _ => return false,
        }
    }
    if has_fill
        && let (Some((sx, sy)), Some((cx, cy))) = (subpath_start, current)
        && !record_edge(cx, cy, sx, sy, &mut edges)
    {
        return false;
    }
    edges == ALL_RECT_EDGES
}

/// Normalizes `-0.0` to `+0.0` so coordinate compression treats both identically.
#[inline]
fn norm_zero(v: f64) -> f64 {
    if v == 0.0 { 0.0 } else { v }
}

/// Returns the union of two boxes.
#[inline]
fn union_bbox(a: BBox, b: BBox) -> BBox {
    (a.0.min(b.0), a.1.min(b.1), a.2.max(b.2), a.3.max(b.3))
}

/// Runs iterative `O(n log n)` clustering over prepared boxes and original path
/// indices until cluster bounding boxes reach a fixed point.
fn cluster_prepared(
    boxes: BudgetVec<'_, BBox>,
    members: BudgetVec<'_, u32>,
    options: ClusterOptions,
    budget: &Budget,
) -> Result<Vec<PathClusterTuple>, Error> {
    let n = boxes.len();
    if n == 0 {
        return Ok(Vec::new());
    }

    // Pass 1: connected components of individual painted path boxes.
    let mut dsu = sweep_union(&boxes, options.x_tolerance, options.y_tolerance, budget)?;

    let (mut cluster_boxes, mut box_cluster) = collapse_initial(&boxes, &mut dsu, budget)?;
    drop(dsu);
    drop(boxes);

    // Passes 2+: merge clusters whose union bounding boxes now reach each other
    // until convergence. Remap through a compact `O(k0)` `origin_map` allocated
    // lazily on the first merge, applying it to `box_cluster` once at the end.
    let mut origin_map: Option<BudgetVec<'_, u32>> = None;
    while cluster_boxes.len() > 1 {
        let prev_count = cluster_boxes.len();
        let mut c_dsu = sweep_union(
            &cluster_boxes,
            options.x_tolerance,
            options.y_tolerance,
            budget,
        )?;
        let (next_boxes, remap) = collapse_clusters(&cluster_boxes, &mut c_dsu, budget)?;
        drop(c_dsu);
        if next_boxes.len() == prev_count {
            break;
        }
        if let Some(map) = origin_map.as_mut() {
            budget.charge(LimitCode::InterpretationFuel, map.len() as u64)?;
            for (step, slot) in map.as_mut_slice().iter_mut().enumerate() {
                if (step & POLL_MASK) == 0 {
                    budget.poll()?;
                }
                *slot = remap[*slot as usize];
            }
        } else {
            origin_map = Some(remap);
        }
        cluster_boxes = next_boxes;
    }

    if let Some(map) = origin_map {
        budget.charge(LimitCode::InterpretationFuel, box_cluster.len() as u64)?;
        for (step, cluster_id) in box_cluster.as_mut_slice().iter_mut().enumerate() {
            if (step & POLL_MASK) == 0 {
                budget.poll()?;
            }
            *cluster_id = map[*cluster_id as usize];
        }
    }

    let cluster_count = cluster_boxes.len();
    if let Some(limit) = options.max_clusters
        && cluster_count > limit
    {
        return Err(Error::Limit(LimitExceeded::over(
            LimitCode::ContentElementCount,
            limit as u64,
            cluster_count as u64,
        )));
    }

    // Count members per cluster to size every index vector fallibly against budget.
    let mut counts = BudgetVec::new(budget);
    counts.try_reserve_exact(cluster_count)?;
    for _ in 0..cluster_count {
        counts.try_push(0usize)?;
    }
    for &cluster_id in box_cluster.as_slice() {
        counts[cluster_id as usize] += 1;
    }

    let mut groups: BudgetVec<'_, BudgetVec<'_, u32>> = BudgetVec::new(budget);
    groups.try_reserve_exact(cluster_count)?;
    for &count in counts.as_slice() {
        let mut group = BudgetVec::new(budget);
        group.try_reserve_exact(count)?;
        groups.try_push(group)?;
    }
    drop(counts);

    for (pos, &member) in members.as_slice().iter().enumerate() {
        if (pos & POLL_MASK) == 0 {
            budget.poll()?;
        }
        let cluster_id = box_cluster[pos] as usize;
        groups[cluster_id].try_push(member)?;
    }
    drop(box_cluster);
    drop(members);

    let mut result = BudgetVec::new(budget);
    result.try_reserve_exact(cluster_count)?;
    for (bbox, group) in cluster_boxes.into_vec().into_iter().zip(groups.into_vec()) {
        result.try_push((bbox, group.into_vec()))?;
    }

    result.sort_by(|a, b| {
        a.0.1
            .total_cmp(&b.0.1)
            .then_with(|| a.0.0.total_cmp(&b.0.0))
            .then_with(|| a.0.3.total_cmp(&b.0.3))
            .then_with(|| a.0.2.total_cmp(&b.0.2))
            .then_with(|| a.1.first().cmp(&b.1.first()))
    });

    Ok(result.into_vec())
}

/// Collapses `n` boxes grouped by `dsu` into compact cluster bounding boxes and
/// a `0..n -> 0..k` cluster assignment vector.
fn collapse_initial<'b>(
    boxes: &[BBox],
    dsu: &mut Dsu<'b>,
    budget: &'b Budget,
) -> Result<(BudgetVec<'b, BBox>, BudgetVec<'b, u32>), Error> {
    let n = boxes.len();
    let mut root_to_cluster = BudgetVec::new(budget);
    root_to_cluster.try_reserve_exact(n)?;
    for _ in 0..n {
        root_to_cluster.try_push(u32::MAX)?;
    }

    let mut cluster_boxes = BudgetVec::new(budget);
    let mut box_cluster = BudgetVec::new(budget);
    box_cluster.try_reserve_exact(n)?;

    for (i, &bbox) in boxes.iter().enumerate() {
        if (i & POLL_MASK) == 0 {
            budget.poll()?;
        }
        let root = dsu.find(i as u32) as usize;
        let cluster_id = root_to_cluster[root];
        if cluster_id == u32::MAX {
            let new_id = cluster_boxes.len() as u32;
            root_to_cluster[root] = new_id;
            cluster_boxes.try_push(bbox)?;
            box_cluster.try_push(new_id)?;
        } else {
            let slot = &mut cluster_boxes[cluster_id as usize];
            *slot = union_bbox(*slot, bbox);
            box_cluster.try_push(cluster_id)?;
        }
    }
    Ok((cluster_boxes, box_cluster))
}

/// Collapses `k` existing cluster boxes grouped by `dsu` into `k_next` merged
/// cluster boxes and a `0..k -> 0..k_next` remap table.
fn collapse_clusters<'b>(
    cluster_boxes: &[BBox],
    dsu: &mut Dsu<'b>,
    budget: &'b Budget,
) -> Result<(BudgetVec<'b, BBox>, BudgetVec<'b, u32>), Error> {
    collapse_initial(cluster_boxes, dsu, budget)
}

/// Disjoint-set union forest with iterative path halving and union by rank.
struct Dsu<'b> {
    parent: BudgetVec<'b, u32>,
    rank: BudgetVec<'b, u8>,
}

impl<'b> Dsu<'b> {
    fn new(len: usize, budget: &'b Budget) -> Result<Self, Error> {
        let mut parent = BudgetVec::new(budget);
        parent.try_reserve_exact(len)?;
        for i in 0..len {
            parent.try_push(i as u32)?;
        }
        let mut rank = BudgetVec::new(budget);
        rank.try_reserve_exact(len)?;
        for _ in 0..len {
            rank.try_push(0)?;
        }
        Ok(Self { parent, rank })
    }

    fn find(&mut self, mut x: u32) -> u32 {
        let mut root = x;
        while self.parent[root as usize] != root {
            root = self.parent[root as usize];
        }
        while self.parent[x as usize] != root {
            let next = self.parent[x as usize];
            self.parent[x as usize] = root;
            x = next;
        }
        root
    }

    fn union(&mut self, a: u32, b: u32) {
        let ra = self.find(a);
        let rb = self.find(b);
        if ra == rb {
            return;
        }
        let (ra_i, rb_i) = (ra as usize, rb as usize);
        if self.rank[ra_i] < self.rank[rb_i] {
            self.parent[ra_i] = rb;
        } else if self.rank[ra_i] > self.rank[rb_i] {
            self.parent[rb_i] = ra;
        } else {
            self.parent[rb_i] = ra;
            self.rank[ra_i] = self.rank[ra_i].saturating_add(1);
        }
    }
}

/// One node of the coordinate-compressed Y segment tree (32 bytes, sentinel
/// `f64::NEG_INFINITY` for absent entries).
#[derive(Clone, Copy)]
struct Node {
    /// Largest `x1` among boxes that completely covered this node.
    cover_x1: f64,
    /// Largest `x1` among active boxes already merged from proper descendants.
    sub_x1: f64,
    /// Upper bound on `x1` of any active data in this node's subtree that has
    /// not yet been merged into its parent; `f64::NEG_INFINITY` when clean.
    dirty_x1: f64,
    /// Box id owning `cover_x1`.
    cover_id: u32,
    /// Box id owning `sub_x1`.
    sub_id: u32,
}

impl Node {
    const EMPTY: Self = Self {
        cover_x1: f64::NEG_INFINITY,
        sub_x1: f64::NEG_INFINITY,
        dirty_x1: f64::NEG_INFINITY,
        cover_id: 0,
        sub_id: 0,
    };
}

struct SegTree<'b> {
    nodes: BudgetVec<'b, Node>,
    leaf_span: usize,
}

impl<'b> SegTree<'b> {
    fn new(unique_ys: usize, budget: &'b Budget) -> Result<Self, Error> {
        let leaf_span = unique_ys
            .max(1)
            .checked_next_power_of_two()
            .ok_or_else(|| Error::Limit(LimitExceeded::new(LimitCode::MemorySize)))?;
        let total_nodes = leaf_span
            .checked_mul(2)
            .ok_or_else(|| Error::Limit(LimitExceeded::new(LimitCode::MemorySize)))?;
        let mut nodes = BudgetVec::new(budget);
        nodes.try_reserve_exact(total_nodes)?;
        for _ in 0..total_nodes {
            nodes.try_push(Node::EMPTY)?;
        }
        Ok(Self { nodes, leaf_span })
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "segment tree recursion parameters avoid heap allocation"
    )]
    fn insert(
        &mut self,
        u: usize,
        l: usize,
        r: usize,
        ql: usize,
        qr: usize,
        box_id: u32,
        reach: f64,
        x1: f64,
        dsu: &mut Dsu<'_>,
        depth: u8,
    ) {
        if depth > MAX_TREE_DEPTH {
            return;
        }
        // Any active box covering `u` intersects `[ql, qr]` because `[l, r]`
        // meets `[ql, qr]`.
        let cover_x1 = self.nodes[u].cover_x1;
        if cover_x1 >= reach {
            dsu.union(box_id, self.nodes[u].cover_id);
        }

        if ql <= l && r <= qr {
            // Canonical node: union with already-merged descendants and flush
            // any unmerged dirty descendants.
            if self.nodes[u].sub_x1 >= reach {
                dsu.union(box_id, self.nodes[u].sub_id);
            }
            if l < r {
                let mid = l + (r - l) / 2;
                let left = u * 2;
                let right = left + 1;
                if self.nodes[left].dirty_x1 >= reach {
                    let best = self.flush(left, l, mid, box_id, reach, dsu, depth + 1);
                    if best.0 > self.nodes[u].sub_x1 {
                        self.nodes[u].sub_x1 = best.0;
                        self.nodes[u].sub_id = best.1;
                    }
                } else {
                    self.nodes[left].dirty_x1 = f64::NEG_INFINITY;
                }
                if self.nodes[right].dirty_x1 >= reach {
                    let best = self.flush(right, mid + 1, r, box_id, reach, dsu, depth + 1);
                    if best.0 > self.nodes[u].sub_x1 {
                        self.nodes[u].sub_x1 = best.0;
                        self.nodes[u].sub_id = best.1;
                    }
                } else {
                    self.nodes[right].dirty_x1 = f64::NEG_INFINITY;
                }
            }
            if x1 > self.nodes[u].cover_x1 {
                self.nodes[u].cover_x1 = x1;
                self.nodes[u].cover_id = box_id;
            }
            self.nodes[u].dirty_x1 = self.nodes[u].dirty_x1.max(x1).max(self.nodes[u].sub_x1);
            return;
        }

        let mid = l + (r - l) / 2;
        let left = u * 2;
        let right = left + 1;
        if ql <= mid {
            self.insert(left, l, mid, ql, qr, box_id, reach, x1, dsu, depth + 1);
        }
        if qr > mid {
            self.insert(right, mid + 1, r, ql, qr, box_id, reach, x1, dsu, depth + 1);
        }
        self.nodes[u].dirty_x1 = self.nodes[u]
            .dirty_x1
            .max(self.nodes[left].dirty_x1)
            .max(self.nodes[right].dirty_x1);
    }

    /// Flushes all unmerged active data in `u`'s subtree, unioning it with
    /// `target_id` and marking `u` clean for its parent. Each call consumes one
    /// dirty node created by an earlier `insert`, bounding total work to
    /// `O(n log n)`.
    #[expect(
        clippy::too_many_arguments,
        reason = "segment tree recursion parameters avoid heap allocation"
    )]
    fn flush(
        &mut self,
        u: usize,
        l: usize,
        r: usize,
        target_id: u32,
        reach: f64,
        dsu: &mut Dsu<'_>,
        depth: u8,
    ) -> (f64, u32) {
        if depth > MAX_TREE_DEPTH {
            return (f64::NEG_INFINITY, 0);
        }
        let mut best = (f64::NEG_INFINITY, 0u32);
        let cover_x1 = self.nodes[u].cover_x1;
        if cover_x1 >= reach {
            let cover_id = self.nodes[u].cover_id;
            dsu.union(target_id, cover_id);
            if cover_x1 > best.0 {
                best = (cover_x1, cover_id);
            }
        }
        let sub_x1 = self.nodes[u].sub_x1;
        if sub_x1 >= reach {
            let sub_id = self.nodes[u].sub_id;
            dsu.union(target_id, sub_id);
            if sub_x1 > best.0 {
                best = (sub_x1, sub_id);
            }
        }
        if l < r {
            let mid = l + (r - l) / 2;
            let left = u * 2;
            let right = left + 1;
            if self.nodes[left].dirty_x1 >= reach {
                let cbest = self.flush(left, l, mid, target_id, reach, dsu, depth + 1);
                if cbest.0 > self.nodes[u].sub_x1 {
                    self.nodes[u].sub_x1 = cbest.0;
                    self.nodes[u].sub_id = cbest.1;
                }
                if cbest.0 > best.0 {
                    best = cbest;
                }
            } else {
                self.nodes[left].dirty_x1 = f64::NEG_INFINITY;
            }
            if self.nodes[right].dirty_x1 >= reach {
                let cbest = self.flush(right, mid + 1, r, target_id, reach, dsu, depth + 1);
                if cbest.0 > self.nodes[u].sub_x1 {
                    self.nodes[u].sub_x1 = cbest.0;
                    self.nodes[u].sub_id = cbest.1;
                }
                if cbest.0 > best.0 {
                    best = cbest;
                }
            } else {
                self.nodes[right].dirty_x1 = f64::NEG_INFINITY;
            }
        }
        self.nodes[u].dirty_x1 = f64::NEG_INFINITY;
        best
    }
}

/// Performs one `O(n log n)` sweep pass, returning the connected components of
/// `boxes` under `(x_tolerance, y_tolerance)`.
fn sweep_union<'b>(
    boxes: &[BBox],
    x_tolerance: f64,
    y_tolerance: f64,
    budget: &'b Budget,
) -> Result<Dsu<'b>, Error> {
    let n = boxes.len();
    budget.charge(LimitCode::InterpretationFuel, n as u64)?;
    let mut dsu = Dsu::new(n, budget)?;
    if n <= 1 {
        return Ok(dsu);
    }

    // Coordinate-compress all Y intervals `[y0, y1 + y_tolerance]`.
    let mut ys = BudgetVec::new(budget);
    ys.try_reserve_exact(
        n.checked_mul(2)
            .ok_or_else(|| Error::Limit(LimitExceeded::new(LimitCode::MemorySize)))?,
    )?;
    for &(_, y0, _, y1) in boxes {
        ys.try_push(norm_zero(y0))?;
        ys.try_push(norm_zero((y1 + y_tolerance).min(f64::MAX)))?;
    }
    ys.sort_by(f64::total_cmp);
    // Deduplicate in place without allocating.
    let mut unique = 0usize;
    for i in 0..ys.len() {
        if unique == 0 || ys[i].to_bits() != ys[unique - 1].to_bits() {
            ys[unique] = ys[i];
            unique += 1;
        }
    }
    ys.truncate(unique);

    // Sort boxes by x0 ascending with deterministic tie-breakers.
    let mut order = BudgetVec::new(budget);
    order.try_reserve_exact(n)?;
    for i in 0..n {
        order.try_push(i as u32)?;
    }
    order.sort_by(|&a, &b| {
        let ba = boxes[a as usize];
        let bb = boxes[b as usize];
        ba.0.total_cmp(&bb.0)
            .then_with(|| ba.2.total_cmp(&bb.2))
            .then_with(|| ba.1.total_cmp(&bb.1))
            .then_with(|| ba.3.total_cmp(&bb.3))
            .then_with(|| a.cmp(&b))
    });

    let mut tree = SegTree::new(unique, budget)?;
    let root_r = tree.leaf_span - 1;
    for (step, &id) in order.as_slice().iter().enumerate() {
        if (step & POLL_MASK) == 0 {
            budget.poll()?;
        }
        let (x0, y0, x1, y1) = boxes[id as usize];
        let y_lo = norm_zero(y0);
        let y_hi = norm_zero((y1 + y_tolerance).min(f64::MAX));
        let Ok(ly) = ys.binary_search_by(|v| v.total_cmp(&y_lo)) else {
            continue;
        };
        let Ok(ry) = ys.binary_search_by(|v| v.total_cmp(&y_hi)) else {
            continue;
        };
        // Clamp above `f64::NEG_INFINITY` so extreme finite tolerances never
        // collide with `Node::EMPTY`'s sentinel value.
        let reach = (x0 - x_tolerance).max(-f64::MAX);
        tree.insert(1, 0, root_r, ly, ry, id, reach, x1, &mut dsu, 0);
    }

    Ok(dsu)
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyropdf_store::budget::{CancellationToken, DocumentLimits};

    /// Exact `O(n^2)` reference oracle for clustering boxes with transitive
    /// union-box merging.
    fn brute_force_clusters(boxes: &[BBox], x_tol: f64, y_tol: f64) -> Vec<PathClusterTuple> {
        let mut clusters: Vec<(BBox, Vec<u32>)> = boxes
            .iter()
            .enumerate()
            .map(|(i, &b)| (b, vec![i as u32]))
            .collect();
        loop {
            let mut merged = false;
            'outer: for i in 0..clusters.len() {
                for j in (i + 1)..clusters.len() {
                    let a = clusters[i].0;
                    let b = clusters[j].0;
                    if a.0 - x_tol <= b.2
                        && b.0 - x_tol <= a.2
                        && a.1 - y_tol <= b.3
                        && b.1 - y_tol <= a.3
                    {
                        let other = clusters.remove(j);
                        clusters[i].0 = union_bbox(clusters[i].0, other.0);
                        clusters[i].1.extend(other.1);
                        clusters[i].1.sort_unstable();
                        merged = true;
                        break 'outer;
                    }
                }
            }
            if !merged {
                break;
            }
        }
        clusters.sort_by(|a, b| {
            a.0.1
                .total_cmp(&b.0.1)
                .then_with(|| a.0.0.total_cmp(&b.0.0))
                .then_with(|| a.0.3.total_cmp(&b.0.3))
                .then_with(|| a.0.2.total_cmp(&b.0.2))
                .then_with(|| a.1.first().cmp(&b.1.first()))
        });
        clusters
    }

    fn run_inputs(
        boxes: &[BBox],
        x_tol: f64,
        y_tol: f64,
        budget: &Budget,
    ) -> Result<Vec<PathClusterTuple>, Error> {
        let inputs: Vec<ClusterInput> = boxes
            .iter()
            .map(|&bbox| ClusterInput {
                bbox,
                has_fill: true,
                has_stroke: true,
                stroke_width: 0.0,
                is_rect: true,
            })
            .collect();
        cluster_path_inputs(
            &inputs,
            None,
            ClusterOptions {
                x_tolerance: x_tol,
                y_tolerance: y_tol,
                exclude_background: false,
                max_clusters: None,
            },
            budget,
        )
    }

    struct Lcg(u64);
    impl Lcg {
        fn next_u64(&mut self) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            self.0
        }
        fn next_f64(&mut self, lo: f64, hi: f64) -> f64 {
            let unit = ((self.next_u64() >> 11) as f64) * (1.0 / ((1u64 << 53) as f64));
            lo + unit * (hi - lo)
        }
    }

    #[test]
    fn sweep_matches_brute_force_oracle_across_random_and_adversarial_sets() {
        let budget = Budget::default();
        for seed in 1u64..=40 {
            let mut rng = Lcg(seed * 9973);
            let n = 20 + (rng.next_u64() as usize % 80);
            let mut boxes = Vec::with_capacity(n);
            for _ in 0..n {
                let x0 = (rng.next_f64(-50.0, 200.0) * 2.0).round() / 2.0;
                let y0 = (rng.next_f64(-50.0, 200.0) * 2.0).round() / 2.0;
                let w = match rng.next_u64() % 8 {
                    0 => 0.0, // vertical hairline
                    _ => (rng.next_f64(0.5, 25.0) * 2.0).round() / 2.0,
                };
                let h = if w == 0.0 {
                    (rng.next_f64(1.0, 30.0) * 2.0).round() / 2.0
                } else {
                    match rng.next_u64() % 8 {
                        0 => 0.0, // horizontal hairline
                        _ => (rng.next_f64(0.5, 25.0) * 2.0).round() / 2.0,
                    }
                };
                boxes.push((x0, y0, x0 + w, y0 + h));
            }
            for (xt, yt) in [(0.0, 0.0), (3.0, 3.0), (0.0, 8.0), (12.5, 1.5)] {
                let got = run_inputs(&boxes, xt, yt, &budget).unwrap();
                let expected = brute_force_clusters(&boxes, xt, yt);
                assert_eq!(got, expected, "seed={seed} xt={xt} yt={yt}");
            }
        }
    }

    #[test]
    fn partial_subtree_flush_then_ancestor_flush_preserves_long_lived_descendant() {
        // Exercises the subtle case where a child subtree is flushed by a short
        // box (`q`), clearing the child's subtree dirty flags while a long-lived
        // descendant (`old_box`) remains active in `sub[child]`, and a later
        // wider box (`p`) covers the parent after `q` has expired.
        let boxes = vec![
            (0.0, 70.0, 100.0, 80.0), // old_box: long-lived in right-right subtree
            (10.0, 50.0, 40.0, 90.0), // q: short box covering right child, flushes old_box
            (55.0, 0.0, 70.0, 100.0), // p: arrives after q expired (55 > 40), covers whole range
            (5.0, 10.0, 15.0, 20.0),  // left-subtree distractor
        ];
        let budget = Budget::default();
        // Verify Pass 1 (`sweep_union`) directly links 0, 1, 2 while keeping 3 disjoint.
        let mut dsu = sweep_union(&boxes, 0.0, 0.0, &budget).unwrap();
        assert_eq!(dsu.find(0), dsu.find(1));
        assert_eq!(dsu.find(0), dsu.find(2));
        assert_ne!(dsu.find(0), dsu.find(3));

        // Full clustering then absorbs box 3 via Pass 2 (inside union box 0..100 x 0..100).
        let got = run_inputs(&boxes, 0.0, 0.0, &budget).unwrap();
        let expected = brute_force_clusters(&boxes, 0.0, 0.0);
        assert_eq!(got, expected);
        assert_eq!(got, vec![((0.0, 0.0, 100.0, 100.0), vec![0, 1, 2, 3])]);
    }

    #[test]
    fn l_frame_absorbs_interior_bar_via_transitive_pass() {
        let boxes = vec![
            (10.0, 10.0, 12.0, 100.0),  // vertical axis
            (10.0, 98.0, 110.0, 100.0), // horizontal axis touching vertical axis
            (50.0, 40.0, 60.0, 60.0),   // interior bar not touching either axis
            (160.0, 10.0, 180.0, 30.0), // separate icon outside
        ];
        let budget = Budget::default();
        let got = run_inputs(&boxes, 3.0, 3.0, &budget).unwrap();
        assert_eq!(got.len(), 2);
        assert_eq!(got[0], ((10.0, 10.0, 110.0, 100.0), vec![0, 1, 2]));
        assert_eq!(got[1], ((160.0, 10.0, 180.0, 30.0), vec![3]));
    }

    #[test]
    fn large_path_count_clusters_quickly_and_honors_budget_limits() {
        // 10,000 paths arranged in 20 clusters of 500 overlapping strokes each.
        let mut inputs = Vec::with_capacity(10_000);
        for cluster in 0..20usize {
            let base_x = (cluster % 5) as f64 * 200.0;
            let base_y = (cluster / 5) as f64 * 200.0;
            for j in 0..500usize {
                let dx = (j % 25) as f64 * 4.0;
                let dy = (j / 25) as f64 * 4.0;
                inputs.push(ClusterInput {
                    bbox: (
                        base_x + dx,
                        base_y + dy,
                        base_x + dx + 5.0,
                        base_y + dy + 5.0,
                    ),
                    has_fill: true,
                    has_stroke: false,
                    stroke_width: 0.0,
                    is_rect: true,
                });
            }
        }
        let budget = Budget::default();
        let result = cluster_path_inputs(
            &inputs,
            None,
            ClusterOptions {
                x_tolerance: 3.0,
                y_tolerance: 3.0,
                exclude_background: true,
                max_clusters: Some(4096),
            },
            &budget,
        )
        .unwrap();
        assert_eq!(result.len(), 20);
        assert!(result.iter().all(|(_, indices)| indices.len() == 500));

        // max_clusters refusal returns ContentElementCount with no partial result.
        let err = cluster_path_inputs(
            &inputs,
            None,
            ClusterOptions {
                x_tolerance: 3.0,
                y_tolerance: 3.0,
                exclude_background: true,
                max_clusters: Some(10),
            },
            &budget,
        )
        .unwrap_err();
        assert!(matches!(
            err,
            Error::Limit(ref refusal) if refusal.code() == LimitCode::ContentElementCount
        ));

        // Tight memory limit refuses via MemorySize.
        let mem_limits = DocumentLimits {
            max_memory_size: Some(4096),
            ..DocumentLimits::default()
        };
        let mem_budget = Budget::new(&mem_limits);
        let err = cluster_path_inputs(
            &inputs,
            None,
            ClusterOptions {
                x_tolerance: 3.0,
                y_tolerance: 3.0,
                exclude_background: true,
                max_clusters: None,
            },
            &mem_budget,
        )
        .unwrap_err();
        assert!(matches!(
            err,
            Error::Limit(ref refusal) if refusal.code() == LimitCode::MemorySize
        ));

        // Cancellation token refuses immediately with Cancelled.
        let token = CancellationToken::new();
        token.cancel();
        let cancel_limits = DocumentLimits {
            cancellation: Some(token),
            ..DocumentLimits::default()
        };
        let cancel_budget = Budget::new(&cancel_limits);
        let err = cluster_path_inputs(
            &inputs,
            None,
            ClusterOptions {
                x_tolerance: 3.0,
                y_tolerance: 3.0,
                exclude_background: true,
                max_clusters: None,
            },
            &cancel_budget,
        )
        .unwrap_err();
        assert!(matches!(
            err,
            Error::Limit(ref refusal) if refusal.code() == LimitCode::Cancelled
        ));
    }

    #[test]
    fn extreme_finite_tolerance_never_collides_with_empty_sentinel() {
        // `x0 - x_tolerance` overflows to `-inf` for `-1e308 - 1e308`, which
        // must remain clamped above `Node::EMPTY`'s `f64::NEG_INFINITY` so
        // Y-disjoint boxes are never falsely merged with `cover_id = 0`.
        let boxes = vec![
            (-1.0e308, 0.0, -1.0e308 + 1.0e300, 10.0),
            (-1.0e308, 100.0, -1.0e308 + 1.0e300, 110.0),
            (10.0, 200.0, 20.0, 210.0),
            (500.0, 100.0, 510.0, 110.0),
        ];
        let budget = Budget::default();
        let got = run_inputs(&boxes, 1.0e308, 0.0, &budget).unwrap();
        // Box 1 and box 3 share Y=[100, 110] and are within x_tolerance=1e308;
        // boxes 0 and 2 are disjoint in Y and must stay separate.
        assert_eq!(got.len(), 3);
        assert_eq!(got[0].1, vec![0]);
        assert_eq!(got[1].1, vec![1, 3]);
        assert_eq!(got[2].1, vec![2]);
    }

    #[test]
    fn multi_pass_staircase_merges_transitively_via_origin_map() {
        // Three passes: L1 (0,1) encloses 2; adding 2 extends bbox right to
        // reach L2 (3,4) which encloses 5; separate box 6 stays isolated.
        let boxes = vec![
            (0.0, 0.0, 2.0, 50.0),     // 0: L1 vertical
            (0.0, 48.0, 40.0, 50.0),   // 1: L1 horizontal
            (35.0, 10.0, 60.0, 20.0),  // 2: inside L1 bbox, extends right to x=60
            (62.0, 10.0, 64.0, 60.0),  // 3: L2 vertical (within 3pt of box 2)
            (62.0, 58.0, 110.0, 60.0), // 4: L2 horizontal
            (90.0, 20.0, 100.0, 30.0), // 5: inside L2 bbox
            (200.0, 0.0, 220.0, 20.0), // 6: isolated
        ];
        let budget = Budget::default();
        let got = run_inputs(&boxes, 3.0, 3.0, &budget).unwrap();
        let expected = brute_force_clusters(&boxes, 3.0, 3.0);
        assert_eq!(got, expected);
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].1, vec![0, 1, 2, 3, 4, 5]);
        assert_eq!(got[1].1, vec![6]);
    }
}
