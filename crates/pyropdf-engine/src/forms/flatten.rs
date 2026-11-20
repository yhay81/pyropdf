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
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, try_insert_map};
use pyropdf_store::pages::{MAX_CHAIN, Page};
use pyropdf_store::refs::{copy_dict_for_edit, copy_for_edit};
use pyropdf_store::tree::{append_parent_tree_entry, number_tree_pairs};
use pyropdf_store::value::{Array, Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::annotations::version::{DEFAULT_VERSION, PdfVersion, header_version, parse_version};
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
    /// Every indirect annotation or widget taken off, by reference.
    removed: Vec<Reference>,
    /// The widgets taken off, by reference.
    pub widgets: Vec<Reference>,
}

/// Returns what flattening `selection` does with the `/Annots` entries
/// `items` of a page, or `None` when it takes none of them off. The plan's
/// lists and copied values are charged to `reader.budget`.
pub fn plan(reader: &Reader<'_>, items: &Array, selection: Selection) -> Result<Option<Plan>> {
    let budget = reader.budget;
    let mut keep = BudgetVec::new(budget);
    let mut drawn = BudgetVec::new(budget);
    let mut removed = BudgetVec::new(budget);
    let mut widgets = BudgetVec::new(budget);
    keep.try_reserve_exact(items.len())?;
    drawn.try_reserve_exact(items.len())?;
    removed.try_reserve_exact(items.len())?;
    widgets.try_reserve_exact(items.len())?;
    let mut changed = false;
    let mut drawing = true;
    for item in items.iter() {
        budget.poll()?;
        let resolved = reader.resolve(item)?;
        let Value::Dict(annotation) = &*resolved else {
            drawing = false;
            keep.try_push(copy_for_edit(item, budget)?)?;
            continue;
        };
        let subtype = reader.entry(annotation, "Subtype")?;
        let subtype = match subtype.as_deref() {
            Some(Value::Name(name)) => Some(name.as_bytes()),
            _ => None,
        };
        let selected = match subtype {
            Some(b"Link") => false,
            Some(b"Widget") => selection.widgets,
            _ => selection.annotations,
        };
        if !selected {
            keep.try_push(copy_for_edit(item, budget)?)?;
            continue;
        }
        changed = true;
        if let Value::Reference(reference) = item {
            removed.try_push(*reference)?;
            if subtype == Some(b"Widget") {
                widgets.try_push(*reference)?;
            }
        }
        let flags = match reader.entry(annotation, "F")?.as_deref() {
            Some(Value::Integer(flags)) => *flags,
            _ => 0,
        };
        if drawing && flags & HIDDEN == 0 {
            drawn.try_push(copy_for_edit(item, budget)?)?;
        }
    }
    Ok(changed.then(|| Plan {
        keep: keep.into_vec(),
        drawn: drawn.into_vec(),
        removed: removed.into_vec(),
        widgets: widgets.into_vec(),
    }))
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
        let mut streams = HashMap::new();
        let mut dicts = BudgetVec::new(budget);
        dicts.try_reserve_exact(objects.len())?;
        for (reference, value) in objects {
            match value {
                Value::Stream(stream) => {
                    try_insert_map(&mut streams, reference, *stream, budget)?;
                }
                Value::Dict(dict) => dicts.try_push((reference, dict))?,
                _ => {}
            }
        }
        let mut forms = HashMap::new();
        for (reference, dict) in dicts.into_vec() {
            let form = match dict.get("AP") {
                Some(Value::Dict(appearances)) => match appearances.get("N") {
                    Some(Value::Reference(form)) => streams.remove(form),
                    _ => None,
                },
                _ => None,
            };
            if let Some(form) = form {
                try_insert_map(&mut forms, reference, (dict, form), budget)?;
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
        try_insert_map(&mut self.written, annotation, reference, budget)?;
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

/// Structure-tree association of a flattened annotation (`StructElem`
/// reference and its `/S` role bytes).
type StructureTag = (Reference, Reference, Vec<u8>);

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
    let page_ref = page.object().unwrap_or_else(|| Reference::new(number, 0));
    let (tagged_doc, struct_parents) = structure_context(transaction.version(), &plan, budget)?;
    let mut placed = BudgetVec::new(budget);
    placed.try_reserve_exact(plan.drawn.len())?;
    let mut drawn_refs = BudgetVec::new(budget);
    drawn_refs.try_reserve_exact(plan.drawn.len())?;
    {
        let reader = Reader::new(transaction.version(), budget);
        for item in &plan.drawn {
            let item_ref = match item {
                Value::Reference(reference) => Some(*reference),
                _ => None,
            };
            if let Some(reference) = item_ref
                && let Some((dict, form)) = missing.forms.get(&reference)
            {
                if let Some(placement) = placement(&reader, dict, form.dict())? {
                    let tag = resolve_structure_tag(&reader, &struct_parents, reference, dict)?;
                    let copied = Stream::new(
                        copy_dict_for_edit(form.dict(), budget)?,
                        form.data().clone(),
                    );
                    drawn_refs.try_push(reference)?;
                    placed.try_push((Source::Missing(reference, copied), placement, tag))?;
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
            let tag = match item_ref {
                Some(annot_ref) => {
                    drawn_refs.try_push(annot_ref)?;
                    resolve_structure_tag(&reader, &struct_parents, annot_ref, annotation)?
                }
                None => None,
            };
            // `Do` draws only forms; an appearance stream without
            // `/Subtype` is one by its role.
            let retyped = if stream.dict().get("Subtype").is_none() {
                let mut dict = copy_dict_for_edit(stream.dict(), budget)?;
                #[expect(
                    clippy::disallowed_methods,
                    reason = "the stream grows by one entry at most"
                )]
                dict.insert(Name::from("Subtype"), Value::Name(Name::from("Form")));
                Some(Stream::new(dict, stream.data().clone()))
            } else {
                None
            };
            placed.try_push((Source::Stream(reference, retyped), placement, tag))?;
        }
    }
    let mut dict = copy_dict_for_edit(page.dict(), budget)?;
    let mut xobjects = Dict::new();
    let mut ops = BudgetVec::new(budget);
    let mut bounds: Option<[f64; 4]> = None;
    let mut mcid_owners = BudgetVec::new(budget);
    let mut rewrites: BudgetVec<'_, (Reference, Reference, Option<i64>)> = BudgetVec::new(budget);
    for (index, (source, (matrix, rect), tag)) in placed.into_vec().into_iter().enumerate() {
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
        let invoke = format!("q {a} {b} {c} {d} {e} {f} cm /{name} Do Q\n");
        if let Some((annot_ref, elem_ref, role)) = tag {
            let mcid = i64::try_from(mcid_owners.len()).unwrap_or(i64::MAX);
            mcid_owners.try_push(Value::Reference(elem_ref))?;
            rewrites.try_push((elem_ref, annot_ref, Some(mcid)))?;
            write_pdf_name(&mut ops, &role)?;
            ops.try_extend_from_slice(format!(" << /MCID {mcid} >> BDC\n").as_bytes())?;
            ops.try_extend_from_slice(invoke.as_bytes())?;
            ops.try_extend_from_slice(b"EMC\n")?;
        } else if tagged_doc {
            ops.try_extend_from_slice(b"/Artifact BMC\n")?;
            ops.try_extend_from_slice(invoke.as_bytes())?;
            ops.try_extend_from_slice(b"EMC\n")?;
        } else {
            ops.try_extend_from_slice(invoke.as_bytes())?;
        }
        budget.charge(
            LimitCode::MemorySize,
            (size_of::<(Name, Value)>() + name.len()) as u64,
        )?;
        #[expect(
            clippy::disallowed_methods,
            reason = "one entry per appearance drawn, charged to the budget above"
        )]
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
    if !struct_parents.is_empty() {
        let reader = Reader::new(transaction.version(), budget);
        for &removed_ref in &plan.removed {
            if drawn_refs.as_slice().contains(&removed_ref) {
                continue;
            }
            let removed_val = Value::Reference(removed_ref);
            let resolved = reader.resolve(&removed_val)?;
            if let Value::Dict(annotation) = &*resolved
                && let Some((_, elem_ref, _)) =
                    resolve_structure_tag(&reader, &struct_parents, removed_ref, annotation)?
            {
                rewrites.try_push((elem_ref, removed_ref, None))?;
            }
        }
    }
    let mut form_reference = None;
    if let Some(bounds) = bounds {
        let form_struct_parents = if mcid_owners.is_empty() {
            None
        } else {
            Some(append_parent_tree_entry(
                transaction,
                Value::Array(Array::from(mcid_owners.into_vec())),
                budget,
            )?)
        };
        let allocated = add_stream_ref(
            transaction,
            drawing_form(xobjects, &bounds, form_struct_parents),
            ops.into_vec(),
            budget,
        )?;
        form_reference = Some(allocated);
        let reader = Reader::new(transaction.version(), budget);
        let (resources, name) = with_form(&reader, page.resources(), Value::Reference(allocated))?;
        let contents = contents(&reader, budget, dict.get("Contents"))?;
        let mut array = BudgetVec::new(budget);
        if !contents.is_empty() {
            array.try_reserve_exact(contents.len() + 3)?;
            #[expect(clippy::disallowed_methods, reason = "a constant of two bytes")]
            array.try_push(add_stream(
                transaction,
                Dict::new(),
                b"q\n".to_vec(),
                budget,
            )?)?;
            array.try_extend_from_slice(&contents)?;
            #[expect(clippy::disallowed_methods, reason = "a constant of three bytes")]
            array.try_push(add_stream(
                transaction,
                Dict::new(),
                b"\nQ\n".to_vec(),
                budget,
            )?)?;
        }
        let drawing = format!("q /{name} Do Q\n").into_bytes();
        array.try_push(add_stream(transaction, Dict::new(), drawing, budget)?)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "the page grows by one entry at most"
        )]
        dict.insert(
            Name::from("Contents"),
            Value::Array(Array::from(array.into_vec())),
        );
        #[expect(
            clippy::disallowed_methods,
            reason = "the page grows by one entry at most"
        )]
        dict.insert(Name::from("Resources"), Value::Dict(resources));
    }
    if !rewrites.is_empty() {
        rewrite_structure_elements(
            transaction,
            page_ref,
            form_reference,
            rewrites.as_slice(),
            budget,
        )?;
    }
    let mut keep = BudgetVec::new(budget);
    keep.try_reserve_exact(plan.keep.len())?;
    for item in plan.keep {
        keep.try_push(match item {
            Value::Dict(entry) => {
                let reference = transaction.allocate()?;
                transaction.write(reference.number(), &Value::Dict(entry), budget)?;
                Value::Reference(reference)
            }
            item => item,
        })?;
    }
    if keep.is_empty() {
        dict.remove("Annots");
    } else {
        #[expect(
            clippy::disallowed_methods,
            reason = "the page grows by one entry at most"
        )]
        dict.insert(
            Name::from("Annots"),
            Value::Array(Array::from(keep.into_vec())),
        );
    }
    transaction.write(number, &Value::Dict(dict), budget)?;
    Ok(())
}

