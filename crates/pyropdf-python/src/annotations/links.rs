//! Links: the link annotations of a page and where they lead (DESIGN.md
//! section 10).
//!
//! A link handle names its annotation as an annotation handle does. A link
//! leads to a destination of this document, directly or through a named
//! destination, or to an action. Named destinations resolve through an index
//! of the catalog's `/Names /Dests` tree and legacy `/Dests` dictionary,
//! memoized with what building it read (section 5).

use std::collections::HashSet;
use std::sync::Arc;

use pyo3::prelude::*;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::{MAX_CHAIN, PageIndex};
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};

use super::{
    Annots, MAX_ANNOTATION_TEXT_BYTES, MAX_PAGE_ANNOTATIONS, Rect, charge, charge_text, edited,
    encode_text, to_display, to_pdf,
};
use crate::document::{_Document, Failed, text_string};
use crate::errors::{LimitError, PdfError, StaleObjectError, store_error};
use crate::pages::{display_box, index, number, rotation};

/// The most entries, and tree nodes, of the named destinations.
const MAX_NAMED_ENTRIES: usize = 65_536;
/// The most edges of the `/Names /Dests` tree.
const MAX_NAMED_EDGES: usize = 131_072;
/// The most levels of the `/Names /Dests` tree.
const MAX_NAMED_DEPTH: usize = 32;
/// The bytes of all the names of the named destinations.
const MAX_NAMED_TEXT_BYTES: usize = 16 * 1024 * 1024;
/// The largest magnitude of a PDF real number, which readers hold as `f32`.
const MAX_REAL: f64 = f32::MAX as f64;

/// Where a link leads: its kind; the zero-based page of this document for
/// `"goto"`, or of another for `"gotor"`; a display point; a zoom; a URI or
/// a file; and a name. The kind `"none"` stands for an action that pyropdf
/// does not read, or a destination that names no page.
pub(crate) type Target = (
    String,
    Option<u32>,
    Option<(f64, f64)>,
    Option<f64>,
    Option<String>,
    Option<String>,
);

/// A link as a handle reads it: its position in the page's `/Annots`, its
/// object, `(0, 0)` for a direct dictionary, its display rectangle, and
/// where it leads.
type Record = (u32, (u32, u16), Rect, Target);

/// Where a destination leads: its zero-based page, display point, and zoom.
type Place = (u32, Option<(f64, f64)>, Option<f64>);

/// A named destination: its name, and where it leads, or `None` when it
/// names no page.
type Named = (String, Option<Place>);

pub(crate) fn none() -> Target {
    ("none".to_owned(), None, None, None, None, None)
}

/// The page of a destination before it is resolved.
#[derive(Clone, Copy, Debug)]
enum DestinationPage {
    Reference(Reference),
    /// Some producers write a zero-based page index instead of a reference.
    Index(i64),
    Missing,
}

/// The fits of a destination that carry a point.
#[derive(Clone, Copy, Debug)]
enum Fit {
    Xyz,
    /// `/FitH` or `/FitBH`, with a top coordinate.
    Horizontal,
    /// `/FitV` or `/FitBV`, with a left coordinate.
    Vertical,
    Other,
}

/// A destination array, reduced to what a target reports.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Destination {
    page: DestinationPage,
    fit: Fit,
    /// Array elements 2 to 4: the fit's coordinates and the zoom.
    numbers: [Option<f64>; 3],
}

impl Destination {
    pub(crate) const UNRESOLVED: Self = Self {
        page: DestinationPage::Missing,
        fit: Fit::Other,
        numbers: [None; 3],
    };

