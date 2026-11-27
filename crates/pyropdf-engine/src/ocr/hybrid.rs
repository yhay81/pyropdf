//! Hybrid region-selective OCR planning and word deduplication.
//!
//! Identifies uncovered raster-image regions on mixed text-and-image pages,
//! coalesces adjacent strips and dense decorative icons under the operation's
//! [`Budget`], and filters recognized OCR words against existing native text
//! so native text is never duplicated.

use pyropdf_store::budget::{Budget, BudgetVec};

use super::{Candidate, Error, sort_candidates};

/// One display-space rectangle `(x0, y0, x1, y1)` in points.
pub type DisplayRect = (f64, f64, f64, f64);

/// One recognized word in full-page display space: `(x0, y0, x1, y1, text, confidence)`.
pub type DisplayOcrWord = (f64, f64, f64, f64, String, f64);

/// Maximum number of render-and-recognize regions planned for a single page.
pub const MAX_HYBRID_REGIONS: usize = 32;

/// Gap in display points within which adjacent or strip-sliced images coalesce.
const INITIAL_COALESCE_GAP: f64 = 4.0;
/// Progressive coalescing gaps applied only when a page still exceeds
/// [`MAX_HYBRID_REGIONS`] uncovered regions.
const PROGRESSIVE_GAPS: [f64; 4] = [16.0, 48.0, 128.0, 384.0];
/// Minimum width and height (points) of a coalesced region able to hold legible text.
const MIN_REGION_SIDE: f64 = 8.0;
/// Minimum area (square points) of a coalesced region able to hold legible text.
const MIN_REGION_AREA: f64 = 128.0;
/// Fraction of a candidate region's area covered by legible non-artifact native
/// text above which the region is considered already covered and skipped.
const REGION_COVERAGE_SKIP_THRESHOLD: f64 = 0.25;
/// Fraction of an OCR word's area covered by existing native text boxes above
/// which the OCR word is dropped as a duplicate.
const WORD_NATIVE_COVERAGE_THRESHOLD: f64 = 0.30;
/// Pairwise IoU threshold against a single existing native text box above which
/// an OCR word is dropped as a duplicate.
const WORD_NATIVE_IOU_THRESHOLD: f64 = 0.30;
/// Overlap fraction (`intersection / min_area`) above which two OCR words from
/// overlapping regions are considered duplicates of each other.
const WORD_SELF_DUPLICATE_THRESHOLD: f64 = 0.65;
/// Maximum iterative passes of pairwise box coalescing per gap level.
const MAX_COALESCE_PASSES: usize = 8;
/// Above this many intersecting text boxes, switch exact sweep-line coverage to
/// the fixed-size 64x64 stack bitgrid so worst-case work remains O(N).
const EXACT_SWEEP_MAX_BOXES: usize = 128;

#[derive(Clone, Copy, Debug, PartialEq)]
struct Rect2 {
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
}

impl Rect2 {
    fn from_tuple(raw: DisplayRect) -> Option<Self> {
        let (x0, y0, x1, y1) = raw;
        if x0.is_finite()
            && y0.is_finite()
            && x1.is_finite()
            && y1.is_finite()
            && x1 > x0
            && y1 > y0
        {
            Some(Self { x0, y0, x1, y1 })
        } else {
            None
        }
    }

    const fn to_tuple(self) -> DisplayRect {
        (self.x0, self.y0, self.x1, self.y1)
    }

    fn width(self) -> f64 {
        self.x1 - self.x0
    }

    fn height(self) -> f64 {
        self.y1 - self.y0
    }

    fn area(self) -> f64 {
        self.width() * self.height()
    }

    fn intersect(self, other: Self) -> Option<Self> {
        let x0 = self.x0.max(other.x0);
        let y0 = self.y0.max(other.y0);
        let x1 = self.x1.min(other.x1);
        let y1 = self.y1.min(other.y1);
        (x1 > x0 && y1 > y0).then_some(Self { x0, y0, x1, y1 })
    }

    fn union(self, other: Self) -> Self {
        Self {
            x0: self.x0.min(other.x0),
            y0: self.y0.min(other.y0),
            x1: self.x1.max(other.x1),
            y1: self.y1.max(other.y1),
        }
    }