/// Returns the dictionary of the form that draws a page's flattened
/// appearances, `xobjects`, within `bounds`.
#[expect(
    clippy::disallowed_methods,
    reason = "each key is a constant, so the form holds five entries at most and its resources one"
)]
fn drawing_form(xobjects: Dict, bounds: &[f64; 4], struct_parents: Option<i64>) -> Dict {
    let mut resources = Dict::new();
    resources.insert(Name::from("XObject"), Value::Dict(xobjects));
    let mut form = Dict::new();
    form.insert(Name::from("Type"), Value::Name(Name::from("XObject")));
    form.insert(Name::from("Subtype"), Value::Name(Name::from("Form")));
    form.insert(Name::from("BBox"), numbers_array(bounds));
    form.insert(Name::from("Resources"), Value::Dict(resources));
    if let Some(key) = struct_parents {
        form.insert(Name::from("StructParents"), Value::Integer(key));
    }
    form
}

/// Writes a stream of `dict` and `data` as a new object and returns its
/// reference.
fn add_stream_ref(
    transaction: &mut Transaction,
    dict: Dict,
    data: Vec<u8>,
    budget: &Budget,
) -> Result<Reference> {
    let reference = transaction.allocate()?;
    let stream = Stream::new(dict, Bytes::from(data));
    transaction.write(reference.number(), &Value::from(stream), budget)?;
    Ok(reference)
}