    /// Reduces a destination array, or a dictionary whose `/D` is one,
    /// resolving its elements with `resolve`.
    fn read(
        value: Option<Value>,
        resolve: &mut dyn FnMut(Option<&Value>) -> PyResult<Option<Value>>,
    ) -> PyResult<Self> {
        let value = match value {
            Some(Value::Dict(dict)) => resolve(dict.get("D"))?,
            value => value,
        };
        let Some(Value::Array(array)) = value else {
            return Ok(Self::UNRESOLVED);
        };
        let page = match array.get(0) {
            Some(Value::Reference(reference)) => DestinationPage::Reference(*reference),
            Some(Value::Integer(index)) => DestinationPage::Index(*index),
            _ => DestinationPage::Missing,
        };
        let fit = match resolve(array.get(1))? {
            Some(Value::Name(name)) => match name.as_bytes() {
                b"XYZ" => Fit::Xyz,
                b"FitH" | b"FitBH" => Fit::Horizontal,
                b"FitV" | b"FitBV" => Fit::Vertical,
                _ => Fit::Other,
            },
            _ => Fit::Other,
        };
        let mut numbers = [None; 3];
        for (slot, at) in numbers.iter_mut().zip(2..) {
            *slot = match resolve(array.get(at))? {
                Some(Value::Integer(value)) => Some(value as f64),
                Some(Value::Real(value)) => Some(value.value()),
                _ => None,
            };
        }
        Ok(Self { page, fit, numbers })
    }

    /// Returns the zero-based page, display point, and zoom that the
    /// destination names in `index`, or `None` when it names no page. A
    /// destination that leaves its coordinates null keeps the viewer's
    /// position, so it has no point.
    fn place(&self, index: &PageIndex) -> Option<Place> {
        let pages = index.pages();
        let at = match self.page {
            DestinationPage::Reference(reference) => index.index_of(reference)?,
            DestinationPage::Index(at) => {
                usize::try_from(at).ok().filter(|at| *at < pages.len())?
            }
            DestinationPage::Missing => return None,
        };
        let page = &pages[at];
        let crop = display_box(page);
        let turn = rotation(page.rotate());
        let [first, second, third] = self.numbers;
        let (point, zoom) = match self.fit {
            Fit::Xyz => (
                (first.is_some() || second.is_some()).then(|| {
                    to_display(
                        crop,
                        turn,
                        first.unwrap_or(crop[0]),
                        second.unwrap_or(crop[3]),
                    )
                }),
                third.filter(|zoom| *zoom != 0.0),
            ),
            Fit::Horizontal => (first.map(|top| to_display(crop, turn, crop[0], top)), None),
            Fit::Vertical => (
                first.map(|left| to_display(crop, turn, left, crop[3])),
                None,
            ),
            Fit::Other => (None, None),
        };
        Some((u32::try_from(at).ok()?, point, zoom))
    }
}

/// The named destinations of a version: the names of the `/Names /Dests`
/// tree, the first entry of a repeated name winning, then those of the
/// legacy `/Dests` dictionary that the tree lacks.
pub(crate) struct NamedDestinations {
    names: Vec<(Box<[u8]>, Destination)>,
    /// Positions in `names`, sorted by name.
    sorted: Vec<usize>,
}

impl NamedDestinations {
    pub(crate) fn get(&self, name: &[u8]) -> Option<Destination> {
        let at = self
            .sorted
            .binary_search_by(|at| (*self.names[*at].0).cmp(name))
            .ok()?;
        Some(self.names[self.sorted[at]].1)
    }

