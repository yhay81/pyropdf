//! Removing a page's rotation while keeping what the page displays.
//!
//! The page's user space turns so that the new space, unrotated, shows what
//! the old one showed rotated: the content draws under one more `cm`, the
//! page boxes and the annotations' geometry move by the same matrix, and the
//! appearances of annotations turn through their `/Matrix`. Display space,
//! which every other page API uses, is the same before and after.

use std::fmt;

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::pages::PageIndex;
use pyropdf_store::refs;
use pyropdf_store::value::{Array, Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::display::{BOXES, Box4, number, rect, rotation, to_display, user_boxes};

/// The result of removing a page's rotation.
pub type Result<T, E = Error> = std::result::Result<T, E>;

/// Why removing a page's rotation refused or failed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Error {
    /// A refusal or failure from the underlying store or budget.
    Store(pyropdf_store::Error),
    /// The page shares its `/Annots` array or one of its annotations with another page.
    SharedAnnotation(String),
}

impl Error {
    /// Returns the limit code if a limit refused the operation.
    #[must_use]
    pub const fn limit_code(&self) -> Option<LimitCode> {
        match self {
            Self::Store(error) => match error.limit() {
                Some(refusal) => Some(refusal.code()),
                None => None,
            },
            Self::SharedAnnotation(_) => None,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Store(error) => fmt::Display::fmt(error, formatter),
            Self::SharedAnnotation(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for Error {}

impl From<pyropdf_store::Error> for Error {
    fn from(error: pyropdf_store::Error) -> Self {
        Self::Store(error)
    }
}

impl From<LimitExceeded> for Error {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Store(pyropdf_store::Error::from(refusal))
    }
}

/// A matrix `[a b c d e f]` that maps `(x, y)` to
/// `(a x + c y + e, b x + d y + f)`.
pub type Matrix = [f64; 6];

/// The identity CTM `[1 0 0 1 0 0]`.
pub const IDENTITY: Matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0];

/// The annotation flag that keeps an annotation upright on a rotated page
/// (ISO 32000-2 12.5.3).
const NO_ROTATE: i64 = 16;

/// Keys of annotations whose arrays list points, as pairs of numbers.
const POINTS: [&str; 4] = ["QuadPoints", "L", "Vertices", "CL"];

/// Keys of annotations whose arrays list arrays of points.
const PATHS: [&str; 2] = ["InkList", "Path"];

/// Maps `(x, y)` by `m`.
#[must_use]
pub fn apply(m: &Matrix, (x, y): (f64, f64)) -> (f64, f64) {
    (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])
}

/// Returns `m` followed by `n`.
#[must_use]
pub fn then(m: &Matrix, n: &Matrix) -> Matrix {
    [
        m[0] * n[0] + m[1] * n[2],
        m[0] * n[1] + m[1] * n[3],
        m[2] * n[0] + m[3] * n[2],
        m[2] * n[1] + m[3] * n[3],
        m[4] * n[0] + m[5] * n[2] + n[4],
        m[4] * n[1] + m[5] * n[3] + n[5],
    ]
}

/// Returns the matrix from the user space of a page with media box `media`
/// and normalized rotation `turn` to the unrotated space that displays the
/// same, whose media box starts at the origin.
#[must_use]
pub fn unrotation((x0, y0, x1, y1): Box4, turn: i64) -> Matrix {
    let height = if turn % 180 == 90 { x1 - x0 } else { y1 - y0 };
    let map = |x, y| {
        // The turn stays in user space, so its unit is the user space unit.
        let (u, v) = to_display([x0, y0, x1, y1], turn, 1.0, x, y);
        (u, height - v)
    };
    let (e, f) = map(0.0, 0.0);
    let (a, b) = map(1.0, 0.0);
    let (c, d) = map(0.0, 1.0);
    [a - e, b - f, c - e, d - f, e, f]
}

/// Maps axis-aligned rectangle `rect` by an axis-preserving matrix `m` and
/// normalizes its corners.
#[must_use]
pub fn mapped_rect(m: &Matrix, (x0, y0, x1, y1): Box4) -> Box4 {
    let (a, b) = apply(m, (x0, y0));
    let (c, d) = apply(m, (x1, y1));
    (a.min(c), b.min(d), a.max(c), b.max(d))
}

fn rect_value((x0, y0, x1, y1): Box4) -> Value {
    Value::Array(Array::from(vec![
        number(x0),
        number(y0),
        number(x1),
        number(y1),
    ]))
}

fn matrix_value([a, b, c, d, e, f]: Matrix) -> Value {
    Value::Array(Array::from(vec![
        number(a),
        number(b),
        number(c),
        number(d),
        number(e),
        number(f),
    ]))
}

#[expect(
    clippy::cast_precision_loss,
    reason = "PDF geometry coordinates are f64"
)]
fn as_number(value: &Value) -> Option<f64> {
    match value {
        Value::Integer(n) => Some(*n as f64),
        Value::Real(r) => Some(r.value()),
        _ => None,
    }
}