/// Writes a stream of `dict` and `data` as a new object and returns a
/// reference to it.
fn add_stream(
    transaction: &mut Transaction,
    dict: Dict,
    data: Vec<u8>,
    budget: &Budget,
) -> Result<Value> {
    Ok(Value::Reference(add_stream_ref(
        transaction,
        dict,
        data,
        budget,
    )?))
}

/// Returns the page resources `resources` with the form `form` added under
/// a name that they do not use yet, and that name.
#[expect(
    clippy::disallowed_methods,
    reason = "the resources and their forms grow by one entry each at most"
)]
fn with_form(
    reader: &Reader<'_>,
    resources: Option<&Value>,
    form: Value,
) -> Result<(Dict, String)> {
    let mut resources = match resources {
        Some(resources) => match &*reader.resolve(resources)? {
            Value::Dict(resources) => copy_dict_for_edit(resources, reader.budget)?,
            _ => Dict::new(),
        },
        None => Dict::new(),
    };
    let mut xobjects = match reader.entry(&resources, "XObject")?.as_deref() {
        Some(Value::Dict(xobjects)) => copy_dict_for_edit(xobjects, reader.budget)?,
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

/// Returns the content streams of a page's `/Contents` entry `value`,
/// grown through `budget`.
fn contents<'b>(
    reader: &Reader<'_>,
    budget: &'b Budget,
    value: Option<&Value>,
) -> Result<BudgetVec<'b, Value>> {
    let mut contents = BudgetVec::new(budget);
    let push_items = |contents: &mut BudgetVec<'b, Value>, items: &Array| -> Result<()> {
        contents.try_reserve_exact(items.len())?;
        for item in items.iter() {
            contents.try_push(copy_for_edit(item, budget)?)?;
        }
        Ok(())
    };
    match value {
        Some(Value::Array(items)) => push_items(&mut contents, items)?,
        Some(value @ Value::Reference(_)) => match &*reader.resolve(value)? {
            Value::Array(items) => push_items(&mut contents, items)?,
            Value::Stream(_) => contents.try_push(copy_for_edit(value, budget)?)?,
            _ => {}
        },
        _ => {}
    }
    Ok(contents)
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