    /// Indexes the named destinations that `reads` reads.
    fn build(reads: &mut Reads<'_>) -> PyResult<Self> {
        let root = reads.trailer().get("Root").cloned();
        let catalog = match read(reads, root.as_ref())? {
            Some(Value::Dict(catalog)) => catalog,
            _ => Dict::new(),
        };
        let mut found = Vec::new();
        let mut text = 0;
        let tree = match read(reads, catalog.get("Names"))? {
            Some(Value::Dict(names)) => names.get("Dests").cloned(),
            _ => None,
        };
        if let Some(tree) = tree {
            walk(reads, tree, &mut text, &mut found)?;
        }
        if let Some(Value::Dict(legacy)) = read(reads, catalog.get("Dests"))? {
            for (key, value) in legacy.iter() {
                if found.len() >= MAX_NAMED_ENTRIES {
                    return Err(count_error(format!(
                        "named destinations exceed the {MAX_NAMED_ENTRIES}-entry safety limit"
                    )));
                }
                charge_names(&mut text, key.as_bytes().len())?;
                let value = read(reads, Some(value))?;
                let destination = Destination::read(value, &mut |value| read(reads, value))?;
                found.push((Box::from(key.as_bytes()), destination));
            }
        }
        // A stable sort by name keeps each name's first entry first.
        let mut sorted: Vec<usize> = (0..found.len()).collect();
        sorted.sort_by(|a, b| found[*a].0.cmp(&found[*b].0));
        sorted.dedup_by(|later, earlier| found[*later].0 == found[*earlier].0);
        let mut kept = vec![false; found.len()];
        for at in &sorted {
            kept[*at] = true;
        }
        let names: Vec<_> = found
            .into_iter()
            .zip(kept)
            .filter_map(|(entry, kept)| kept.then_some(entry))
            .collect();
        let mut sorted: Vec<usize> = (0..names.len()).collect();
        sorted.sort_by(|a, b| names[*a].0.cmp(&names[*b].0));
        Ok(Self { names, sorted })
    }
}

/// Follows references from `value` through `reads`, at most [`MAX_CHAIN`];
/// a null, an absent object, or a missing entry is `None`.
pub(crate) fn read(reads: &mut Reads<'_>, value: Option<&Value>) -> PyResult<Option<Value>> {
    let mut current = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(value) => return Ok(Some(value.clone())),
    };
    for _ in 0..MAX_CHAIN {
        let Some(object) = reads
            .get(current.number())
            .map_err(|e| store_error(&e))?
            .filter(|object| object.generation() == current.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Null => return Ok(None),
            Value::Reference(next) => current = *next,
            value => return Ok(Some(value.clone())),
        }
    }
    Err(PdfError::new_err(format!(
        "a reference cycle or a chain of more than {MAX_CHAIN} references"
    )))
}

/// Walks the `/Names /Dests` tree from `root` and adds its entries to
/// `found`, under the limits of its entries, nodes, edges, levels, and
/// name bytes.
fn walk(
    reads: &mut Reads<'_>,
    root: Value,
    text: &mut usize,
    found: &mut Vec<(Box<[u8]>, Destination)>,
) -> PyResult<()> {
    let mut stack = vec![(root, 1_usize)];
    let mut visited = HashSet::new();
    let (mut nodes, mut edges, mut entries) = (0_usize, 0_usize, 0_usize);
    while let Some((node, depth)) = stack.pop() {
        if let Value::Reference(reference) = node
            && !visited.insert(reference)
        {
            continue;
        }
        let Some(Value::Dict(node)) = read(reads, Some(&node))? else {
            continue;
        };
        nodes += 1;
        if nodes > MAX_NAMED_ENTRIES {
            return Err(count_error(format!(
                "named-destination tree exceeds the {MAX_NAMED_ENTRIES}-node safety limit"
            )));
        }
        if let Some(Value::Array(names)) = read(reads, node.get("Names"))? {
            entries += names.len().div_ceil(2);
            if entries > MAX_NAMED_ENTRIES {
                return Err(count_error(format!(
                    "named-destination tree exceeds the {MAX_NAMED_ENTRIES}-entry safety limit"
                )));
            }
            for pair in names.as_slice().chunks(2) {
                let [key, value] = pair else { continue };
                let Some(Value::String(key)) = read(reads, Some(key))? else {
                    continue;
                };
                charge_names(text, key.as_bytes().len())?;
                let value = read(reads, Some(value))?;
                let destination = Destination::read(value, &mut |value| read(reads, value))?;
                found.push((Box::from(key.as_bytes()), destination));
            }
        }
        if let Some(Value::Array(kids)) = read(reads, node.get("Kids"))? {
            if !kids.is_empty() && depth >= MAX_NAMED_DEPTH {
                return Err(PdfError::new_err(format!(
                    "named-destination tree exceeds the {MAX_NAMED_DEPTH}-level safety limit"
                )));
            }
            edges += kids.len();
            if edges > MAX_NAMED_EDGES {
                return Err(count_error(format!(
                    "named-destination tree exceeds the {MAX_NAMED_EDGES}-edge safety limit"
                )));
            }
            stack.extend(kids.iter().rev().map(|kid| (kid.clone(), depth + 1)));
        }
    }
    Ok(())
}