    fn iou(self, other: Self) -> f64 {
        let Some(inter) = self.intersect(other) else {
            return 0.0;
        };
        let inter_area = inter.area();
        let union_area = self.area() + other.area() - inter_area;
        if union_area > 0.0 {
            inter_area / union_area
        } else {
            0.0
        }
    }
}

fn find_root(parents: &mut [usize], mut index: usize) -> usize {
    let mut root = index;
    while parents[root] != root {
        root = parents[root];
    }
    while parents[index] != index {
        let next = parents[index];
        parents[index] = root;
        index = next;
    }
    root
}

/// Coalesces rectangles separated by at most `gap` points in both axes until
/// convergence (bounded by [`MAX_COALESCE_PASSES`]).
fn coalesce_rects<'b>(
    rects: &[Rect2],
    gap: f64,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, Rect2>, Error> {
    let mut current = BudgetVec::new(budget);
    current.try_extend_from_slice(rects)?;
    for _ in 0..MAX_COALESCE_PASSES {
        budget.poll()?;
        if current.len() <= 1 {
            break;
        }
        current.sort_unstable_by(|a, b| {
            a.x0.total_cmp(&b.x0)
                .then_with(|| a.y0.total_cmp(&b.y0))
                .then_with(|| a.x1.total_cmp(&b.x1))
                .then_with(|| a.y1.total_cmp(&b.y1))
        });
        let count = current.len();
        let mut parents = BudgetVec::new(budget);
        parents.try_reserve_exact(count)?;
        parents.try_extend(0..count)?;

        let mut merged_any = false;
        for i in 0..count {
            budget.poll()?;
            let a = current[i];
            let x_limit = a.x1 + gap;
            for j in (i + 1)..count {
                let b = current[j];
                if b.x0 > x_limit {
                    break;
                }
                if b.y0 <= a.y1 + gap && a.y0 <= b.y1 + gap {
                    let ra = find_root(&mut parents, i);
                    let rb = find_root(&mut parents, j);
                    if ra != rb {
                        parents[rb] = ra;
                        merged_any = true;
                    }
                }
            }
        }
        if !merged_any {
            break;
        }

        let mut groups: BudgetVec<'_, Option<Rect2>> = BudgetVec::new(budget);
        groups.try_reserve_exact(count)?;
        groups.try_extend(std::iter::repeat_n(None, count))?;
        for (idx, rect) in current.iter().copied().enumerate() {
            let root = find_root(&mut parents, idx);
            groups[root] = Some(match groups[root] {
                Some(existing) => existing.union(rect),
                None => rect,
            });
        }
        let mut next = BudgetVec::new(budget);
        for rect in groups.into_vec().into_iter().flatten() {
            next.try_push(rect)?;
        }
        current = next;
    }
    Ok(current)
}