/// Returns `(tagged_doc, struct_parents)` for `version`: whether the document
/// has a structure tree or `/MarkInfo << /Marked true >>`, and—when at least
/// one flattened/removed annotation carries `/StructParent`—the mapping from
/// `/StructParent` integer keys to `StructElem` references in `/ParentTree`.
fn structure_context(
    version: &Version,
    plan: &Plan,
    budget: &Budget,
) -> Result<(bool, HashMap<i64, Reference>)> {
    let reader = Reader::new(version, budget);
    let Some(root) = version.trailer().get("Root") else {
        return Ok((false, HashMap::new()));
    };
    let catalog_resolved = reader.resolve(root)?;
    let Value::Dict(catalog) = &*catalog_resolved else {
        return Ok((false, HashMap::new()));
    };
    let struct_root = reader.entry(catalog, "StructTreeRoot")?;
    let has_struct_tree = matches!(struct_root.as_deref(), Some(Value::Dict(_)));
    let is_marked = matches!(
        reader.entry(catalog, "MarkInfo")?.as_deref(),
        Some(Value::Dict(mark_info)) if mark_info.get("Marked") == Some(&Value::Boolean(true))
    );
    let tagged_doc = has_struct_tree || is_marked;
    let mut map = HashMap::new();
    let Some(Value::Dict(struct_root)) = struct_root.as_deref() else {
        return Ok((tagged_doc, map));
    };
    let mut any_struct_parent = false;
    for &reference in &plan.removed {
        let ref_val = Value::Reference(reference);
        let resolved = reader.resolve(&ref_val)?;
        if let Value::Dict(annot) = &*resolved
            && matches!(reader.entry(annot, "StructParent")?.as_deref(), Some(Value::Integer(key)) if *key >= 0)
        {
            any_struct_parent = true;
            break;
        }
    }
    if !any_struct_parent {
        return Ok((tagged_doc, map));
    }
    let Some(parent_tree) = struct_root.get("ParentTree") else {
        return Ok((tagged_doc, map));
    };
    let mut pairs = BudgetVec::new(budget);
    number_tree_pairs(version, parent_tree, &mut pairs, budget)?;
    for (key, value) in pairs.into_vec() {
        if let Value::Reference(elem_ref) = value {
            try_insert_map(&mut map, key, elem_ref, budget)?;
        }
    }
    Ok((tagged_doc, map))
}