fn count_error(message: String) -> PyErr {
    LimitError::new_err(("named_destination_count", message))
}

fn charge_names(total: &mut usize, amount: usize) -> PyResult<()> {
    *total = total.saturating_add(amount);
    if *total > MAX_NAMED_TEXT_BYTES {
        return Err(LimitError::new_err((
            "named_destination_text_size",
            format!("named-destination text exceeds the {MAX_NAMED_TEXT_BYTES}-byte safety limit"),
        )));
    }
    Ok(())
}

/// Returns the named destinations of the version that reads see.
pub(crate) fn named(document: &_Document) -> PyResult<Arc<NamedDestinations>> {
    let (version, parent) = document.reading_parent()?;
    let budget = document.budget();
    let (memo, version) = match &parent {
        // An open transaction that has written nothing reads its parent.
        Some(parent) => (&document.destinations, parent),
        None if document.in_transaction() => (&document.draft_destinations, &version),
        None => (&document.destinations, &version),
    };
    memo.get_or_compute(&(), version, &budget, |reads| {
        NamedDestinations::build(reads).map_err(Failed)
    })
    .map_err(|Failed(error)| error)
}

/// Reads the links of one page, looking named destinations up once per
/// call. `budget` holds the text bytes that the call encoded and returned.
struct Links<'a> {
    document: &'a _Document,
    annots: &'a Annots,
    named: Option<Arc<NamedDestinations>>,
    budget: (usize, usize),
}

impl<'a> Links<'a> {
    const fn new(document: &'a _Document, annots: &'a Annots) -> Self {
        Self {
            document,
            annots,
            named: None,
            budget: (0, 0),
        }
    }

    /// Returns a link's display rectangle and target, or `None` for an
    /// annotation that is no link or has no rectangle.
    fn link(&mut self, dict: &Dict) -> PyResult<Option<(Rect, Target)>> {
        if self.annots.name(dict, "Subtype")?.as_deref() != Some(b"Link") {
            return Ok(None);
        }
        let Some(area) = self.annots.rect(dict.get("Rect"))? else {
            return Ok(None);
        };
        Ok(Some((self.annots.to_display(area), self.target(dict)?)))
    }

    /// Returns where a link leads: its `/A` action, else its `/Dest`.
    fn target(&mut self, link: &Dict) -> PyResult<Target> {
        let lead = lead(self, link)?;
        let annots = self.annots;
        settle(&lead, &annots.index, &mut |name| self.lookup(name))
    }

    fn lookup(&mut self, name: &[u8]) -> PyResult<Destination> {
        charge(&mut self.budget.0, name.len(), "encoded named destination")?;
        if self.named.is_none() {
            self.named = Some(named(self.document)?);
        }
        Ok(self
            .named
            .as_ref()
            .and_then(|named| named.get(name))
            .unwrap_or(Destination::UNRESOLVED))
    }
}

impl LeadReader for Links<'_> {
    fn resolved(&mut self, value: Option<&Value>) -> PyResult<Option<Value>> {
        self.annots.resolved(value)
    }

    fn text(&mut self, bytes: &[u8], label: &str, decode: bool) -> PyResult<String> {
        if !decode {
            return charge_text(bytes, &mut self.budget, label);
        }
        let (encoded, returned) = &mut self.budget;
        charge(encoded, bytes.len(), &format!("encoded {label}"))?;
        let text = text_string(bytes);
        charge(returned, text.len(), &format!("returned {label}"))?;
        Ok(text)
    }
}