/// Computes the fraction of `region`'s area covered by the union of `boxes`.
///
/// Uses an O(k) upper bound first, then an exact sweep-line when at most
/// [`EXACT_SWEEP_MAX_BOXES`] boxes intersect `region`, and a zero-allocation
/// 64x64 stack bitgrid above that to prevent worst-case quadratic blowup.
fn covered_fraction(region: Rect2, boxes: &[Rect2], budget: &Budget) -> Result<f64, Error> {
    let region_area = region.area();
    if !(region_area.is_finite() && region_area > 0.0) || boxes.is_empty() {
        return Ok(0.0);
    }

    let mut clipped = BudgetVec::new(budget);
    let mut area_sum = 0.0_f64;
    for &item in boxes {
        budget.poll()?;
        if let Some(inter) = region.intersect(item) {
            area_sum += inter.area();
            clipped.try_push(inter)?;
        }
    }
    if clipped.is_empty() {
        return Ok(0.0);
    }
    if clipped.len() == 1 {
        return Ok((area_sum / region_area).clamp(0.0, 1.0));
    }

    if clipped.len() <= EXACT_SWEEP_MAX_BOXES {
        let mut xs = BudgetVec::new(budget);
        xs.try_reserve_exact(clipped.len() * 2)?;
        for rect in clipped.iter() {
            xs.try_push(rect.x0)?;
            xs.try_push(rect.x1)?;
        }
        xs.sort_unstable_by(f64::total_cmp);

        let mut covered = 0.0_f64;
        let mut y_spans = BudgetVec::new(budget);
        for pair in xs.windows(2) {
            budget.poll()?;
            let (xa, xb) = (pair[0], pair[1]);
            let dx = xb - xa;
            if dx <= 0.0 {
                continue;
            }
            y_spans.clear();
            for rect in clipped.iter() {
                if rect.x0 <= xa && rect.x1 >= xb {
                    y_spans.try_push((rect.y0, rect.y1))?;
                }
            }
            if y_spans.is_empty() {
                continue;
            }
            y_spans.sort_unstable_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.1.total_cmp(&b.1)));
            let mut cur_y0 = y_spans[0].0;
            let mut cur_y1 = y_spans[0].1;
            let mut dy = 0.0_f64;
            for &(y0, y1) in y_spans.iter().skip(1) {
                if y0 <= cur_y1 {
                    cur_y1 = cur_y1.max(y1);
                } else {
                    dy += cur_y1 - cur_y0;
                    cur_y0 = y0;
                    cur_y1 = y1;
                }
            }
            dy += cur_y1 - cur_y0;
            covered += dx * dy;
        }
        return Ok((covered / region_area).clamp(0.0, 1.0));
    }

    // 64x64 stack bitgrid fallback for pages with > 128 intersecting text runs.
    let mut rows = [0u64; 64];
    let inv_w = 64.0 / region.width();
    let inv_h = 64.0 / region.height();
    for rect in clipped.iter() {
        budget.poll()?;
        let gx0 = (((rect.x0 - region.x0) * inv_w).floor() as i64).clamp(0, 63) as usize;
        let gx1 = (((rect.x1 - region.x0) * inv_w).ceil() as i64).clamp(1, 64) as usize;
        let gy0 = (((rect.y0 - region.y0) * inv_h).floor() as i64).clamp(0, 63) as usize;
        let gy1 = (((rect.y1 - region.y0) * inv_h).ceil() as i64).clamp(1, 64) as usize;
        let mask = if gx1 - gx0 >= 64 {
            u64::MAX
        } else {
            ((1u64 << (gx1 - gx0)) - 1) << gx0
        };
        for row in &mut rows[gy0..gy1] {
            *row |= mask;
        }
    }
    let filled: u32 = rows.iter().map(|row| row.count_ones()).sum();
    let grid_fraction = f64::from(filled) / 4096.0;
    let upper_bound = (area_sum / region_area).clamp(0.0, 1.0);
    Ok(grid_fraction.min(upper_bound))
}