/// Resolves the `StructElem` reference and `/S` role name of `annot_ref` when
/// `annotation`'s `/StructParent` names a `StructElem` in `struct_parents`
/// whose `/K` holds an `/OBJR` to `annot_ref`.
fn resolve_structure_tag(
    reader: &Reader<'_>,
    struct_parents: &HashMap<i64, Reference>,
    annot_ref: Reference,
    annotation: &Dict,
) -> Result<Option<StructureTag>> {
    if struct_parents.is_empty() {
        return Ok(None);
    }
    let sp_entry = reader.entry(annotation, "StructParent")?;
    let Some(Value::Integer(key)) = sp_entry.as_deref() else {
        return Ok(None);
    };
    let Some(&elem_ref) = struct_parents.get(key) else {
        return Ok(None);
    };
    let elem_ref_val = Value::Reference(elem_ref);
    let elem_val = reader.resolve(&elem_ref_val)?;
    let Value::Dict(elem_dict) = &*elem_val else {
        return Ok(None);
    };
    let role_entry = reader.entry(elem_dict, "S")?;
    let Some(Value::Name(role)) = role_entry.as_deref() else {
        return Ok(None);
    };
    if !elem_has_objr_for(reader, elem_dict, annot_ref)? {
        return Ok(None);
    }
    let mut role_bytes = BudgetVec::new(reader.budget);
    role_bytes.try_extend_from_slice(role.as_bytes())?;
    Ok(Some((annot_ref, elem_ref, role_bytes.into_vec())))
}

fn is_objr_for(reader: &Reader<'_>, item: &Value, annot_ref: Reference) -> Result<bool> {
    let resolved = reader.resolve(item)?;
    let Value::Dict(dict) = &*resolved else {
        return Ok(false);
    };
    let is_objr = matches!(
        reader.entry(dict, "Type")?.as_deref(),
        Some(Value::Name(kind)) if kind.as_bytes() == b"OBJR"
    );
    Ok(is_objr && dict.get("Obj") == Some(&Value::Reference(annot_ref)))
}

fn elem_has_objr_for(reader: &Reader<'_>, elem: &Dict, annot_ref: Reference) -> Result<bool> {
    let Some(kids) = elem.get("K") else {
        return Ok(false);
    };
    if is_objr_for(reader, kids, annot_ref)? {
        return Ok(true);
    }
    let resolved = reader.resolve(kids)?;
    let Value::Array(items) = &*resolved else {
        return Ok(false);
    };
    for item in items.iter() {
        if is_objr_for(reader, item, annot_ref)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Writes `bytes` as a PDF `/Name` token onto `out`, escaping whitespace,
/// delimiters, `#`, and non-ASCII bytes as `#XX`.
fn write_pdf_name(out: &mut BudgetVec<'_, u8>, bytes: &[u8]) -> Result<()> {
    out.try_push(b'/')?;
    for &byte in bytes {
        let needs_escape = !(0x21..=0x7e).contains(&byte)
            || matches!(
                byte,
                b'#' | b'(' | b')' | b'<' | b'>' | b'[' | b']' | b'{' | b'}' | b'/' | b'%'
            );
        if needs_escape {
            let hex = format!("#{byte:02X}");
            out.try_extend_from_slice(hex.as_bytes())?;
        } else {
            out.try_push(byte)?;
        }
    }
    Ok(())
}

#[expect(
    clippy::disallowed_methods,
    reason = "MCR dictionary holds four fixed keys"
)]
fn make_mcr(page_ref: Reference, form_ref: Reference, mcid: i64) -> Value {
    let mut mcr = Dict::new();
    mcr.insert(Name::from("Type"), Value::Name(Name::from("MCR")));
    mcr.insert(Name::from("Pg"), Value::Reference(page_ref));
    mcr.insert(Name::from("Stm"), Value::Reference(form_ref));
    mcr.insert(Name::from("MCID"), Value::Integer(mcid));
    Value::Dict(mcr)
}