fn four_numbers(array: &Array) -> Option<[f64; 4]> {
    let mut nums = array.iter().filter_map(as_number);
    let four = [nums.next()?, nums.next()?, nums.next()?, nums.next()?];
    nums.next().is_none().then_some(four)
}

fn six_numbers(array: &Array) -> Option<Matrix> {
    let mut nums = array.iter().filter_map(as_number);
    let six = [
        nums.next()?,
        nums.next()?,
        nums.next()?,
        nums.next()?,
        nums.next()?,
        nums.next()?,
    ];
    nums.next().is_none().then_some(six)
}

/// Maps the points of `array`, pairs of numbers, by `m`; `None` when an item
/// is not a number or a pair is incomplete.
fn mapped_points(m: &Matrix, array: &Array, budget: &Budget) -> Result<Option<Value>> {
    if !array.len().is_multiple_of(2) {
        return Ok(None);
    }
    let mut turned = BudgetVec::new(budget);
    turned.try_reserve_exact(array.len())?;
    let mut items = array.iter();
    while let (Some(first), Some(second)) = (items.next(), items.next()) {
        budget.poll()?;
        let (Some(x), Some(y)) = (as_number(first), as_number(second)) else {
            return Ok(None);
        };
        let (nx, ny) = apply(m, (x, y));
        turned.try_push(number(nx))?;
        turned.try_push(number(ny))?;
    }
    Ok(Some(Value::Array(Array::from(turned.into_vec()))))
}

/// The state of one removal: the document read before it and the copies of
/// appearance streams that it made.
struct Turn<'a> {
    version: &'a Version,
    budget: &'a Budget,
    /// The page's matrix from old to new user space.
    matrix: Matrix,
    turn: i64,
    /// Sorted `(original_number, copy_reference)` pairs for appearance streams.
    copies: BudgetVec<'a, (u32, Reference)>,
    /// Objects that the removal stopped using, freed when nothing else does.
    unused: BudgetVec<'a, u32>,
}