/// Plans display-space candidate OCR regions from placed raster image boxes
/// and legible non-artifact native text boxes within `bounds`.
///
/// Adjacent/strip-sliced images are coalesced first so strip-encoded scans
/// become one region, tiny isolated decorative images are discarded, regions
/// already covered by legible native text are skipped, and dense grids of
/// images coalesce progressively so at most [`MAX_HYBRID_REGIONS`] regions are
/// returned.
pub fn plan_hybrid_regions(
    images: &[DisplayRect],
    coverage_text: &[DisplayRect],
    bounds: DisplayRect,
    budget: &Budget,
) -> Result<Vec<DisplayRect>, Error> {
    budget.poll()?;
    let Some(bounds_rect) = Rect2::from_tuple(bounds) else {
        return Ok(Vec::new());
    };

    let mut clipped_images = BudgetVec::new(budget);
    for &raw in images {
        budget.poll()?;
        if let Some(rect) = Rect2::from_tuple(raw).and_then(|r| r.intersect(bounds_rect)) {
            clipped_images.try_push(rect)?;
        }
    }
    if clipped_images.is_empty() {
        return Ok(Vec::new());
    }

    let mut text_boxes = BudgetVec::new(budget);
    for &raw in coverage_text {
        budget.poll()?;
        if let Some(rect) = Rect2::from_tuple(raw).and_then(|r| r.intersect(bounds_rect)) {
            text_boxes.try_push(rect)?;
        }
    }

    // 1. Coalesce touching or strip-sliced images before applying minimum-size filtering.
    let coalesced = coalesce_rects(&clipped_images, INITIAL_COALESCE_GAP, budget)?;

    // 2. Discard tiny decorative fragments and regions already covered by legible native text.
    let mut regions = BudgetVec::new(budget);
    for &rect in coalesced.iter() {
        budget.poll()?;
        if rect.width() < MIN_REGION_SIDE
            || rect.height() < MIN_REGION_SIDE
            || rect.area() < MIN_REGION_AREA
        {
            continue;
        }
        let covered = covered_fraction(rect, &text_boxes, budget)?;
        if covered >= REGION_COVERAGE_SKIP_THRESHOLD {
            continue;
        }
        regions.try_push(rect)?;
    }

    // 3. Bound region count on pages with many scattered decorative images.
    for gap in PROGRESSIVE_GAPS {
        if regions.len() <= MAX_HYBRID_REGIONS {
            break;
        }
        regions = coalesce_rects(&regions, gap, budget)?;
    }
    if regions.len() > MAX_HYBRID_REGIONS {
        let mut combined = regions[0];
        for &rect in regions.iter().skip(1) {
            combined = combined.union(rect);
        }
        regions.clear();
        regions.try_push(combined)?;
    }

    // 4. Deterministic top-to-bottom, left-to-right order.
    regions.sort_unstable_by(|a, b| {
        a.y0.total_cmp(&b.y0)
            .then_with(|| a.x0.total_cmp(&b.x0))
            .then_with(|| a.y1.total_cmp(&b.y1))
            .then_with(|| a.x1.total_cmp(&b.x1))
    });

    let mut out = BudgetVec::new(budget);
    out.try_reserve_exact(regions.len())?;
    for rect in regions.iter() {
        out.try_push(rect.to_tuple())?;
    }
    Ok(out.into_vec())
}

/// Rotates a display-space box clockwise by `rotation` degrees within `(width, height)`
/// so upright reading-order sorting matches `Engine::recognize`.
fn rotate_box_clockwise(rect: Rect2, width: f64, height: f64, rotation: usize) -> Rect2 {
    match rotation {
        90 => Rect2 {
            x0: height - rect.y1,
            y0: rect.x0,
            x1: height - rect.y0,
            y1: rect.x1,
        },
        180 => Rect2 {
            x0: width - rect.x1,
            y0: height - rect.y1,
            x1: width - rect.x0,
            y1: height - rect.y0,
        },
        270 => Rect2 {
            x0: rect.y0,
            y0: width - rect.x1,
            x1: rect.y1,
            y1: width - rect.x0,
        },
        _ => rect,
    }
}