/// Replaces `/OBJR` entries of flattened/removed annotations in their owning
/// `StructElem` dictionaries with `/MCR` dictionaries pointing into `form_ref`
/// (or removes them when the annotation was hidden and undrawn), and supplies
/// `/O /PrintField /Role` on PDF 1.7+ `Form` structure elements that no longer
/// have an interactive widget child (ISO 32000-1 Table 348, PDF/UA-1 §7.18.4).
fn rewrite_structure_elements(
    transaction: &mut Transaction,
    page_ref: Reference,
    form_ref: Option<Reference>,
    rewrites: &[(Reference, Reference, Option<i64>)],
    budget: &Budget,
) -> Result<()> {
    let mut elem_refs = BudgetVec::new(budget);
    for &(elem_ref, _, _) in rewrites {
        if !elem_refs.as_slice().contains(&elem_ref) {
            elem_refs.try_push(elem_ref)?;
        }
    }
    let allow_print_field = {
        let reader = Reader::new(transaction.version(), budget);
        pdf_version(transaction.version(), &reader)? >= PdfVersion(1, 7)
    };
    for elem_ref in elem_refs.into_vec() {
        let elem_dict = {
            let reader = Reader::new(transaction.version(), budget);
            let elem_ref_val = Value::Reference(elem_ref);
            let resolved = reader.resolve(&elem_ref_val)?;
            let Value::Dict(dict) = &*resolved else {
                continue;
            };
            copy_dict_for_edit(dict, budget)?
        };
        let Some(old_k) = elem_dict.get("K").cloned() else {
            continue;
        };
        let (updated_k, print_role) = {
            let reader = Reader::new(transaction.version(), budget);
            let mut matched_single = None;
            let mut widget_role = None;
            for &(owner, annot_ref, mcid) in rewrites {
                if owner == elem_ref {
                    if widget_role.is_none() && allow_print_field {
                        widget_role = print_field_role(&reader, annot_ref)?;
                    }
                    if matched_single.is_none() && is_objr_for(&reader, &old_k, annot_ref)? {
                        matched_single = Some(mcid);
                    }
                }
            }
            let updated_k = if let Some(mcid) = matched_single {
                mcid.zip(form_ref)
                    .map(|(id, form)| make_mcr(page_ref, form, id))
            } else {
                let resolved_k = reader.resolve(&old_k)?;
                if let Value::Array(items) = &*resolved_k {
                    let mut new_items = BudgetVec::new(budget);
                    new_items.try_reserve_exact(items.len())?;
                    for item in items.iter() {
                        let mut matched = None;
                        for &(owner, annot_ref, mcid) in rewrites {
                            if owner == elem_ref && is_objr_for(&reader, item, annot_ref)? {
                                matched = Some(mcid);
                                break;
                            }
                        }
                        match matched {
                            Some(Some(id)) => {
                                if let Some(form) = form_ref {
                                    new_items.try_push(make_mcr(page_ref, form, id))?;
                                }
                            }
                            Some(None) => {}
                            None => new_items.try_push(copy_for_edit(item, budget)?)?,
                        }
                    }
                    Some(Value::Array(Array::from(new_items.into_vec())))
                } else {
                    Some(copy_for_edit(&old_k, budget)?)
                }
            };
            let need_role = if let Some(role) = widget_role
                && resolves_to_form_elem(transaction.version(), &reader, &elem_dict)?
                && !has_widget_objr(&reader, updated_k.as_ref())?
                && !has_print_field_role(&reader, elem_dict.get("A"))?
            {
                Some(role)
            } else {
                None
            };
            (updated_k, need_role)
        };
        let mut elem_dict = elem_dict;
        let elem_a_modified = if let Some(role) = print_role {
            let reader = Reader::new(transaction.version(), budget);
            attach_print_field_role(&reader, &mut elem_dict, role, budget)?;
            true
        } else {
            false
        };
        match updated_k {
            Some(Value::Reference(_)) => {
                if elem_a_modified {
                    transaction.write(elem_ref.number(), &Value::Dict(elem_dict), budget)?;
                }
            }
            Some(new_k) => {
                if let Value::Reference(k_ref) = old_k
                    && matches!(new_k, Value::Array(_))
                {
                    transaction.write(k_ref.number(), &new_k, budget)?;
                    if elem_a_modified {
                        transaction.write(elem_ref.number(), &Value::Dict(elem_dict), budget)?;
                    }
                } else {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "replaces the existing /K entry of the StructElem"
                    )]
                    elem_dict.insert(Name::from("K"), new_k);
                    transaction.write(elem_ref.number(), &Value::Dict(elem_dict), budget)?;
                }
            }
            None => {
                elem_dict.remove("K");
                transaction.write(elem_ref.number(), &Value::Dict(elem_dict), budget)?;
            }
        }
    }
    Ok(())
}