impl Turn<'_> {
    fn resolved(&self, value: Option<&Value>) -> Result<Option<Value>> {
        Ok(super::resolve(self.version, value, self.budget)?)
    }

    fn copied(&self, reference: Reference) -> Option<Reference> {
        self.copies
            .binary_search_by_key(&reference.number(), |&(num, _)| num)
            .ok()
            .map(|at| self.copies[at].1)
    }

    /// Moves the geometry of annotation `dict` to the new user space and
    /// turns its appearances.
    fn annotation(&mut self, transaction: &mut Transaction, dict: &mut Dict) -> Result<()> {
        let flags = self
            .resolved(dict.get("F"))?
            .and_then(|value| as_number(&value))
            .unwrap_or(0.0) as i64;
        let area = self.resolved(dict.get("Rect"))?.as_ref().and_then(rect);
        // An upright annotation keeps its appearance and turns about the
        // upper-left corner of its rectangle, which stays in place.
        let (map, linear) = match area {
            Some((x0, _, _, y1)) if flags & NO_ROTATE != 0 => {
                let (x, y) = apply(&self.matrix, (x0, y1));
                ([1.0, 0.0, 0.0, 1.0, x - x0, y - y1], IDENTITY)
            }
            _ => {
                let [a, b, c, d, _, _] = self.matrix;
                (self.matrix, [a, b, c, d, 0.0, 0.0])
            }
        };
        if let Some(area) = area {
            let moved = mapped_rect(&map, area);
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Rect key"
            )]
            dict.insert(Name::from("Rect"), rect_value(moved));
            if let Some(Value::Array(differences)) = self.resolved(dict.get("RD"))?
                && let Some([l, t, r, b]) = four_numbers(&differences)
            {
                let inner = mapped_rect(&map, (area.0 + l, area.1 + b, area.2 - r, area.3 - t));
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /RD key"
                )]
                dict.insert(
                    Name::from("RD"),
                    rect_value((
                        inner.0 - moved.0,
                        moved.3 - inner.3,
                        moved.2 - inner.2,
                        inner.1 - moved.1,
                    )),
                );
            }
        }
        for key in POINTS {
            if let Some(Value::Array(points)) = self.resolved(dict.get(key))?
                && let Some(moved) = mapped_points(&map, &points, self.budget)?
            {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "inserts only a key from the fixed four-element POINTS array"
                )]
                dict.insert(Name::from(key), moved);
            }
        }
        for key in PATHS {
            let Some(Value::Array(paths)) = self.resolved(dict.get(key))? else {
                continue;
            };
            let mut moved = BudgetVec::new(self.budget);
            moved.try_reserve_exact(paths.len())?;
            let mut complete = true;
            for path in paths.iter() {
                let Value::Array(points) = path else {
                    complete = false;
                    break;
                };
                match mapped_points(&map, points, self.budget)? {
                    Some(points) => moved.try_push(points)?,
                    None => {
                        complete = false;
                        break;
                    }
                }
            }
            if complete {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "inserts only a key from the fixed two-element PATHS array"
                )]
                dict.insert(Name::from(key), Value::Array(Array::from(moved.into_vec())));
            }
        }
        if linear == IDENTITY {
            return Ok(());
        }
        if matches!(self.resolved(dict.get("Subtype"))?, Some(Value::Name(name)) if name.as_bytes() == b"Widget")
        {
            self.widget_rotation(dict)?;
        }
        self.appearances(transaction, dict, &linear)
    }

    /// Turns the rotation that a widget's appearance characteristics give
    /// its generated appearance, so that a regenerated appearance stays as
    /// upright on the display as before.
    fn widget_rotation(&mut self, dict: &mut Dict) -> Result<()> {
        let mut mark = match self.resolved(dict.get("MK"))? {
            Some(Value::Dict(mark)) => mark,
            _ => Dict::new(),
        };
        let current = mark.get("R").and_then(as_number).unwrap_or(0.0) as i64;
        let turned = (current - self.turn).rem_euclid(360);
        if let Some(Value::Reference(old)) = dict.get("MK") {
            self.unused.try_push(old.number())?;
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /R key"
        )]
        mark.insert(Name::from("R"), Value::Integer(turned));
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /MK key"
        )]
        dict.insert(Name::from("MK"), Value::Dict(mark));
        Ok(())
    }

    /// Replaces the appearance dictionary of `dict` with a direct one whose
    /// streams are copies turned by `linear`. The originals stay for any
    /// other object that uses them.
    fn appearances(
        &mut self,
        transaction: &mut Transaction,
        dict: &mut Dict,
        linear: &Matrix,
    ) -> Result<()> {
        let Some(Value::Dict(mut appearance)) = self.resolved(dict.get("AP"))? else {
            return Ok(());
        };
        if let Some(Value::Reference(old)) = dict.get("AP") {
            self.unused.try_push(old.number())?;
        }
        for (_, value) in appearance.iter_mut() {
            match value {
                Value::Reference(reference) => {
                    let reference = *reference;
                    if let Some(copy) = self.copied(reference) {
                        *value = Value::Reference(copy);
                        continue;
                    }
                    match self.resolved(Some(&Value::Reference(reference)))? {
                        Some(Value::Stream(stream)) => {
                            *value = self.copy_stream(transaction, reference, stream, linear)?;
                        }
                        Some(Value::Dict(mut states)) => {
                            self.unused.try_push(reference.number())?;
                            self.states(transaction, &mut states, linear)?;
                            *value = Value::Dict(states);
                        }
                        _ => {}
                    }
                }
                Value::Dict(states) => {
                    self.states(transaction, states, linear)?;
                }
                _ => {}
            }
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /AP key"
        )]
        dict.insert(Name::from("AP"), Value::Dict(appearance));
        Ok(())
    }

    fn states(
        &mut self,
        transaction: &mut Transaction,
        states: &mut Dict,
        linear: &Matrix,
    ) -> Result<()> {
        for (_, value) in states.iter_mut() {
            let Value::Reference(reference) = *value else {
                continue;
            };
            if let Some(copy) = self.copied(reference) {
                *value = Value::Reference(copy);
                continue;
            }
            if let Some(Value::Stream(stream)) =
                self.resolved(Some(&Value::Reference(reference)))?
            {
                *value = self.copy_stream(transaction, reference, stream, linear)?;
            }
        }
        Ok(())
    }

    /// Writes a copy of appearance `stream` (at `reference`) whose `/Matrix`
    /// is followed by `linear`, recorded once for all the page's annotations.
    fn copy_stream(
        &mut self,
        transaction: &mut Transaction,
        reference: Reference,
        mut stream: Box<Stream>,
        linear: &Matrix,
    ) -> Result<Value> {
        let matrix = match self.resolved(stream.dict().get("Matrix"))? {
            Some(Value::Array(items)) => six_numbers(&items).unwrap_or(IDENTITY),
            _ => IDENTITY,
        };
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Matrix key"
        )]
        stream
            .dict_mut()
            .insert(Name::from("Matrix"), matrix_value(then(&matrix, linear)));
        let copy = transaction.allocate()?;
        transaction.write(copy.number(), &Value::Stream(stream), self.budget)?;
        let key = reference.number();
        let at = self
            .copies
            .binary_search_by_key(&key, |&(num, _)| num)
            .unwrap_or_else(|at| at);
        self.copies.try_push((key, copy))?;
        self.copies[at..].rotate_right(1);
        self.unused.try_push(key)?;
        Ok(Value::Reference(copy))
    }

    /// Moves the explicit destinations under `roots` that name `page` to
    /// the turned user space, and returns whether one moved. A walk over
    /// one object's value, bounded by the budget.
    fn retarget<'v>(
        &self,
        roots: impl Iterator<Item = &'v mut Value>,
        page: Reference,
    ) -> Result<bool> {
        let mut pending = BudgetVec::new(self.budget);
        for root in roots {
            pending.try_push(root)?;
        }
        let mut moved = false;
        while let Some(value) = pending.pop() {
            self.budget.poll()?;
            match value {
                Value::Array(array) if names(array, page) => {
                    moved |= self.destination(array);
                }
                Value::Array(array) => {
                    for item in array.iter_mut() {
                        pending.try_push(item)?;
                    }
                }
                Value::Dict(dict) => {
                    for (_, item) in dict.iter_mut() {
                        pending.try_push(item)?;
                    }
                }
                _ => {}
            }
        }
        Ok(moved)
    }

    /// Moves destination `array`, whose page is the turned one. Each new
    /// coordinate depends on one old one, so a null stays null; on a
    /// quarter turn a horizontal fit becomes a vertical one.
    fn destination(&self, array: &mut Array) -> bool {
        let m = self.matrix;
        let quarter = self.turn % 180 == 90;
        let operand = |at: usize| array.get(at).and_then(as_number);
        let x_of = |x: Option<f64>, y: Option<f64>| {
            if quarter {
                y.map(|y| m[2] * y + m[4])
            } else {
                x.map(|x| m[0] * x + m[4])
            }
        };
        let y_of = |x: Option<f64>, y: Option<f64>| {
            if quarter {
                x.map(|x| m[1] * x + m[5])
            } else {
                y.map(|y| m[3] * y + m[5])
            }
        };
        let Some(&Value::Reference(page)) = array.get(0) else {
            return false;
        };
        let Some(Value::Name(fit)) = array.get(1) else {
            return false;
        };
        let opt = |v: Option<f64>| v.map_or(Value::Null, number);
        let items = match fit.as_bytes() {
            b"XYZ" => {
                let (left, top) = (operand(2), operand(3));
                vec![
                    Value::Reference(page),
                    Value::Name(Name::from("XYZ")),
                    opt(x_of(left, top)),
                    opt(y_of(left, top)),
                    opt(operand(4)),
                ]
            }
            name @ (b"FitH" | b"FitBH") => {
                let top = operand(2);
                let bounded = name == b"FitBH";
                let (fit_name, coord) = match (quarter, bounded) {
                    (true, false) => ("FitV", x_of(None, top)),
                    (true, true) => ("FitBV", x_of(None, top)),
                    (false, false) => ("FitH", y_of(None, top)),
                    (false, true) => ("FitBH", y_of(None, top)),
                };
                vec![
                    Value::Reference(page),
                    Value::Name(Name::from(fit_name)),
                    opt(coord),
                ]
            }
            name @ (b"FitV" | b"FitBV") => {
                let left = operand(2);
                let bounded = name == b"FitBV";
                let (fit_name, coord) = match (quarter, bounded) {
                    (true, false) => ("FitH", y_of(left, None)),
                    (true, true) => ("FitBH", y_of(left, None)),
                    (false, false) => ("FitV", x_of(left, None)),
                    (false, true) => ("FitBV", x_of(left, None)),
                };
                vec![
                    Value::Reference(page),
                    Value::Name(Name::from(fit_name)),
                    opt(coord),
                ]
            }
            b"FitR" => {
                let [Some(x0), Some(y0), Some(x1), Some(y1)] = [2, 3, 4, 5].map(operand) else {
                    return false;
                };
                let (a, b, c, d) = mapped_rect(&m, (x0, y0, x1, y1));
                vec![
                    Value::Reference(page),
                    Value::Name(Name::from("FitR")),
                    number(a),
                    number(b),
                    number(c),
                    number(d),
                ]
            }
            _ => return false,
        };
        *array = Array::from(items);
        true
    }
}

