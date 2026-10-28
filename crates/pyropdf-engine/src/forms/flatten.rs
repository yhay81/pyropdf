//! Flattening: the appearances of annotations and widgets drawn into page
//! content, and the annotations taken off their pages (yhay81/pyropdf#197).
//!
//! Each drawn annotation shows what a render shows: its normal appearance,
//! or the state of it that `/AS` names, mapped onto its `/Rect` as ISO
//! 32000-2 §12.5.5 maps it, or, for one stored without, the appearance that
//! a render synthesizes ([`synthesized`](crate::annotations::synthesized)).
//! Links stay, popups leave with the annotations, and hidden annotations
//! leave undrawn. hayro stops drawing a page's annotations at an entry that
//! is no annotation dictionary, so flattening draws none after it either.
//! A page draws its appearances through one form, so its content grows by
//! one stream per flattening. Ported from pylopdf's
//! `rust/src/document/flattening.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321.

use std::collections::HashMap;
use std::sync::Arc;

use pyropdf_store::Result;
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::pages::{MAX_CHAIN, Page};
use pyropdf_store::value::{Array, Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::Transaction;

use crate::annotations::{Reader, fmt, numbers_array};

/// The annotation flag that hides an annotation (ISO 32000-2 Table 167).
const HIDDEN: i64 = 1 << 1;

/// The resource name of the form that draws a page's flattened
/// appearances, with a number after it when the page uses the name.
const RESOURCE: &str = "Flattened";

/// The annotations that flattening takes off.
#[derive(Clone, Copy, Debug)]
pub struct Selection {
    /// Every annotation but links and widgets.
    pub annotations: bool,
    /// The widgets of form fields.
    pub widgets: bool,
}

/// What flattening does with the `/Annots` of one page.
#[derive(Debug)]
pub struct Plan {
    /// The entries that stay.
    keep: Vec<Value>,
    /// The entries whose appearances are drawn.
    drawn: Vec<Value>,
    /// The widgets taken off, by reference.
    pub widgets: Vec<Reference>,
}

/// Returns what flattening `selection` does with the `/Annots` entries
/// `items` of a page, or `None` when it takes none of them off. The plan's
/// lists, at most one entry of each per item, are charged up front.
pub fn plan(reader: &Reader<'_>, items: &Array, selection: Selection) -> Result<Option<Plan>> {
    let entry = 2 * size_of::<Value>() + size_of::<Reference>();
    reader
        .budget
        .charge(LimitCode::MemorySize, (items.len() * entry) as u64)?;
    let mut plan = Plan {
        keep: Vec::new(),
        drawn: Vec::new(),
        widgets: Vec::new(),
    };
    let mut changed = false;
    let mut drawing = true;
    for item in items.iter() {
        reader.budget.poll()?;
        let resolved = reader.resolve(item)?;
        let Value::Dict(annotation) = &*resolved else {
            drawing = false;
            plan.keep.push(item.clone());
            continue;
        };
        let subtype = match reader.entry(annotation, "Subtype")?.as_deref() {
            Some(Value::Name(name)) => Some(name.as_bytes().to_vec()),
            _ => None,
        };
        let selected = match subtype.as_deref() {
            Some(b"Link") => false,
            Some(b"Widget") => selection.widgets,
            _ => selection.annotations,
        };
        if !selected {
            plan.keep.push(item.clone());
            continue;
        }
        changed = true;
        if subtype.as_deref() == Some(b"Widget")
            && let Value::Reference(reference) = item
        {
            plan.widgets.push(*reference);
        }
        let flags = match reader.entry(annotation, "F")?.as_deref() {
            Some(Value::Integer(flags)) => *flags,
            _ => 0,
        };
        if drawing && flags & HIDDEN == 0 {
            plan.drawn.push(item.clone());
        }
    }
    Ok(changed.then_some(plan))
}

/// The appearances that a render synthesizes for annotations stored
/// without one, as [`synthesized`](crate::annotations::synthesized) lists
/// them; flattening writes each form the first time that it draws it.
#[derive(Debug, Default)]
pub struct Missing {
    /// The annotation dictionary with its fitted `/Rect`, and its form, by
    /// the annotation's reference.
    forms: HashMap<Reference, (Dict, Stream)>,
    /// The forms written, by the annotation's reference.
    written: HashMap<Reference, Reference>,
}

impl Missing {
    /// Returns the appearances of the objects that
    /// [`synthesized`](crate::annotations::synthesized) returns, charging
    /// the maps that hold them to `budget`.
    pub fn new(objects: Vec<(Reference, Value)>, budget: &Budget) -> Result<Self> {
        let entry = size_of::<(Reference, (Dict, Stream))>() + size_of::<(Reference, Reference)>();
        budget.charge(LimitCode::MemorySize, (objects.len() * entry) as u64)?;
        let mut streams = HashMap::new();
        let mut dicts = Vec::new();
        for (reference, value) in objects {
            match value {
                Value::Stream(stream) => {
                    streams.insert(reference, *stream);
                }
                Value::Dict(dict) => dicts.push((reference, dict)),
                _ => {}
            }
        }
        let mut forms = HashMap::new();
        for (reference, dict) in dicts {
            let form = match dict.get("AP") {
                Some(Value::Dict(appearances)) => match appearances.get("N") {
                    Some(Value::Reference(form)) => streams.remove(form),
                    _ => None,
                },
                _ => None,
            };
            if let Some(form) = form {
                forms.insert(reference, (dict, form));
            }
        }
        Ok(Self {
            forms,
            written: HashMap::new(),
        })
    }

    /// Returns the form of annotation `annotation`, writing it the first
    /// time.
    fn write(
        &mut self,
        transaction: &mut Transaction,
        annotation: Reference,
        form: Stream,
        budget: &Budget,
    ) -> Result<Reference> {
        if let Some(&reference) = self.written.get(&annotation) {
            return Ok(reference);
        }
        let reference = transaction.allocate()?;
        transaction.write(reference.number(), &Value::from(form), budget)?;
        self.written.insert(annotation, reference);
        Ok(reference)
    }
}

/// Where one appearance comes from.
enum Source {
    /// The stream object of an annotation's appearance, with the stream to
    /// write again when it lacks `/Subtype`.
    Stream(Reference, Option<Stream>),
    /// The form that a render synthesizes for the annotation.
    Missing(Reference, Stream),
}

/// Draws the appearances of `plan` into the content of page object
/// `number`, `page`, through one form, and leaves in its `/Annots` only the
/// entries that stay; those that are dictionaries are stored as objects, so
/// that a handle to one that moved goes stale rather than naming another.
/// A page without entries left loses `/Annots`.
pub fn draw(
    transaction: &mut Transaction,
    number: u32,
    page: &Page,
    plan: Plan,
    missing: &mut Missing,
    budget: &Budget,
) -> Result<()> {
    let mut placed = Vec::with_capacity(plan.drawn.len());
    {
        let reader = Reader {
            version: transaction.version(),
            budget,
        };
        for item in &plan.drawn {
            if let Value::Reference(reference) = item
                && let Some((dict, form)) = missing.forms.get(reference)
            {
                if let Some(placement) = placement(&reader, dict, form.dict())? {
                    placed.push((Source::Missing(*reference, form.clone()), placement));
                }
                continue;
            }
            let resolved = reader.resolve(item)?;
            let Value::Dict(annotation) = &*resolved else {
                continue;
            };
            let Some((reference, stream)) = normal_appearance(&reader, annotation)? else {
                continue;
            };
            let Value::Stream(stream) = &*stream else {
                continue;
            };
            let Some(placement) = placement(&reader, annotation, stream.dict())? else {
                continue;
            };
            // `Do` draws only forms; an appearance stream without
            // `/Subtype` is one by its role.
            let retyped = stream.dict().get("Subtype").is_none().then(|| {
                let mut stream = (**stream).clone();
                stream
                    .dict_mut()
                    .insert(Name::from("Subtype"), Value::Name(Name::from("Form")));
                stream
            });
            placed.push((Source::Stream(reference, retyped), placement));
        }
    }
    let mut dict = page.dict().clone();
    let mut xobjects = Dict::new();
    let mut ops = Vec::new();
    let mut bounds: Option<[f64; 4]> = None;
    for (index, (source, (matrix, rect))) in placed.into_iter().enumerate() {
        let reference = match source {
            Source::Stream(reference, None) => reference,
            Source::Stream(reference, Some(stream)) => {
                transaction.write(reference.number(), &Value::from(stream), budget)?;
                reference
            }
            Source::Missing(annotation, form) => {
                missing.write(transaction, annotation, form, budget)?
            }
        };
        let name = format!("A{index}");
        let [a, b, c, d, e, f] = matrix.map(fmt);
        ops.extend_from_slice(format!("q {a} {b} {c} {d} {e} {f} cm /{name} Do Q\n").as_bytes());
        xobjects.insert(Name::from(name.as_str()), Value::Reference(reference));
        bounds = Some(match bounds {
            None => rect,
            Some(b) => [
                b[0].min(rect[0]),
                b[1].min(rect[1]),
                b[2].max(rect[2]),
                b[3].max(rect[3]),
            ],
        });
    }
    if let Some(bounds) = bounds {
        let mut resources = Dict::new();
        resources.insert(Name::from("XObject"), Value::Dict(xobjects));
        let mut form = Dict::new();
        form.insert(Name::from("Type"), Value::Name(Name::from("XObject")));
        form.insert(Name::from("Subtype"), Value::Name(Name::from("Form")));
        form.insert(Name::from("BBox"), numbers_array(&bounds));
        form.insert(Name::from("Resources"), Value::Dict(resources));
        budget.charge(LimitCode::MemorySize, ops.len() as u64)?;
        let form = add_stream(transaction, form, ops, budget)?;
        let reader = Reader {
            version: transaction.version(),
            budget,
        };
        let (resources, name) = with_form(&reader, page.resources(), form)?;
        let contents = contents(&reader, dict.get("Contents"))?;
        let mut array = Array::new();
        if !contents.is_empty() {
            array.push(add_stream(
                transaction,
                Dict::new(),
                b"q\n".to_vec(),
                budget,
            )?);
            for content in contents {
                array.push(content);
            }
            array.push(add_stream(
                transaction,
                Dict::new(),
                b"\nQ\n".to_vec(),
                budget,
            )?);
        }
        let drawing = format!("q /{name} Do Q\n").into_bytes();
        array.push(add_stream(transaction, Dict::new(), drawing, budget)?);
        dict.insert(Name::from("Contents"), Value::Array(array));
        dict.insert(Name::from("Resources"), Value::Dict(resources));
    }
    let mut keep = Array::new();
    for item in plan.keep {
        keep.push(match item {
            Value::Dict(entry) => {
                let reference = transaction.allocate()?;
                transaction.write(reference.number(), &Value::Dict(entry), budget)?;
                Value::Reference(reference)
            }
            item => item,
        });
    }
    if keep.is_empty() {
        dict.remove("Annots");
    } else {
        dict.insert(Name::from("Annots"), Value::Array(keep));
    }
    transaction.write(number, &Value::Dict(dict), budget)?;
    Ok(())
}

/// Writes a stream of `dict` and `data` as a new object and returns a
/// reference to it.
fn add_stream(
    transaction: &mut Transaction,
    dict: Dict,
    data: Vec<u8>,
    budget: &Budget,
) -> Result<Value> {
    let reference = transaction.allocate()?;
    let stream = Stream::new(dict, Bytes::from(data));
    transaction.write(reference.number(), &Value::from(stream), budget)?;
    Ok(Value::Reference(reference))
}

/// Returns the page resources `resources` with the form `form` added under
/// a name that they do not use yet, and that name.
fn with_form(
    reader: &Reader<'_>,
    resources: Option<&Value>,
    form: Value,
) -> Result<(Dict, String)> {
    let mut resources = match resources {
        Some(resources) => match &*reader.resolve(resources)? {
            Value::Dict(resources) => resources.clone(),
            _ => Dict::new(),
        },
        None => Dict::new(),
    };
    let mut xobjects = match reader.entry(&resources, "XObject")?.as_deref() {
        Some(Value::Dict(xobjects)) => xobjects.clone(),
        _ => Dict::new(),
    };
    let name = (0..=xobjects.len())
        .map(|n| {
            if n == 0 {
                RESOURCE.to_owned()
            } else {
                format!("{RESOURCE}{n}")
            }
        })
        .find(|name| xobjects.get(name).is_none())
        .expect("one of more names than forms is free");
    xobjects.insert(Name::from(name.as_str()), form);
    resources.insert(Name::from("XObject"), Value::Dict(xobjects));
    Ok((resources, name))
}

/// Returns the content streams of a page's `/Contents` entry `value`.
fn contents(reader: &Reader<'_>, value: Option<&Value>) -> Result<Vec<Value>> {
    Ok(match value {
        Some(Value::Array(items)) => items.iter().cloned().collect(),
        Some(value @ Value::Reference(_)) => match &*reader.resolve(value)? {
            Value::Array(items) => items.iter().cloned().collect(),
            Value::Stream(_) => vec![value.clone()],
            _ => Vec::new(),
        },
        _ => Vec::new(),
    })
}

/// Returns the normal appearance that a render draws for `annotation`:
/// `/AP /N` itself, or the state of it that `/AS` names, else `Off`, with
/// the reference that names its stream.
fn normal_appearance(
    reader: &Reader<'_>,
    annotation: &Dict,
) -> Result<Option<(Reference, Arc<Value>)>> {
    let Some(appearances) = reader.entry(annotation, "AP")? else {
        return Ok(None);
    };
    let Value::Dict(appearances) = &*appearances else {
        return Ok(None);
    };
    let Some(normal) = appearances.get("N") else {
        return Ok(None);
    };
    if let Some(found) = stream_object(reader, normal)? {
        return Ok(Some(found));
    }
    let resolved = reader.resolve(normal)?;
    let Value::Dict(states) = &*resolved else {
        return Ok(None);
    };
    if let Some(Value::Name(state)) = reader.entry(annotation, "AS")?.as_deref()
        && let Some(stream) = states.get(state.as_bytes())
        && let Some(found) = stream_object(reader, stream)?
    {
        return Ok(Some(found));
    }
    match states.get("Off") {
        Some(stream) => stream_object(reader, stream),
        None => Ok(None),
    }
}

/// Returns the stream that `value` names through at most [`MAX_CHAIN`]
/// references, with the last of them, or `None` for anything else.
fn stream_object(reader: &Reader<'_>, value: &Value) -> Result<Option<(Reference, Arc<Value>)>> {
    let &Value::Reference(mut reference) = value else {
        return Ok(None);
    };
    for _ in 0..MAX_CHAIN {
        let Some(object) = reader
            .version
            .get(reference.number(), reader.budget)?
            .filter(|object| object.generation() == reference.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Stream(_) => return Ok(Some((reference, Arc::clone(object.value())))),
            Value::Reference(next) => reference = *next,
            _ => return Ok(None),
        }
    }
    Ok(None)
}

/// Returns `N` finite numbers of the entry `key` of `dict`.
fn numbers<const N: usize>(
    reader: &Reader<'_>,
    dict: &Dict,
    key: &str,
) -> Result<Option<[f64; N]>> {
    let Some(value) = dict.get(key) else {
        return Ok(None);
    };
    Ok(reader
        .numbers_of(value, N)?
        .and_then(|numbers| <[f64; N]>::try_from(numbers.into_vec()).ok()))
}

/// Returns the matrix that maps the appearance form of `annotation`, whose
/// dictionary is `form`, onto the annotation's rectangle (ISO 32000-2
/// §12.5.5), and that rectangle, or `None` when either box is unusable.
fn placement(
    reader: &Reader<'_>,
    annotation: &Dict,
    form: &Dict,
) -> Result<Option<([f64; 6], [f64; 4])>> {
    let (Some(bbox), Some(rect)) = (
        numbers::<4>(reader, form, "BBox")?,
        numbers::<4>(reader, annotation, "Rect")?,
    ) else {
        return Ok(None);
    };
    let rect = [
        rect[0].min(rect[2]),
        rect[1].min(rect[3]),
        rect[0].max(rect[2]),
        rect[1].max(rect[3]),
    ];
    let [a, b, c, d, e, f] =
        numbers::<6>(reader, form, "Matrix")?.unwrap_or([1.0, 0.0, 0.0, 1.0, 0.0, 0.0]);
    let corners = [
        (bbox[0], bbox[1]),
        (bbox[2], bbox[1]),
        (bbox[2], bbox[3]),
        (bbox[0], bbox[3]),
    ]
    .map(|(x, y)| (a * x + c * y + e, b * x + d * y + f));
    let (x0, y0, x1, y1) = corners.iter().fold(
        (
            f64::INFINITY,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::NEG_INFINITY,
        ),
        |(x0, y0, x1, y1), &(x, y)| (x0.min(x), y0.min(y), x1.max(x), y1.max(y)),
    );
    let (width, height) = (x1 - x0, y1 - y0);
    if !(width > 0.0 && height > 0.0) {
        return Ok(None);
    }
    let (sx, sy) = ((rect[2] - rect[0]) / width, (rect[3] - rect[1]) / height);
    let matrix = [sx, 0.0, 0.0, sy, rect[0] - sx * x0, rect[1] - sy * y0];
    Ok(matrix
        .iter()
        .all(|value| value.is_finite())
        .then_some((matrix, rect)))
}