/// Where a link or an outline item leads before its destination resolves:
/// a target, a destination, or the name of a named destination.
#[derive(Clone, Debug)]
pub(crate) enum Lead {
    Target(Target),
    Destination(Destination),
    Name(Box<[u8]>),
}

/// What reading a [`Lead`] needs: the values that references name, and
/// text that a budget charges.
pub(crate) trait LeadReader {
    /// Returns `value`, or what it refers to; `None` for a null.
    fn resolved(&mut self, value: Option<&Value>) -> PyResult<Option<Value>>;

    /// Returns `bytes` as text, decoded as a PDF text string when
    /// `decode`, after charging them as `label`.
    fn text(&mut self, bytes: &[u8], label: &str, decode: bool) -> PyResult<String>;
}

/// Returns where `dict`, a link or an outline item, leads: its `/A`
/// action, else its `/Dest`.
pub(crate) fn lead(reader: &mut impl LeadReader, dict: &Dict) -> PyResult<Lead> {
    let Some(Value::Dict(action)) = reader.resolved(dict.get("A"))? else {
        return match dict.get("Dest") {
            Some(destination) => destination_lead(reader, destination),
            None => Ok(Lead::Target(none())),
        };
    };
    let kind = match reader.resolved(action.get("S"))? {
        Some(Value::Name(kind)) => kind,
        _ => return Ok(Lead::Target(none())),
    };
    Ok(Lead::Target(match kind.as_bytes() {
        b"URI" => {
            let uri = match reader.resolved(action.get("URI"))? {
                Some(Value::String(uri)) => Some(reader.text(uri.as_bytes(), "URI text", false)?),
                _ => None,
            };
            ("uri".to_owned(), None, None, None, uri, None)
        }
        b"GoTo" => match action.get("D") {
            Some(destination) => return destination_lead(reader, destination),
            None => none(),
        },
        b"GoToR" => {
            let file = file(reader, action.get("F"))?;
            let (page, name) = match reader.resolved(action.get("D"))? {
                Some(Value::String(name)) => (
                    None,
                    Some(reader.text(name.as_bytes(), "named destination", false)?),
                ),
                Some(Value::Name(name)) => (
                    None,
                    Some(reader.text(name.as_bytes(), "named destination", false)?),
                ),
                Some(Value::Array(items)) => (
                    match items.get(0) {
                        Some(Value::Integer(page)) => u32::try_from(*page).ok(),
                        _ => None,
                    },
                    None,
                ),
                _ => (None, None),
            };
            ("gotor".to_owned(), page, None, None, file, name)
        }
        b"Launch" => {
            let file = file(reader, action.get("F"))?;
            ("launch".to_owned(), None, None, None, file, None)
        }
        b"Named" => {
            let name = match reader.resolved(action.get("N"))? {
                Some(Value::Name(name)) => {
                    Some(reader.text(name.as_bytes(), "named action", false)?)
                }
                _ => None,
            };
            ("named".to_owned(), None, None, None, None, name)
        }
        _ => none(),
    }))
}

/// Returns the name of a file specification: its `/UF`, else its `/F`,
/// or the string itself.
fn file(reader: &mut impl LeadReader, value: Option<&Value>) -> PyResult<Option<String>> {
    let name = match reader.resolved(value)? {
        Some(Value::Dict(spec)) => match reader.resolved(spec.get("UF"))? {
            Some(name) => Some(name),
            None => reader.resolved(spec.get("F"))?,
        },
        value => value,
    };
    let Some(Value::String(name)) = name else {
        return Ok(None);
    };
    reader.text(name.as_bytes(), "file name", true).map(Some)
}