/// Returns whether `array` is an explicit destination of `page`.
fn names(array: &Array, page: Reference) -> bool {
    matches!(array.get(0), Some(Value::Reference(first)) if *first == page)
        && matches!(array.get(1), Some(Value::Name(fit))
            if matches!(fit.as_bytes(), b"XYZ" | b"FitH" | b"FitBH" | b"FitV" | b"FitBV" | b"FitR"))
}

/// Writes a content stream of `data` and returns its reference.
fn content(transaction: &mut Transaction, data: Bytes, budget: &Budget) -> Result<Value> {
    let reference = transaction.allocate()?;
    let stream = Stream::new(Dict::new(), data);
    transaction.write(reference.number(), &Value::from(stream), budget)?;
    Ok(Value::Reference(reference))
}

/// Sets the rotation of `page_ref` to 0 while keeping what it displays: the
/// content, the page boxes, the geometry and appearances of its annotations,
/// links, and widgets, and the explicit destinations that name the page move
/// to the turned user space.
///
/// Appearance streams are copied so objects that other pages share keep their
/// meaning. An annotation or `/Annots` array that another page lists too
/// returns [`Error::SharedAnnotation`] and rolls back any unpublished writes
/// made during this call. Returns `Ok(false)` when `index` no longer lists
/// `page_ref`.
pub fn remove_page_rotation(
    transaction: &mut Transaction,
    index: &PageIndex,
    references_cache: &refs::Cache,
    budget: &Budget,
    page_ref: Reference,
) -> Result<bool> {
    let Some(at) = index.index_of(page_ref) else {
        return Ok(false);
    };
    let page = &index.pages()[at];
    let turn = rotation(page.rotate());
    if turn == 0 {
        return Ok(true);
    }
    let savepoint = transaction.savepoint();
    let result = apply_unrotation(
        transaction,
        index,
        references_cache,
        budget,
        page_ref,
        at,
        turn,
    );
    if result.is_err() {
        transaction.rollback(savepoint);
    }
    result
}