/// Filters recognized OCR words against existing native text boxes so native
/// text is never duplicated, optionally removes cross-region duplicates, and
/// optionally orders the surviving words in column-aware reading order.
pub fn filter_hybrid_words(
    words: &[DisplayOcrWord],
    native_boxes: &[DisplayRect],
    rotation: usize,
    page_size: (f64, f64),
    deduplicate_words: bool,
    sort_reading_order: bool,
    budget: &Budget,
) -> Result<Vec<DisplayOcrWord>, Error> {
    if !matches!(rotation, 0 | 90 | 180 | 270) {
        return Err(Error::Failed(
            "rotation must be 0, 90, 180, or 270".to_owned(),
        ));
    }
    budget.poll()?;

    let mut native = BudgetVec::new(budget);
    for &raw in native_boxes {
        budget.poll()?;
        if let Some(rect) = Rect2::from_tuple(raw) {
            native.try_push(rect)?;
        }
    }

    // 1. Filter out OCR words that overlap existing native text boxes.
    let mut kept_indices = BudgetVec::new(budget);
    let mut kept_rects = BudgetVec::new(budget);
    for (idx, word) in words.iter().enumerate() {
        budget.poll()?;
        if word.4.trim().is_empty() {
            continue;
        }
        let Some(rect) = Rect2::from_tuple((word.0, word.1, word.2, word.3)) else {
            continue;
        };
        if !native.is_empty() {
            let covered = covered_fraction(rect, &native, budget)?;
            let overlaps_native = covered >= WORD_NATIVE_COVERAGE_THRESHOLD
                || native
                    .iter()
                    .any(|&n| rect.iou(n) >= WORD_NATIVE_IOU_THRESHOLD);
            if overlaps_native {
                continue;
            }
        }
        kept_indices.try_push(idx)?;
        kept_rects.try_push(rect)?;
    }

    // 2. Optionally deduplicate near-identical OCR words from adjacent/overlapping regions.
    let mut deduped = BudgetVec::new(budget);
    if deduplicate_words && kept_indices.len() > 1 {
        let mut suppressed = BudgetVec::new(budget);
        suppressed.try_reserve_exact(kept_indices.len())?;
        suppressed.try_extend(std::iter::repeat_n(false, kept_indices.len()))?;
        for i in 0..kept_indices.len() {
            budget.poll()?;
            if suppressed[i] {
                continue;
            }
            let a = kept_rects[i];
            let a_conf = words[kept_indices[i]].5;
            for j in (i + 1)..kept_indices.len() {
                if suppressed[j] {
                    continue;
                }
                let b = kept_rects[j];
                let Some(inter) = a.intersect(b) else {
                    continue;
                };
                let min_area = a.area().min(b.area());
                if min_area > 0.0 && inter.area() / min_area >= WORD_SELF_DUPLICATE_THRESHOLD {
                    let b_conf = words[kept_indices[j]].5;
                    if b_conf > a_conf {
                        suppressed[i] = true;
                        break;
                    }
                    suppressed[j] = true;
                }
            }
        }
        for (pos, &orig_idx) in kept_indices.iter().enumerate() {
            if !suppressed[pos] {
                deduped.try_push((orig_idx, kept_rects[pos]))?;
            }
        }
    } else {
        for (pos, &orig_idx) in kept_indices.iter().enumerate() {
            deduped.try_push((orig_idx, kept_rects[pos]))?;
        }
    }

    // 3. Optionally order surviving words in column-aware reading order.
    let mut result = BudgetVec::new(budget);
    result.try_reserve_exact(deduped.len())?;
    if sort_reading_order && deduped.len() > 1 {
        let (page_w, page_h) = page_size;
        let mut candidates = BudgetVec::new(budget);
        candidates.try_reserve_exact(deduped.len())?;
        for &(orig_idx, rect) in deduped.iter() {
            let oriented = rotate_box_clockwise(rect, page_w, page_h, rotation);
            candidates.try_push(Candidate {
                x0: oriented.x0 as f32,
                y0: oriented.y0 as f32,
                x1: oriented.x1 as f32,
                y1: oriented.y1 as f32,
                score: words[orig_idx].5 as f32,
                index: orig_idx,
            })?;
        }
        let mut sorted = candidates.into_vec();
        sort_candidates(&mut sorted)?;
        for candidate in sorted {
            result.try_push(words[candidate.index].clone())?;
        }
    } else {
        for &(orig_idx, _) in deduped.iter() {
            result.try_push(words[orig_idx].clone())?;
        }
    }
    Ok(result.into_vec())
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyropdf_store::budget::LimitCode;
    use std::time::Duration;

    #[test]
    fn strip_sliced_scan_coalesces_into_one_region() {
        let budget = Budget::default();
        let strips: Vec<DisplayRect> = (0..200)
            .map(|i| {
                let y0 = 20.0 + i as f64 * 3.0;
                (20.0, y0, 580.0, y0 + 3.0)
            })
            .collect();
        let regions = plan_hybrid_regions(&strips, &[], (0.0, 0.0, 612.0, 792.0), &budget).unwrap();
        assert_eq!(regions, vec![(20.0, 20.0, 580.0, 620.0)]);
    }

    #[test]
    fn tiny_isolated_decorative_images_are_ignored() {
        let budget = Budget::default();
        let icons: Vec<DisplayRect> = (0..100)
            .map(|i| {
                let x = 20.0 + (i % 10) as f64 * 50.0;
                let y = 20.0 + (i / 10) as f64 * 50.0;
                (x, y, x + 6.0, y + 6.0)
            })
            .collect();
        let regions = plan_hybrid_regions(&icons, &[], (0.0, 0.0, 612.0, 792.0), &budget).unwrap();
        assert!(regions.is_empty());
    }

    #[test]
    fn many_scattered_images_coalesce_within_region_cap() {
        let budget = Budget::default();
        let tiles: Vec<DisplayRect> = (0..80)
            .map(|i| {
                let x = 10.0 + (i % 10) as f64 * 40.0;
                let y = 10.0 + (i / 10) as f64 * 40.0;
                (x, y, x + 20.0, y + 20.0)
            })
            .collect();
        let regions = plan_hybrid_regions(&tiles, &[], (0.0, 0.0, 612.0, 792.0), &budget).unwrap();
        assert!(!regions.is_empty());
        assert!(regions.len() <= MAX_HYBRID_REGIONS);
    }

    #[test]
    fn coverage_skips_text_covered_banner_but_keeps_tiny_overlap_scan() {
        let budget = Budget::default();
        let banner = (40.0, 20.0, 240.0, 50.0); // area 6000
        let scan = (40.0, 100.0, 440.0, 300.0); // area 80000
        let native_text = [
            // Covers 180 * 18 = 3240 (54% of banner).
            (50.0, 26.0, 230.0, 44.0),
            // Tiny edge overlap on top of scan: 100 * 4 = 400 (0.5% of scan).
            (50.0, 96.0, 150.0, 104.0),
        ];
        let regions = plan_hybrid_regions(
            &[banner, scan],
            &native_text,
            (0.0, 0.0, 612.0, 792.0),
            &budget,
        )
        .unwrap();
        assert_eq!(regions, vec![scan]);
    }

    #[test]
    fn repeated_overlapping_text_boxes_do_not_double_count_coverage() {
        let budget = Budget::default();
        let image = (0.0, 0.0, 200.0, 200.0); // area 40000
        // 10 identical copies of a 20x20 box (true union area = 400 = 1%, sum = 4000 = 10%).
        let repeated = [(10.0, 10.0, 30.0, 30.0); 10];
        let regions =
            plan_hybrid_regions(&[image], &repeated, (0.0, 0.0, 200.0, 200.0), &budget).unwrap();
        assert_eq!(regions, vec![image]);

        // Even > 128 identical copies (bitgrid path) must not exceed true 1% coverage.
        let many = vec![(10.0, 10.0, 30.0, 30.0); 150];
        let regions =
            plan_hybrid_regions(&[image], &many, (0.0, 0.0, 200.0, 200.0), &budget).unwrap();
        assert_eq!(regions, vec![image]);
    }

    #[test]
    fn word_filter_drops_native_duplicates_and_keeps_adjacent_ocr_words() {
        let budget = Budget::default();
        // Native line made of two individual words, OCR detected as one merged phrase.
        let native = [(20.0, 16.0, 70.0, 30.0), (76.0, 16.0, 130.0, 30.0)];
        let words = vec![
            (16.0, 12.0, 134.0, 34.0, "Digital header".to_owned(), 0.99),
            (20.0, 120.0, 160.0, 145.0, "Scanned body".to_owned(), 0.95),
        ];
        let kept =
            filter_hybrid_words(&words, &native, 0, (360.0, 220.0), true, true, &budget).unwrap();
        assert_eq!(kept.len(), 1);
        assert_eq!(kept[0].4, "Scanned body");
    }

    #[test]
    fn planner_observes_expired_deadline_and_memory_limit() {
        let expired = Budget::default().with_timeout(Duration::ZERO);
        let err = plan_hybrid_regions(
            &[(0.0, 0.0, 100.0, 100.0)],
            &[],
            (0.0, 0.0, 100.0, 100.0),
            &expired,
        )
        .unwrap_err();
        assert!(matches!(err, Error::Limit(refusal) if refusal.code() == LimitCode::Deadline));

        let tight = Budget::default().with_limit(LimitCode::MemorySize, Some(1));
        let err = plan_hybrid_regions(
            &[(0.0, 0.0, 100.0, 100.0)],
            &[],
            (0.0, 0.0, 100.0, 100.0),
            &tight,
        )
        .unwrap_err();
        assert!(matches!(err, Error::Limit(refusal) if refusal.code() == LimitCode::MemorySize));
    }
}