/// Returns a destination of this document, or the name of one.
fn destination_lead(reader: &mut impl LeadReader, value: &Value) -> PyResult<Lead> {
    Ok(match reader.resolved(Some(value))? {
        Some(Value::Name(name)) => Lead::Name(Box::from(name.as_bytes())),
        Some(Value::String(name)) => Lead::Name(Box::from(name.as_bytes())),
        value => Lead::Destination(Destination::read(value, &mut |value| {
            reader.resolved(value)
        })?),
    })
}

/// Returns the target of `lead` in `index`, with `lookup` giving the
/// destination of a name: `"goto"` for a destination that names a page,
/// `"none"` for one that does not.
pub(crate) fn settle(
    lead: &Lead,
    index: &PageIndex,
    lookup: &mut dyn FnMut(&[u8]) -> PyResult<Destination>,
) -> PyResult<Target> {
    let destination = match lead {
        Lead::Target(target) => return Ok(target.clone()),
        Lead::Destination(destination) => *destination,
        Lead::Name(name) => lookup(name)?,
    };
    Ok(match destination.place(index) {
        Some((page, point, zoom)) => ("goto".to_owned(), Some(page), point, zoom, None, None),
        None => none(),
    })
}

/// Returns the links of page object `(number, generation)` in `/Annots`
/// order, or `None` for a page that is gone.
#[pyfunction]
pub fn link_list(
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<Vec<Record>>> {
    let Some(annots) = Annots::read(document, number, generation)? else {
        return Ok(None);
    };
    let mut links = Links::new(document, &annots);
    let mut out = Vec::new();
    for (position, item) in annots.items.iter().enumerate() {
        let Some((id, dict)) = annots.entry(item)? else {
            continue;
        };
        if let Some((area, target)) = links.link(&dict)? {
            out.push((
                u32::try_from(position).unwrap_or(u32::MAX),
                id,
                area,
                target,
            ));
        }
    }
    Ok(Some(out))
}

/// Returns the link that a handle names, or `None` for a page that is gone.
#[pyfunction]
pub fn link_read(
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> PyResult<Option<Record>> {
    let Some((annots, position, dict)) = edited(document, number, generation, index, id)? else {
        return Ok(None);
    };
    match Links::new(document, &annots).link(&dict)? {
        Some((area, target)) => Ok(Some((
            u32::try_from(position).unwrap_or(u32::MAX),
            id,
            area,
            target,
        ))),
        None => Err(StaleObjectError::new_err(format!(
            "annotation {} at index {position} is no longer a link",
            id.0
        ))),
    }
}

/// Returns the named destinations of the document in the order that
/// `doc.destinations` lists them.
#[pyfunction]
pub fn named_destinations(document: &_Document) -> PyResult<Vec<Named>> {
    let (_, index, _) = index(document)?;
    let named = named(document)?;
    Ok(named
        .names
        .iter()
        .map(|(name, destination)| {
            (
                String::from_utf8_lossy(name).into_owned(),
                destination.place(&index),
            )
        })
        .collect())
}

/// Adds a link over a display rectangle of page object `(number,
/// generation)` that leads to `target`, and returns its object, or `None`
/// for a page that is gone.
#[pyfunction]
pub fn link_add(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    area: Rect,
    target: Target,
) -> PyResult<Option<(u32, u16)>> {
    let Some(annots) = Annots::edit(document, number, generation)? else {
        return Ok(None);
    };
    if annots.items.len() >= MAX_PAGE_ANNOTATIONS {
        return Err(PdfError::new_err(format!(
            "page annotations exceed the {MAX_PAGE_ANNOTATIONS}-entry safety limit"
        )));
    }
    if !matches!(
        annots.resolved(annots.page().dict().get("Annots"))?,
        None | Some(Value::Array(_))
    ) {
        return Err(PdfError::new_err("the page's /Annots is not an array"));
    }
    let rect = link_rect(&annots, area)?;
    let entry = entry(&annots, &target)?;
    let page = annots.page().object();
    document.edit(py, |transaction, budget| {
        let reference = transaction.allocate().map_err(|e| store_error(&e))?;
        let mut dict = Dict::new();
        dict.insert(Name::from("Type"), Value::Name(Name::from("Annot")));
        dict.insert(Name::from("Subtype"), Value::Name(Name::from("Link")));
        dict.insert(Name::from("Rect"), rect);
        dict.insert(
            Name::from("Border"),
            Value::Array(Array::from(vec![Value::Integer(0); 3])),
        );
        dict.insert(Name::from("F"), Value::Integer(4));
        if let Some(page) = page {
            dict.insert(Name::from("P"), Value::Reference(page));
        }
        if let Some((key, value)) = entry {
            dict.insert(Name::from(key), value);
        }
        transaction
            .write(reference.number(), &Value::Dict(dict), budget)
            .map_err(|e| store_error(&e))?;
        let mut items = annots.items.clone();
        items.push(Value::Reference(reference));
        annots.write_items(transaction, items, budget)?;
        Ok(Some((reference.number(), reference.generation())))
    })
}

/// Moves the link that a handle names to a display rectangle, makes it lead
/// to `target`, or both, and returns its object, which a direct dictionary
/// gains here; or `None` for a page that is gone.
#[pyfunction]
#[pyo3(signature = (document, number, generation, index, id, area, target))]
#[expect(
    clippy::too_many_arguments,
    reason = "a handle's page and annotation, and the two things a link edit changes"
)]
pub fn link_edit(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    area: Option<Rect>,
    target: Option<Target>,
) -> PyResult<Option<(u32, u16)>> {
    let Some((annots, position, dict)) = edited(document, number, generation, index, id)? else {
        return Ok(None);
    };
    if annots.name(&dict, "Subtype")?.as_deref() != Some(b"Link") {
        return Err(StaleObjectError::new_err(format!(
            "annotation {} at index {position} is no longer a link",
            id.0
        )));
    }
    let rect = area.map(|area| link_rect(&annots, area)).transpose()?;
    let entry = target.map(|target| entry(&annots, &target)).transpose()?;
    annots
        .write(py, document, position, false, dict, |_, _, dict, _, _| {
            if let Some(rect) = rect {
                dict.insert(Name::from("Rect"), rect);
            }
            if let Some(entry) = entry {
                dict.remove("Dest");
                dict.remove("A");
                if let Some((key, value)) = entry {
                    dict.insert(Name::from(key), value);
                }
            }
            Ok(false)
        })
        .map(Some)
}