fn pdf_version(version: &Version, reader: &Reader<'_>) -> Result<PdfVersion> {
    let mut effective = header_version(version).unwrap_or(DEFAULT_VERSION);
    if let Some(root) = version.trailer().get("Root")
        && let Value::Dict(catalog) = &*reader.resolve(root)?
        && let Some(Value::Name(declared)) = reader.entry(catalog, "Version")?.as_deref()
        && let Some(parsed) = parse_version(declared.as_bytes())
    {
        effective = effective.max(parsed);
    }
    Ok(effective)
}

fn resolves_to_form_elem(version: &Version, reader: &Reader<'_>, elem_dict: &Dict) -> Result<bool> {
    let s_entry = reader.entry(elem_dict, "S")?;
    let Some(Value::Name(initial)) = s_entry.as_deref() else {
        return Ok(false);
    };
    if initial.as_bytes() == b"Form" {
        return Ok(true);
    }
    let Some(root) = version.trailer().get("Root") else {
        return Ok(false);
    };
    let catalog = reader.resolve(root)?;
    let Value::Dict(catalog) = &*catalog else {
        return Ok(false);
    };
    let struct_root = reader.entry(catalog, "StructTreeRoot")?;
    let Some(Value::Dict(struct_root)) = struct_root.as_deref() else {
        return Ok(false);
    };
    let role_map = reader.entry(struct_root, "RoleMap")?;
    let Some(Value::Dict(role_map)) = role_map.as_deref() else {
        return Ok(false);
    };
    let mut current = initial.clone();
    for _ in 0..MAX_CHAIN {
        let Some(entry) = role_map.get(current.as_bytes()) else {
            return Ok(false);
        };
        let mapped = reader.resolve(entry)?;
        let Value::Name(next) = &*mapped else {
            return Ok(false);
        };
        if next.as_bytes() == b"Form" {
            return Ok(true);
        }
        if next.as_bytes() == current.as_bytes() {
            return Ok(false);
        }
        current = next.clone();
    }
    Ok(false)
}

