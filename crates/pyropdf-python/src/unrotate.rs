//! Removing a page's rotation while keeping what the page displays.
//!
//! The page's user space turns so that the new space, unrotated, shows what
//! the old one showed rotated: the content draws under one more `cm`, the
//! page boxes and the annotations' geometry move by the same matrix, and the
//! appearances of annotations turn through their `/Matrix`. Display space,
//! which every other page API uses, is the same before and after.

use std::collections::{HashMap, HashSet};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::{Array, Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::annotations::{CopyPart, charge_copy, to_display};
use crate::document::_Document;
use crate::errors::{limit_error, store_error};
use crate::outline::free;
use crate::pages::{BOXES, Box4, number, rect, resolve, rotation, user_boxes};

/// A matrix `[a b c d e f]` that maps `(x, y)` to
/// `(a x + c y + e, b x + d y + f)`.
type Matrix = [f64; 6];

const IDENTITY: Matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0];

/// The annotation flag that keeps an annotation upright on a rotated page
/// (ISO 32000-2 12.5.3).
const NO_ROTATE: i64 = 16;

/// Keys of annotations whose arrays list points, as pairs of numbers.
const POINTS: [&str; 4] = ["QuadPoints", "L", "Vertices", "CL"];

/// Keys of annotations whose arrays list arrays of points.
const PATHS: [&str; 2] = ["InkList", "Path"];

fn apply(m: &Matrix, (x, y): (f64, f64)) -> (f64, f64) {
    (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])
}

/// Returns `m` followed by `n`.
fn then(m: &Matrix, n: &Matrix) -> Matrix {
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
fn unrotation((x0, y0, x1, y1): Box4, turn: i64) -> Matrix {
    let height = if turn % 180 == 90 { x1 - x0 } else { y1 - y0 };
    let map = |x, y| {
        let (u, v) = to_display([x0, y0, x1, y1], turn, x, y);
        (u, height - v)
    };
    let (e, f) = map(0.0, 0.0);
    let (a, b) = map(1.0, 0.0);
    let (c, d) = map(0.0, 1.0);
    [a - e, b - f, c - e, d - f, e, f]
}

fn mapped_rect(m: &Matrix, (x0, y0, x1, y1): Box4) -> Box4 {
    let (a, b) = apply(m, (x0, y0));
    let (c, d) = apply(m, (x1, y1));
    (a.min(c), b.min(d), a.max(c), b.max(d))
}

fn numbers(values: &[f64]) -> Value {
    Value::Array(Array::from(
        values
            .iter()
            .map(|&value| number(value))
            .collect::<Vec<_>>(),
    ))
}

fn rect_value((x0, y0, x1, y1): Box4) -> Value {
    numbers(&[x0, y0, x1, y1])
}

fn as_number(value: &Value) -> Option<f64> {
    match value {
        Value::Integer(n) => Some(*n as f64),
        Value::Real(r) => Some(r.value()),
        _ => None,
    }
}

/// Maps the points of `array`, pairs of numbers, by `m`; `None` when an item
/// is not a number or a pair is incomplete.
fn mapped_points(m: &Matrix, array: &Array, budget: &Budget) -> PyResult<Option<Value>> {
    let mut values = Vec::new();
    for item in array.iter() {
        let Some(value) = as_number(item) else {
            return Ok(None);
        };
        values.push(value);
    }
    if values.len() % 2 != 0 {
        return Ok(None);
    }
    for [x, y] in values.as_chunks_mut::<2>().0 {
        budget.poll().map_err(|e| limit_error(&e))?;
        (*x, *y) = apply(m, (*x, *y));
    }
    Ok(Some(numbers(&values)))
}

/// The state of one removal: the document read before it and the copies of
/// appearance streams that it made.
struct Turn<'a> {
    version: &'a Version,
    budget: &'a Budget,
    /// The page's matrix from old to new user space.
    matrix: Matrix,
    turn: i64,
    /// The copy of each appearance stream that the page's annotations use.
    copies: HashMap<u32, Reference>,
    /// Objects that the removal stopped using, freed when nothing else does.
    unused: Vec<u32>,
}