/// Returns the PDF `/Rect` of a link's display rectangle.
fn link_rect(annots: &Annots, (x0, y0, x1, y1): Rect) -> PyResult<Value> {
    if !([x0, y0, x1, y1].iter().all(|value| value.is_finite()) && x0 < x1 && y0 < y1) {
        return Err(PdfError::new_err(
            "a link rectangle is finite with x0 < x1 and y0 < y1",
        ));
    }
    Ok(annots.to_pdf((x0, y0, x1, y1)))
}

/// Returns a number that a destination writes, refusing one outside the
/// range of PDF real numbers.
fn real(value: f64, label: &str) -> PyResult<Value> {
    if !value.is_finite() || value.abs() > MAX_REAL {
        return Err(PdfError::new_err(format!(
            "a destination's {label} {value} is outside the PDF real-number range"
        )));
    }
    Ok(number(value))
}

/// Returns the key and value that make a link lead to `target`: `/Dest`
/// for a page of this document, `/A` for an action, or nothing.
fn entry(annots: &Annots, target: &Target) -> PyResult<Option<(&'static str, Value)>> {
    let (_, _, _, _, text, name) = target;
    let size = 4 + text.as_deref().map_or(0, str::len) + name.as_deref().map_or(0, str::len);
    if size > MAX_ANNOTATION_TEXT_BYTES {
        return Err(LimitError::new_err((
            "annotation_input_size",
            format!(
                "link subtype and target input exceeds the {MAX_ANNOTATION_TEXT_BYTES}-byte UTF-8 limit"
            ),
        )));
    }
    target_entry(&annots.index, target)
}