fn apply_unrotation(
    transaction: &mut Transaction,
    index: &PageIndex,
    references_cache: &refs::Cache,
    budget: &Budget,
    page_ref: Reference,
    at: usize,
    turn: i64,
) -> Result<bool> {
    let version = transaction.snapshot();
    let page = &index.pages()[at];
    let boxes = user_boxes(page);
    let mut state = Turn {
        version: &version,
        budget,
        matrix: unrotation(boxes[0], turn),
        turn,
        copies: BudgetVec::new(budget),
        unused: BudgetVec::new(budget),
    };
    let Some(Value::Dict(mut dict)) = state.resolved(Some(&Value::Reference(page_ref)))? else {
        return Ok(false);
    };

    let mut listing = BudgetVec::new(budget);
    for (other_at, other) in index.pages().iter().enumerate() {
        if other_at == at {
            continue;
        }
        if let Some(object) = other.object() {
            listing.try_push(object.number())?;
        }
        if let Some(Value::Reference(array)) = other.dict().get("Annots") {
            listing.try_push(array.number())?;
        }
    }
    listing.sort_unstable();

    let array = match dict.get("Annots") {
        Some(Value::Reference(array)) => Some(*array),
        _ => None,
    };
    if array.is_some_and(|array| listing.binary_search(&array.number()).is_ok()) {
        return Err(Error::SharedAnnotation(
            "the page shares its /Annots array with another page".to_owned(),
        ));
    }
    let mut items = match state.resolved(dict.get("Annots"))? {
        Some(Value::Array(items)) => items,
        _ => Array::new(),
    };
    budget.check(LimitCode::AnnotationCount, items.len() as u64)?;
    let references = references_cache.in_transaction(transaction, budget)?;
    let mut direct = false;
    for item in items.iter_mut() {
        budget.poll()?;
        match item {
            &mut Value::Reference(reference) => {
                if references
                    .referrers(reference.number())
                    .iter()
                    .any(|referrer| listing.binary_search(referrer).is_ok())
                {
                    return Err(Error::SharedAnnotation(format!(
                        "annotation {} is listed by another page too",
                        reference.number()
                    )));
                }
                let Some(Value::Dict(mut annotation)) =
                    state.resolved(Some(&Value::Reference(reference)))?
                else {
                    continue;
                };
                state.annotation(transaction, &mut annotation)?;
                transaction.write(reference.number(), &Value::Dict(annotation), budget)?;
            }
            Value::Dict(annotation) => {
                state.annotation(transaction, annotation)?;
                direct = true;
            }
            _ => {}
        }
    }
    if direct {
        match array {
            Some(array) => {
                transaction.write(array.number(), &Value::Array(items), budget)?;
            }
            None => {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /Annots key"
                )]
                dict.insert(Name::from("Annots"), Value::Array(items));
            }
        }
    }

    // The media and crop boxes may be inherited; the page sets its own.
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /MediaBox key"
    )]
    dict.insert(
        Name::from(BOXES[0]),
        rect_value(mapped_rect(&state.matrix, boxes[0])),
    );
    if let Some(crop) = page.crop_box().and_then(rect) {
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /CropBox key"
        )]
        dict.insert(
            Name::from(BOXES[1]),
            rect_value(mapped_rect(&state.matrix, crop)),
        );
    }
    for key in &BOXES[2..] {
        if let Some(own) = state.resolved(dict.get(*key))?.as_ref().and_then(rect) {
            #[expect(
                clippy::disallowed_methods,
                reason = "inserts only a key from the fixed BOXES slice"
            )]
            dict.insert(
                Name::from(*key),
                rect_value(mapped_rect(&state.matrix, own)),
            );
        }
    }

    let mut contents = BudgetVec::new(budget);
    match state.resolved(dict.get("Contents"))? {
        Some(Value::Array(items)) if !items.is_empty() => {
            if let Some(Value::Reference(old)) = dict.get("Contents")
                && !items.as_slice().contains(&Value::Reference(*old))
            {
                state.unused.try_push(old.number())?;
            }
            contents.try_reserve_exact(items.len() + 2)?;
            let [a, b, c, d, e, f] = state.matrix.map(|value| value + 0.0);
            contents.try_push(content(
                transaction,
                Bytes::from(format!("q {a} {b} {c} {d} {e} {f} cm\n").into_bytes()),
                budget,
            )?)?;
            for item in items.iter() {
                contents.try_push(refs::copy_for_edit(item, budget)?)?;
            }
            contents.try_push(content(transaction, Bytes::from(&b"\nQ\n"[..]), budget)?)?;
        }
        Some(Value::Stream(_)) => {
            if let Some(entry) = dict.get("Contents") {
                contents.try_reserve_exact(3)?;
                let [a, b, c, d, e, f] = state.matrix.map(|value| value + 0.0);
                contents.try_push(content(
                    transaction,
                    Bytes::from(format!("q {a} {b} {c} {d} {e} {f} cm\n").into_bytes()),
                    budget,
                )?)?;
                contents.try_push(refs::copy_for_edit(entry, budget)?)?;
                contents.try_push(content(transaction, Bytes::from(&b"\nQ\n"[..]), budget)?)?;
            }
        }
        _ => {}
    }
    if !contents.is_empty() {
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Contents key"
        )]
        dict.insert(
            Name::from("Contents"),
            Value::Array(Array::from(contents.into_vec())),
        );
    }
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /Rotate key"
    )]
    dict.insert(Name::from("Rotate"), Value::Integer(0));

    // Destinations in the page's own direct annotations, then in every
    // other object that names the page.
    let number = page_ref.number();
    state.retarget(dict.iter_mut().map(|(_, value)| value), page_ref)?;
    transaction.write(number, &Value::Dict(dict), budget)?;
    let current = transaction.snapshot();
    for &referrer in references.referrers(number) {
        if referrer == number {
            continue;
        }
        let Some(object) = current.get(referrer, budget)? else {
            continue;
        };
        let mut value = refs::copy_for_edit(object.value(), budget)?;
        if state.retarget(std::iter::once(&mut value), page_ref)? {
            transaction.write(referrer, &value, budget)?;
        }
    }
    let unused = state.unused.into_vec();
    refs::free_unreferenced(references_cache, transaction, &[], &unused, budget)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use proptest::prelude::*;
    use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};
    use pyropdf_store::index::Index;
    use pyropdf_store::memo::Memo;
    use pyropdf_store::pages::PageIndex;
    use pyropdf_store::refs;
    use pyropdf_store::save::FileBase;
    use pyropdf_store::value::{Bytes, Name, Reference, Value};
    use pyropdf_store::version::Document;

    use crate::display::{display_box, rect, rotation, to_display, user_boxes};

    use super::*;

    fn open_pdf(objects: &[&str]) -> Document {
        let mut out = b"%PDF-1.7\n".to_vec();
        let mut offsets = Vec::new();
        for (number, object) in (1..).zip(objects) {
            offsets.push(out.len());
            out.extend_from_slice(format!("{number} 0 obj\n{object}\nendobj\n").as_bytes());
        }
        let xref = out.len();
        out.extend_from_slice(
            format!("xref\n0 {}\n0000000000 65535 f \n", offsets.len() + 1).as_bytes(),
        );
        for offset in offsets {
            out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
        }
        out.extend_from_slice(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
                objects.len() + 1
            )
            .as_bytes(),
        );
        let budget = Budget::default();
        let index = Index::open(Bytes::new(out), &budget).unwrap();
        Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None)
    }

    fn stream(data: &str, extra: &str) -> String {
        format!(
            "<< /Length {} {extra}>>\nstream\n{data}\nendstream",
            data.len()
        )
    }

    fn approx_eq(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-6
    }

    fn matrix_approx_eq(a: &Matrix, b: &Matrix) -> bool {
        a.iter().zip(b).all(|(&x, &y)| approx_eq(x, y))
    }

    proptest! {
        #[test]
        fn unrotation_preserves_display_points_and_boxes_across_all_rotations(
            mx0 in -500.0_f64..500.0,
            my0 in -500.0_f64..500.0,
            mw in 10.0_f64..1500.0,
            mh in 10.0_f64..1500.0,
            inset_l in 0.0_f64..4.0,
            inset_b in 0.0_f64..4.0,
            inset_r in 0.0_f64..4.0,
            inset_t in 0.0_f64..4.0,
            turn in prop::sample::select(vec![0_i64, 90, 180, 270]),
            unit in 0.25_f64..8.0,
            x in -1000.0_f64..1000.0,
            y in -1000.0_f64..1000.0,
        ) {
            let media = (mx0, my0, mx0 + mw, my0 + mh);
            let crop = (
                mx0 + inset_l,
                my0 + inset_b,
                mx0 + mw - inset_r,
                my0 + mh - inset_t,
            );
            let m = unrotation(media, turn);

            // Turned media box starts at the origin and swaps dimensions on quarter turns.
            let new_media = mapped_rect(&m, media);
            prop_assert!(approx_eq(new_media.0, 0.0));
            prop_assert!(approx_eq(new_media.1, 0.0));
            if turn % 180 == 90 {
                prop_assert!(approx_eq(new_media.2, mh));
                prop_assert!(approx_eq(new_media.3, mw));
            } else {
                prop_assert!(approx_eq(new_media.2, mw));
                prop_assert!(approx_eq(new_media.3, mh));
            }

            // Every point's display coordinates stay identical before and after unrotation.
            let new_crop = mapped_rect(&m, crop);
            let (nx, ny) = apply(&m, (x, y));
            let before = to_display([crop.0, crop.1, crop.2, crop.3], turn, unit, x, y);
            let after = to_display(
                [new_crop.0, new_crop.1, new_crop.2, new_crop.3],
                0,
                unit,
                nx,
                ny,
            );
            prop_assert!(approx_eq(before.0, after.0), "before={before:?} after={after:?}");
            prop_assert!(approx_eq(before.1, after.1), "before={before:?} after={after:?}");
        }

        #[test]
        fn matrix_composition_matches_sequential_point_application_and_inverses(
            m in prop::array::uniform6(-50.0_f64..50.0),
            n in prop::array::uniform6(-50.0_f64..50.0),
            x in -200.0_f64..200.0,
            y in -200.0_f64..200.0,
            w in 1.0_f64..1000.0,
            h in 1.0_f64..1000.0,
        ) {
            let composed = apply(&then(&m, &n), (x, y));
            let sequential = apply(&n, apply(&m, (x, y)));
            prop_assert!(approx_eq(composed.0, sequential.0));
            prop_assert!(approx_eq(composed.1, sequential.1));

            prop_assert!(matrix_approx_eq(&then(&IDENTITY, &m), &m));
            prop_assert!(matrix_approx_eq(&then(&m, &IDENTITY), &m));

            // Origin-anchored 0, 90+270, 180+180, and four 90-degree square turns return to IDENTITY.
            prop_assert!(matrix_approx_eq(&unrotation((0.0, 0.0, w, h), 0), &IDENTITY));
            let m90 = unrotation((0.0, 0.0, w, h), 90);
            let m270 = unrotation((0.0, 0.0, h, w), 270);
            prop_assert!(matrix_approx_eq(&then(&m90, &m270), &IDENTITY));

            let m180 = unrotation((0.0, 0.0, w, h), 180);
            prop_assert!(matrix_approx_eq(&then(&m180, &m180), &IDENTITY));

            let sq90 = unrotation((0.0, 0.0, w, w), 90);
            let sq180 = then(&sq90, &sq90);
            let sq360 = then(&sq180, &sq180);
            prop_assert!(matrix_approx_eq(&sq360, &IDENTITY));
        }
    }

    #[test]
    fn store_unrotation_handles_all_four_rotations_and_preserves_display_boxes() {
        for turn in [0_i64, 90, 180, 270] {
            let content_obj = stream("10 20 30 40 re f", "");
            let ap_obj = stream(
                "0 0 40 20 re f",
                "/Type /XObject /Subtype /Form /BBox [0 0 40 20] ",
            );
            let page_obj = format!(
                "<< /Type /Page /Parent 2 0 R /Rotate {turn} /CropBox [20 10 280 190] \
                 /TrimBox [25 15 275 185] /Contents 4 0 R \
                 /Annots [5 0 R << /Type /Annot /Subtype /Ink /Rect [50 60 90 100] \
                 /InkList [[50 60 70 80 90 100]] >>] >>"
            );
            let doc = open_pdf(&[
                "<< /Type /Catalog /Pages 2 0 R /OpenAction [3 0 R /XYZ 40 50 1.5] >>",
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 200] >>",
                &page_obj,
                &content_obj,
                "<< /Type /Annot /Subtype /Widget /FT /Tx /T (f) /Rect [100 50 140 70] \
                 /RD [1 2 3 4] /QuadPoints [100 70 140 70 100 50 140 50] /MK << /R 90 >> \
                 /AP << /N 6 0 R >> >>",
                &ap_obj,
            ]);
            let budget = Budget::default();
            let refs_cache = refs::Cache::new();

            let before_version = doc.current();
            let before_index =
                PageIndex::memoized(&Memo::new(1), &before_version, &budget).unwrap();
            let before_page = &before_index.pages()[0];
            let before_crop = display_box(before_page);
            let before_boxes: Vec<Box4> = user_boxes(before_page)
                .into_iter()
                .map(|(x0, y0, x1, y1)| {
                    let (a, b) = to_display(before_crop, turn, 1.0, x0, y0);
                    let (c, d) = to_display(before_crop, turn, 1.0, x1, y1);
                    (a.min(c), b.min(d), a.max(c), b.max(d))
                })
                .collect();

            let mut tx = doc.begin();
            let found = remove_page_rotation(
                &mut tx,
                &before_index,
                &refs_cache,
                &budget,
                Reference::new(3, 0),
            )
            .unwrap();
            assert!(found);

            let after_version = tx.commit(&budget).unwrap();
            if turn == 0 {
                assert!(
                    before_version
                        .changes(&after_version, &budget)
                        .unwrap()
                        .unwrap()
                        .is_empty()
                );
                continue;
            }

            let after_index = PageIndex::memoized(&Memo::new(1), &after_version, &budget).unwrap();
            let after_page = &after_index.pages()[0];
            assert_eq!(rotation(after_page.rotate()), 0);

            let after_crop = display_box(after_page);
            let after_boxes: Vec<Box4> = user_boxes(after_page)
                .into_iter()
                .map(|(x0, y0, x1, y1)| {
                    let (a, b) = to_display(after_crop, 0, 1.0, x0, y0);
                    let (c, d) = to_display(after_crop, 0, 1.0, x1, y1);
                    (a.min(c), b.min(d), a.max(c), b.max(d))
                })
                .collect();
            assert_eq!(after_boxes, before_boxes);

            // Content is wrapped in `q ... cm` and `Q`.
            let Some(Value::Array(contents)) = after_page.dict().get("Contents") else {
                panic!("expected wrapped /Contents array");
            };
            assert_eq!(contents.len(), 3);

            // Widget /MK /R rotated by -turn mod 360.
            let widget_obj = after_version.get(5, &budget).unwrap().unwrap();
            let Value::Dict(widget) = widget_obj.value().as_ref() else {
                panic!("widget must remain a dict");
            };
            let Some(Value::Dict(mk)) = widget.get("MK") else {
                panic!("widget must have direct /MK");
            };
            assert_eq!(
                mk.get("R"),
                Some(&Value::Integer((90 - turn).rem_euclid(360)))
            );
        }
    }

    #[test]
    fn shared_annotation_rolls_back_earlier_writes_and_stale_page_returns_false() {
        let doc = open_pdf(&[
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 300 200] >>",
            "<< /Type /Page /Parent 2 0 R /Rotate 90 /Annots [5 0 R 6 0 R] >>",
            "<< /Type /Page /Parent 2 0 R /Annots [6 0 R] >>",
            "<< /Type /Annot /Subtype /Square /Rect [10 10 30 30] >>",
            "<< /Type /Annot /Subtype /Square /Rect [40 40 60 60] >>",
        ]);
        let budget = Budget::default();
        let refs_cache = refs::Cache::new();
        let index = PageIndex::memoized(&Memo::new(1), &doc.current(), &budget).unwrap();

        let mut tx = doc.begin();
        // Missing page returns Ok(false).
        assert!(
            !remove_page_rotation(&mut tx, &index, &refs_cache, &budget, Reference::new(99, 0))
                .unwrap()
        );

        // Pre-existing write in the same transaction is preserved when unrotation rolls back.
        let marker = tx.allocate().unwrap();
        tx.write(marker.number(), &Value::Name(Name::from("Kept")), &budget)
            .unwrap();

        let err = remove_page_rotation(&mut tx, &index, &refs_cache, &budget, Reference::new(3, 0))
            .unwrap_err();
        assert!(matches!(err, Error::SharedAnnotation(_)));

        let committed = tx.commit(&budget).unwrap();
        // Annotation 5 (processed before shared annotation 6) was rolled back.
        let annot5 = committed.get(5, &budget).unwrap().unwrap();
        let Value::Dict(dict5) = annot5.value().as_ref() else {
            panic!("annot 5 is a dict");
        };
        assert_eq!(
            dict5.get("Rect").and_then(rect),
            Some((10.0, 10.0, 30.0, 30.0))
        );
        assert!(committed.get(marker.number(), &budget).unwrap().is_some());
    }

    #[test]
    fn annotation_count_budget_refuses_cleanly() {
        let doc = open_pdf(&[
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 200] >>",
            "<< /Type /Page /Parent 2 0 R /Rotate 90 /Annots [4 0 R 5 0 R] >>",
            "<< /Type /Annot /Subtype /Square /Rect [10 10 20 20] >>",
            "<< /Type /Annot /Subtype /Square /Rect [30 30 40 40] >>",
        ]);
        let refs_cache = refs::Cache::new();
        let index = PageIndex::memoized(&Memo::new(1), &doc.current(), &Budget::default()).unwrap();

        let limits = DocumentLimits::default();
        let budget = Budget::new(&limits).with_limit(LimitCode::AnnotationCount, Some(1));

        let mut tx = doc.begin();
        let err = remove_page_rotation(&mut tx, &index, &refs_cache, &budget, Reference::new(3, 0))
            .unwrap_err();
        assert_eq!(err.limit_code(), Some(LimitCode::AnnotationCount));
    }
}