fn print_field_role(reader: &Reader<'_>, annot_ref: Reference) -> Result<Option<&'static str>> {
    let annot_val = Value::Reference(annot_ref);
    let resolved = reader.resolve(&annot_val)?;
    let Value::Dict(annot) = &*resolved else {
        return Ok(None);
    };
    let is_widget = matches!(
        reader.entry(annot, "Subtype")?.as_deref(),
        Some(Value::Name(kind)) if kind.as_bytes() == b"Widget"
    );
    if !is_widget {
        return Ok(None);
    }
    let mut is_btn = None;
    let mut flags = None;
    if let Some(Value::Name(ft)) = reader.entry(annot, "FT")?.as_deref() {
        is_btn = Some(ft.as_bytes() == b"Btn");
    }
    if let Some(Value::Integer(ff)) = reader.entry(annot, "Ff")?.as_deref() {
        flags = Some(*ff);
    }
    let mut next_parent = annot.get("Parent").cloned();
    for _ in 0..super::MAX_PARENTS {
        if is_btn.is_some() && flags.is_some() {
            break;
        }
        let Some(parent_val) = next_parent else {
            break;
        };
        let parent_resolved = reader.resolve(&parent_val)?;
        let Value::Dict(parent) = &*parent_resolved else {
            break;
        };
        if is_btn.is_none()
            && let Some(Value::Name(ft)) = reader.entry(parent, "FT")?.as_deref()
        {
            is_btn = Some(ft.as_bytes() == b"Btn");
        }
        if flags.is_none()
            && let Some(Value::Integer(ff)) = reader.entry(parent, "Ff")?.as_deref()
        {
            flags = Some(*ff);
        }
        next_parent = parent.get("Parent").cloned();
    }
    let ff = flags.unwrap_or(0);
    let role = match is_btn {
        Some(true) if (ff & (1 << 16)) != 0 => "pb",
        Some(true) if (ff & (1 << 15)) != 0 => "rb",
        Some(true) => "cb",
        _ => "tv",
    };
    Ok(Some(role))
}

fn is_widget_objr_item(reader: &Reader<'_>, item: &Value) -> Result<bool> {
    let resolved = reader.resolve(item)?;
    let Value::Dict(dict) = &*resolved else {
        return Ok(false);
    };
    let is_objr = matches!(
        reader.entry(dict, "Type")?.as_deref(),
        Some(Value::Name(kind)) if kind.as_bytes() == b"OBJR"
    );
    if !is_objr {
        return Ok(false);
    }
    let Some(obj) = dict.get("Obj") else {
        return Ok(false);
    };
    let target = reader.resolve(obj)?;
    let Value::Dict(annot) = &*target else {
        return Ok(false);
    };
    Ok(matches!(
        reader.entry(annot, "Subtype")?.as_deref(),
        Some(Value::Name(kind)) if kind.as_bytes() == b"Widget"
    ))
}

fn has_widget_objr(reader: &Reader<'_>, k: Option<&Value>) -> Result<bool> {
    let Some(k) = k else {
        return Ok(false);
    };
    if is_widget_objr_item(reader, k)? {
        return Ok(true);
    }
    let resolved = reader.resolve(k)?;
    let Value::Array(items) = &*resolved else {
        return Ok(false);
    };
    for item in items.iter() {
        if is_widget_objr_item(reader, item)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn is_print_field_role_dict(reader: &Reader<'_>, item: &Value) -> Result<bool> {
    let resolved = reader.resolve(item)?;
    let dict = match &*resolved {
        Value::Dict(dict) => dict,
        Value::Stream(stream) => stream.dict(),
        _ => return Ok(false),
    };
    let is_print_field = matches!(
        reader.entry(dict, "O")?.as_deref(),
        Some(Value::Name(owner)) if owner.as_bytes() == b"PrintField"
    );
    let has_role = matches!(reader.entry(dict, "Role")?.as_deref(), Some(Value::Name(_)));
    Ok(is_print_field && has_role)
}

fn has_print_field_role(reader: &Reader<'_>, attr: Option<&Value>) -> Result<bool> {
    let Some(attr) = attr else {
        return Ok(false);
    };
    if is_print_field_role_dict(reader, attr)? {
        return Ok(true);
    }
    let resolved = reader.resolve(attr)?;
    let Value::Array(items) = &*resolved else {
        return Ok(false);
    };
    for item in items.iter() {
        if is_print_field_role_dict(reader, item)? {
            return Ok(true);
        }
    }
    Ok(false)
}

#[expect(
    clippy::disallowed_methods,
    reason = "PrintField attribute dictionary holds two fixed keys"
)]
fn make_print_field_attr(role: &'static str) -> Value {
    let mut dict = Dict::new();
    dict.insert(Name::from("O"), Value::Name(Name::from("PrintField")));
    dict.insert(Name::from("Role"), Value::Name(Name::from(role)));
    Value::Dict(dict)
}

fn attach_print_field_role(
    reader: &Reader<'_>,
    elem_dict: &mut Dict,
    role: &'static str,
    budget: &Budget,
) -> Result<()> {
    let attr = make_print_field_attr(role);
    let new_a = match elem_dict.get("A") {
        None => attr,
        Some(existing) => {
            let resolved = reader.resolve(existing)?;
            if let Value::Array(items) = &*resolved {
                let mut merged = BudgetVec::new(budget);
                merged.try_reserve_exact(items.len() + 1)?;
                for item in items.iter() {
                    merged.try_push(copy_for_edit(item, budget)?)?;
                }
                merged.try_push(attr)?;
                Value::Array(Array::from(merged.into_vec()))
            } else {
                let mut pair = BudgetVec::new(budget);
                pair.try_reserve_exact(2)?;
                pair.try_push(copy_for_edit(existing, budget)?)?;
                pair.try_push(attr)?;
                Value::Array(Array::from(pair.into_vec()))
            }
        }
    };
    #[expect(
        clippy::disallowed_methods,
        reason = "sets or replaces the single /A entry on the StructElem"
    )]
    elem_dict.insert(Name::from("A"), new_a);
    Ok(())
}