impl Turn<'_> {
    fn resolved(&self, value: Option<&Value>) -> PyResult<Option<Value>> {
        resolve(self.version, value, self.budget)
    }

    /// Moves the geometry of annotation `dict` to the new user space and
    /// turns its appearances.
    fn annotation(&mut self, transaction: &mut Transaction, dict: &mut Dict) -> PyResult<()> {
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
            dict.insert(Name::from("Rect"), rect_value(moved));
            if let Some(Value::Array(differences)) = self.resolved(dict.get("RD"))?
                && let [l, t, r, b] =
                    differences.iter().filter_map(as_number).collect::<Vec<_>>()[..]
            {
                let inner = mapped_rect(&map, (area.0 + l, area.1 + b, area.2 - r, area.3 - t));
                dict.insert(
                    Name::from("RD"),
                    numbers(&[
                        inner.0 - moved.0,
                        moved.3 - inner.3,
                        moved.2 - inner.2,
                        inner.1 - moved.1,
                    ]),
                );
            }
        }
        for key in POINTS {
            if let Some(Value::Array(points)) = self.resolved(dict.get(key))?
                && let Some(moved) = mapped_points(&map, &points, self.budget)?
            {
                dict.insert(Name::from(key), moved);
            }
        }
        for key in PATHS {
            let Some(Value::Array(paths)) = self.resolved(dict.get(key))? else {
                continue;
            };
            let mut moved = Vec::new();
            for path in paths.iter() {
                let Value::Array(points) = path else {
                    moved.clear();
                    break;
                };
                match mapped_points(&map, points, self.budget)? {
                    Some(points) => moved.push(points),
                    None => {
                        moved.clear();
                        break;
                    }
                }
            }
            if moved.len() == paths.len() {
                dict.insert(Name::from(key), Value::Array(Array::from(moved)));
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
    fn widget_rotation(&mut self, dict: &mut Dict) -> PyResult<()> {
        let mut mark = match self.resolved(dict.get("MK"))? {
            Some(Value::Dict(mark)) => mark,
            _ => Dict::new(),
        };
        let current = mark.get("R").and_then(as_number).unwrap_or(0.0) as i64;
        let turned = (current - self.turn).rem_euclid(360);
        if let Some(Value::Reference(old)) = dict.get("MK") {
            self.unused.push(old.number());
        }
        mark.insert(Name::from("R"), Value::Integer(turned));
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
    ) -> PyResult<()> {
        let Some(Value::Dict(appearance)) = self.resolved(dict.get("AP"))? else {
            return Ok(());
        };
        if let Some(Value::Reference(old)) = dict.get("AP") {
            self.unused.push(old.number());
        }
        let mut turned = Dict::new();
        for (key, value) in appearance.iter() {
            let value = match value {
                Value::Reference(reference) => match self.resolved(Some(value))? {
                    Some(Value::Stream(_)) => self.copy(transaction, *reference, linear)?,
                    Some(Value::Dict(states)) => {
                        self.unused.push(reference.number());
                        Value::Dict(self.states(transaction, &states, linear)?)
                    }
                    _ => value.clone(),
                },
                Value::Dict(states) => Value::Dict(self.states(transaction, states, linear)?),
                other => other.clone(),
            };
            turned.insert(key.clone(), value);
        }
        dict.insert(Name::from("AP"), Value::Dict(turned));
        Ok(())
    }

    fn states(
        &mut self,
        transaction: &mut Transaction,
        states: &Dict,
        linear: &Matrix,
    ) -> PyResult<Dict> {
        let mut turned = Dict::new();
        for (state, value) in states.iter() {
            let value = match value {
                Value::Reference(reference)
                    if matches!(self.resolved(Some(value))?, Some(Value::Stream(_))) =>
                {
                    self.copy(transaction, *reference, linear)?
                }
                other => other.clone(),
            };
            turned.insert(state.clone(), value);
        }
        Ok(turned)
    }

    /// Returns a copy of appearance stream `reference` whose `/Matrix` is
    /// followed by `linear`, made once for all the page's annotations.
    fn copy(
        &mut self,
        transaction: &mut Transaction,
        reference: Reference,
        linear: &Matrix,
    ) -> PyResult<Value> {
        if let Some(copy) = self.copies.get(&reference.number()) {
            return Ok(Value::Reference(*copy));
        }
        let Some(Value::Stream(stream)) = self.resolved(Some(&Value::Reference(reference)))? else {
            return Ok(Value::Reference(reference));
        };
        let matrix = match self.resolved(stream.dict().get("Matrix"))? {
            Some(Value::Array(items)) => {
                match items.iter().filter_map(as_number).collect::<Vec<_>>()[..] {
                    [a, b, c, d, e, f] => [a, b, c, d, e, f],
                    _ => IDENTITY,
                }
            }
            _ => IDENTITY,
        };
        let mut dict = stream.dict().clone();
        dict.insert(Name::from("Matrix"), numbers(&then(&matrix, linear)));
        let copy = transaction.allocate().map_err(|e| store_error(&e))?;
        let turned = Stream::new(dict, stream.data().clone());
        transaction
            .write(copy.number(), &Value::from(turned), self.budget)
            .map_err(|e| store_error(&e))?;
        self.copies.insert(reference.number(), copy);
        self.unused.push(reference.number());
        Ok(Value::Reference(copy))
    }
}

/// Sets the rotation of page object `(number, generation)` to 0 while
/// keeping what it displays, in one transaction: the content, the page
/// boxes, and the geometry and appearances of its annotations, links, and
/// widgets move to the turned user space. Appearance streams are copied, so
/// objects that other pages share keep their meaning. An annotation or
/// `/Annots` array that another page lists too raises `ValueError` and
/// changes nothing. Returns `false` for a stale page.
#[pyfunction]
pub fn remove_page_rotation(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<bool> {
    document.edit(py, |transaction, budget| {
        let version = transaction.snapshot();
        let index = PageIndex::memoized(&document.draft_pages, &version, budget)
            .map_err(|e| store_error(&e))?;
        let Some(at) = index.index_of(Reference::new(number, generation)) else {
            return Ok(false);
        };
        let page = &index.pages()[at];
        let turn = rotation(page.rotate());
        if turn == 0 {
            return Ok(true);
        }
        let boxes = user_boxes(page);
        let mut state = Turn {
            version: &version,
            budget,
            matrix: unrotation(boxes[0], turn),
            turn,
            copies: HashMap::new(),
            unused: Vec::new(),
        };
        charge_copy(CopyPart::Dict(page.dict()), budget)?;
        let mut dict = page.dict().clone();

        let others = index
            .pages()
            .iter()
            .enumerate()
            .filter(|&(other, _)| other != at)
            .map(|(_, page)| page);
        let mut listing = HashSet::new();
        for other in others {
            if let Some(object) = other.object() {
                listing.insert(object.number());
            }
            if let Some(Value::Reference(array)) = other.dict().get("Annots") {
                listing.insert(array.number());
            }
        }
        let array = match dict.get("Annots") {
            Some(Value::Reference(array)) => Some(*array),
            _ => None,
        };
        if array.is_some_and(|array| listing.contains(&array.number())) {
            return Err(PyValueError::new_err(
                "the page shares its /Annots array with another page",
            ));
        }
        let mut items = match state.resolved(dict.get("Annots"))? {
            Some(Value::Array(items)) => items,
            _ => Array::new(),
        };
        budget
            .check(LimitCode::AnnotationCount, items.len() as u64)
            .map_err(|e| limit_error(&e))?;
        let references = crate::outline::references(document, transaction, budget)?;
        let mut direct = false;
        for item in items.iter_mut() {
            budget.poll().map_err(|e| limit_error(&e))?;
            match item {
                &mut Value::Reference(reference) => {
                    if references
                        .referrers(reference.number())
                        .iter()
                        .any(|referrer| listing.contains(referrer))
                    {
                        return Err(PyValueError::new_err(format!(
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
                    transaction
                        .write(reference.number(), &Value::Dict(annotation), budget)
                        .map_err(|e| store_error(&e))?;
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
                    transaction
                        .write(array.number(), &Value::Array(items), budget)
                        .map_err(|e| store_error(&e))?;
                }
                None => {
                    dict.insert(Name::from("Annots"), Value::Array(items));
                }
            }
        }

        // The media and crop boxes may be inherited; the page sets its own.
        dict.insert(
            Name::from(BOXES[0]),
            rect_value(mapped_rect(&state.matrix, boxes[0])),
        );
        if let Some(crop) = page.crop_box().and_then(rect) {
            dict.insert(
                Name::from(BOXES[1]),
                rect_value(mapped_rect(&state.matrix, crop)),
            );
        }
        for key in &BOXES[2..] {
            if let Some(own) = state.resolved(dict.get(*key))?.as_ref().and_then(rect) {
                dict.insert(
                    Name::from(*key),
                    rect_value(mapped_rect(&state.matrix, own)),
                );
            }
        }
        let contents = match state.resolved(dict.get("Contents"))? {
            Some(Value::Array(items)) => items.iter().cloned().collect(),
            Some(Value::Stream(_)) => dict.get("Contents").into_iter().cloned().collect(),
            _ => Vec::new(),
        };
        if !contents.is_empty() {
            if let Some(Value::Reference(old)) = dict.get("Contents")
                && !contents.contains(&Value::Reference(*old))
            {
                state.unused.push(old.number());
            }
            let [a, b, c, d, e, f] = state.matrix.map(|value| value + 0.0);
            let mut array = Array::new();
            array.push(content(
                transaction,
                format!("q {a} {b} {c} {d} {e} {f} cm\n"),
                budget,
            )?);
            for item in contents {
                array.push(item);
            }
            array.push(content(transaction, "\nQ\n".to_owned(), budget)?);
            dict.insert(Name::from("Contents"), Value::Array(array));
        }
        dict.insert(Name::from("Rotate"), Value::Integer(0));
        transaction
            .write(number, &Value::Dict(dict), budget)
            .map_err(|e| store_error(&e))?;
        let unused = std::mem::take(&mut state.unused);
        free(document, transaction, &unused, budget)?;
        Ok(true)
    })
}

/// Writes a content stream of `data` and returns its reference.
fn content(transaction: &mut Transaction, data: String, budget: &Budget) -> PyResult<Value> {
    let reference = transaction.allocate().map_err(|e| store_error(&e))?;
    let stream = Stream::new(Dict::new(), Bytes::from(data.into_bytes()));
    transaction
        .write(reference.number(), &Value::from(stream), budget)
        .map_err(|e| store_error(&e))?;
    Ok(Value::Reference(reference))
}