/// Returns the key and value that make a link or an outline item lead to
/// `target`, a page of `index` for `"goto"`: `/Dest` for a page of this
/// document, `/A` for an action, or nothing.
pub(crate) fn target_entry(
    index: &PageIndex,
    target: &Target,
) -> PyResult<Option<(&'static str, Value)>> {
    let (kind, page, point, zoom, text, name) = target;
    let text = |label: &str| {
        text.as_deref()
            .filter(|text| !text.is_empty())
            .ok_or_else(|| PdfError::new_err(format!("a {kind} target needs a {label}")))
    };
    let action = |entries: Vec<(&str, Value)>| {
        Some((
            "A",
            Value::Dict(
                entries
                    .into_iter()
                    .map(|(key, value)| (Name::from(key), value))
                    .collect(),
            ),
        ))
    };
    Ok(match kind.as_str() {
        "none" => None,
        "goto" => {
            let at = page.ok_or_else(|| PdfError::new_err("a destination needs a page"))?;
            let pages = index.pages();
            let Some(target) = pages.get(at as usize) else {
                return Err(PdfError::new_err(format!(
                    "destination page {at} is out of range (0..{})",
                    pages.len().saturating_sub(1)
                )));
            };
            let reference = target.object().ok_or_else(|| {
                PdfError::new_err("the destination page is not an indirect object")
            })?;
            let zoom = match zoom {
                Some(zoom) if *zoom < 0.0 => {
                    return Err(PdfError::new_err(
                        "a destination's zoom must not be negative",
                    ));
                }
                Some(zoom) => Some(real(*zoom, "zoom")?),
                None => None,
            };
            let array = match (point, zoom) {
                (None, None) => vec![Value::Reference(reference), Value::Name(Name::from("Fit"))],
                (point, zoom) => {
                    let (left, top) = match point {
                        Some((x, y)) => {
                            let (left, top) =
                                to_pdf(display_box(target), rotation(target.rotate()), *x, *y);
                            (real(left, "left")?, real(top, "top")?)
                        }
                        None => (Value::Null, Value::Null),
                    };
                    vec![
                        Value::Reference(reference),
                        Value::Name(Name::from("XYZ")),
                        left,
                        top,
                        zoom.unwrap_or(Value::Null),
                    ]
                }
            };
            Some(("Dest", Value::Array(Array::from(array))))
        }
        "uri" => action(vec![
            ("S", Value::Name(Name::from("URI"))),
            (
                "URI",
                Value::String(PdfString::literal(text("URI")?.as_bytes())),
            ),
        ]),
        "gotor" => {
            let destination = match name.as_deref().filter(|name| !name.is_empty()) {
                Some(name) => Value::String(PdfString::literal(name.as_bytes())),
                None => Value::Array(Array::from(vec![
                    Value::Integer(i64::from(page.unwrap_or(0))),
                    Value::Name(Name::from("Fit")),
                ])),
            };
            action(vec![
                ("S", Value::Name(Name::from("GoToR"))),
                ("F", encode_text(text("file")?)),
                ("D", destination),
            ])
        }
        "launch" => action(vec![
            ("S", Value::Name(Name::from("Launch"))),
            ("F", encode_text(text("file")?)),
        ]),
        "named" => {
            let name = name
                .as_deref()
                .filter(|name| !name.is_empty())
                .ok_or_else(|| PdfError::new_err("a named action needs a name"))?;
            action(vec![
                ("S", Value::Name(Name::from("Named"))),
                ("N", Value::Name(Name::from(name))),
            ])
        }
        other => return Err(PdfError::new_err(format!("unknown target kind {other:?}"))),
    })
}
