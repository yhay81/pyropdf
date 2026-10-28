//! The outline (ISO 32000-2 section 12.3.3): its items as handles read
//! them, every item in document order, and the writes that replace the
//! outline or edit one item (DESIGN.md section 10).
//!
//! The walk of every item is memoized with the objects that it read
//! (section 5). It keeps where each item leads before named destinations
//! and pages resolve, so a page edit that leaves the outline's objects alone
//! keeps it, and reads resolve the targets in the page index of their
//! version. Every read and write shares pylopdf's outline limits, so an
//! outline that one call writes stays readable by the others. Ported from
//! pylopdf's `rust/src/document/outline.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321.

use std::collections::HashSet;
use std::sync::Arc;

use pyo3::prelude::*;
use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::{MAX_CHAIN, PageIndex};
use pyropdf_store::refs;
use pyropdf_store::value::{Array, Dict, Name, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::annotations::encode_text;
use crate::annotations::links::{
    Destination, Lead, LeadReader, NamedDestinations, Target, lead, named, read, settle,
    target_entry,
};
use crate::document::{_Document, Failed, text_string};
use crate::errors::{LimitError, PdfError, StaleObjectError, store_error};
use crate::pages::{index, number};

/// The most items of the outline, and of one write.
const MAX_ITEMS: usize = 65_536;
/// The most `/First` and `/Next` entries between the items.
const MAX_EDGES: usize = 131_072;
/// The most levels of the outline.
const MAX_DEPTH: usize = 64;
/// The bytes of the titles and target texts of the outline.
const MAX_TEXT_BYTES: usize = 16 * 1024 * 1024;

/// An item's color: red, green, and blue from 0 to 1.
type Color = (f64, f64, f64);

/// An item as a handle reads it: its level, title, target, color, `/F`
/// flags, `/Count`, and whether it has children.
type ItemRecord = (u32, String, Target, Option<Color>, i64, i64, bool);

/// An item of the whole outline in document order: its object, `(0, 0)`
/// for a direct dictionary, level, title, target, color, `/F` flags, and
/// `/Count`.
type EntryRecord = ((u32, u16), u32, String, Target, Option<Color>, i64, i64);

/// An item to write: its level, title, target, color, `/F` flags, and
/// whether its children show.
type Spec = (u32, String, Target, Option<Color>, u32, bool);

/// Every item of an outline in document order, as the walk read them.
pub(crate) struct Outline {
    items: Vec<Item>,
}

/// An item of an outline before its target resolves.
struct Item {
    id: (u32, u16),
    level: u32,
    title: String,
    lead: Lead,
    color: Option<Color>,
    flags: i64,
    count: i64,
}

/// The text of one walk, charged against [`MAX_TEXT_BYTES`]: what the
/// document encodes and what the walk returns. A walk that checks an
/// edit's text charges it as input.
#[derive(Default)]
struct Text {
    source: usize,
    returned: usize,
    input: bool,
}

impl Text {
    fn charge(&mut self, amount: usize, returned: bool) -> PyResult<()> {
        let (total, label) = if returned {
            (&mut self.returned, "returned text")
        } else {
            (&mut self.source, "source text")
        };
        *total = total.saturating_add(amount);
        if *total <= MAX_TEXT_BYTES {
            return Ok(());
        }
        Err(if self.input {
            LimitError::new_err((
                "toc_input_size",
                format!("outline {label} exceeds the {MAX_TEXT_BYTES}-byte safety limit"),
            ))
        } else {
            LimitError::new_err((
                "toc_text_size",
                format!("TOC {label} exceeds the {MAX_TEXT_BYTES}-byte safety limit"),
            ))
        })
    }
}

/// Reads the items of an outline, charging their text.
struct Walker<'r, 'b> {
    reads: &'r mut Reads<'b>,
    text: Text,
}

impl LeadReader for Walker<'_, '_> {
    fn resolved(&mut self, value: Option<&Value>) -> PyResult<Option<Value>> {
        read(self.reads, value)
    }

    fn text(&mut self, bytes: &[u8], _: &str, decode: bool) -> PyResult<String> {
        self.text.charge(bytes.len(), false)?;
        let text = if decode {
            text_string(bytes)
        } else {
            String::from_utf8_lossy(bytes).into_owned()
        };
        self.text.charge(text.len(), true)?;
        Ok(text)
    }
}

impl Walker<'_, '_> {
    /// Returns an item's title, empty when it has none.
    fn title(&mut self, item: &Dict) -> PyResult<String> {
        match self.resolved(item.get("Title"))? {
            Some(Value::String(title)) => self.text(title.as_bytes(), "title", true),
            _ => Ok(String::new()),
        }
    }

    /// Returns where an item leads, charging the name of a named
    /// destination.
    fn lead(&mut self, item: &Dict) -> PyResult<Lead> {
        let lead = lead(self, item)?;
        if let Lead::Name(name) = &lead {
            self.text(name, "destination name", false)?;
        }
        Ok(lead)
    }

    /// Returns an item's color: three numbers from 0 to 1, or `None` for
    /// the viewer's default.
    fn color(&mut self, item: &Dict) -> PyResult<Option<Color>> {
        let Some(Value::Array(components)) = self.resolved(item.get("C"))? else {
            return Ok(None);
        };
        if components.len() != 3 {
            return Ok(None);
        }
        let mut color = [0.0; 3];
        for (slot, component) in color.iter_mut().zip(components.iter()) {
            *slot = match self.resolved(Some(component))? {
                Some(Value::Integer(value)) => value as f64,
                Some(Value::Real(value)) => value.value(),
                _ => return Ok(None),
            };
            if !(slot.is_finite() && (0.0..=1.0).contains(slot)) {
                return Ok(None);
            }
        }
        Ok(Some((color[0], color[1], color[2])))
    }

    /// Returns an integer entry of an item, or 0.
    fn integer(&mut self, item: &Dict, key: &str) -> PyResult<i64> {
        Ok(match self.resolved(item.get(key))? {
            Some(Value::Integer(value)) => value,
            _ => 0,
        })
    }
}

/// Resolves targets in the page index of their version, indexing the
/// named destinations once.
struct Places<'a> {
    document: &'a _Document,
    index: Arc<PageIndex>,
    named: Option<Arc<NamedDestinations>>,
}

impl<'a> Places<'a> {
    const fn new(document: &'a _Document, index: Arc<PageIndex>) -> Self {
        Self {
            document,
            index,
            named: None,
        }
    }

    fn target(&mut self, lead: &Lead) -> PyResult<Target> {
        let index = Arc::clone(&self.index);
        settle(lead, &index, &mut |name| {
            if self.named.is_none() {
                let (version, budget) = view(self.document)?;
                self.named = Some(named(self.document, &version, &budget)?);
            }
            Ok(self
                .named
                .as_ref()
                .and_then(|named| named.get(name))
                .unwrap_or(Destination::UNRESOLVED))
        })
    }
}

fn count_error(message: String) -> PyErr {
    LimitError::new_err(("toc_entry_count", message))
}

fn stale((number, generation): (u32, u16)) -> PyErr {
    StaleObjectError::new_err(format!(
        "outline item {number} {generation} R no longer exists"
    ))
}

/// Returns the catalog and its object.
fn catalog(reads: &mut Reads<'_>) -> PyResult<Option<(Dict, Option<u32>)>> {
    let root = reads.trailer().get("Root").cloned();
    let object = last(reads, root.as_ref())?;
    Ok(match read(reads, root.as_ref())? {
        Some(Value::Dict(catalog)) => Some((catalog, object)),
        _ => None,
    })
}

/// Returns the outline's root dictionary and the object that holds it, or
/// `None` for a document without an outline.
fn root(reads: &mut Reads<'_>) -> PyResult<Option<(Dict, Option<u32>)>> {
    let Some((catalog, _)) = catalog(reads)? else {
        return Ok(None);
    };
    let value = catalog.get("Outlines");
    match read(reads, value)? {
        None => Ok(None),
        Some(Value::Dict(root)) => Ok(Some((root, last(reads, value)?))),
        Some(_) => Err(PdfError::new_err("catalog Outlines is not a dictionary")),
    }
}

/// Returns the object at the end of the references from `value`, or
/// `None` for a direct value.
fn last(reads: &mut Reads<'_>, value: Option<&Value>) -> PyResult<Option<u32>> {
    let Some(Value::Reference(mut current)) = value.cloned() else {
        return Ok(None);
    };
    for _ in 0..MAX_CHAIN {
        let next = match reads.get(current.number()).map_err(|e| store_error(&e))? {
            Some(object) if object.generation() == current.generation() => {
                match &**object.value() {
                    Value::Reference(next) => *next,
                    _ => return Ok(Some(current.number())),
                }
            }
            _ => return Ok(Some(current.number())),
        };
        current = next;
    }
    Ok(Some(current.number()))
}

/// Returns the dictionary of item object `id`, or `None` when it is gone.
fn item(reads: &mut Reads<'_>, (number, generation): (u32, u16)) -> PyResult<Option<Dict>> {
    Ok(match reads.get(number).map_err(|e| store_error(&e))? {
        Some(object) if object.generation() == generation => match &**object.value() {
            Value::Dict(item) => Some(item.clone()),
            _ => None,
        },
        _ => None,
    })
}

/// Returns the items of the `/First` and `/Next` chain of `parent` that
/// are objects, up to the first entry that is no item or that the chain
/// already listed.
fn children(reads: &mut Reads<'_>, parent: &Dict) -> PyResult<Vec<(u32, u16)>> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    let mut next = parent.get("First").cloned();
    while let Some(Value::Reference(reference)) = next {
        let id = (reference.number(), reference.generation());
        if !seen.insert(id) {
            break;
        }
        let Some(child) = item(reads, id)? else {
            break;
        };
        if out.len() >= MAX_ITEMS {
            return Err(count_error(format!(
                "outline children exceed the {MAX_ITEMS}-entry safety limit"
            )));
        }
        out.push(id);
        next = child.get("Next").cloned();
    }
    Ok(out)
}

/// Returns whether an item has children: whether its `/First` names an
/// item.
fn has_children(reads: &mut Reads<'_>, item_dict: &Dict) -> PyResult<bool> {
    Ok(match item_dict.get("First") {
        Some(Value::Reference(first)) => {
            item(reads, (first.number(), first.generation()))?.is_some()
        }
        _ => false,
    })
}

/// Returns an item's level: one more than the `/Parent` steps to the root
/// `root`, at most [`MAX_DEPTH`].
fn level(reads: &mut Reads<'_>, item_dict: &Dict, root: Option<u32>) -> PyResult<u32> {
    let mut level = 1;
    let mut parent = item_dict.get("Parent").cloned();
    for _ in 0..MAX_DEPTH {
        let Some(Value::Reference(reference)) = parent else {
            break;
        };
        if Some(reference.number()) == root {
            break;
        }
        let Some(next) = item(reads, (reference.number(), reference.generation()))? else {
            break;
        };
        let Some(above) = next.get("Parent") else {
            break;
        };
        level += 1;
        parent = Some(above.clone());
    }
    Ok(level)
}

/// Walks every item of the outline in document order under the outline
/// limits. `edit` stands for the dictionary of one item, to check the text
/// that an edit of it leaves.
fn walk(reads: &mut Reads<'_>, edit: Option<((u32, u16), &Dict)>) -> PyResult<Outline> {
    let mut items = Vec::new();
    let Some((root, _)) = root(reads)? else {
        return Ok(Outline { items });
    };
    let Some(first) = root.get("First").cloned() else {
        return Ok(Outline { items });
    };
    let mut walker = Walker {
        reads,
        text: Text {
            input: edit.is_some(),
            ..Text::default()
        },
    };
    let mut stack = vec![(first, 1_usize)];
    let mut visited = HashSet::new();
    let (mut nodes, mut edges) = (0_usize, 1_usize);
    while let Some((value, depth)) = stack.pop() {
        let id = match &value {
            Value::Reference(reference) => {
                if !visited.insert(*reference) {
                    continue;
                }
                (reference.number(), reference.generation())
            }
            _ => (0, 0),
        };
        let node = match edit {
            Some((edited, dict)) if id.0 != 0 && edited == id => dict.clone(),
            _ => match walker.resolved(Some(&value))? {
                Some(Value::Dict(node)) => node,
                _ => continue,
            },
        };
        nodes += 1;
        if nodes > MAX_ITEMS {
            return Err(count_error(format!(
                "outline tree exceeds the {MAX_ITEMS}-node safety limit"
            )));
        }
        let title = walker.title(&node)?;
        let lead = walker.lead(&node)?;
        items.push(Item {
            id,
            level: u32::try_from(depth).unwrap_or(u32::MAX),
            title,
            lead,
            color: walker.color(&node)?,
            flags: walker.integer(&node, "F")?,
            count: walker.integer(&node, "Count")?,
        });
        for (key, child_depth) in [("Next", depth), ("First", depth + 1)] {
            let Some(next) = node.get(key) else {
                continue;
            };
            if child_depth > MAX_DEPTH {
                return Err(PdfError::new_err(format!(
                    "outline tree exceeds the {MAX_DEPTH}-level safety limit"
                )));
            }
            edges += 1;
            if edges > MAX_EDGES {
                return Err(count_error(format!(
                    "outline tree exceeds the {MAX_EDGES}-edge safety limit"
                )));
            }
            // `/First` goes on top, so a child comes before its next sibling.
            stack.push((next.clone(), child_depth));
        }
    }
    Ok(Outline { items })
}

/// Returns the version that reads see.
fn view(document: &_Document) -> PyResult<(Arc<Version>, Budget)> {
    Ok((document.reading_parent()?.0, document.budget()))
}

/// Returns the outline's items of the version that reads see, memoized,
/// and that version's page index.
fn memoized(document: &_Document) -> PyResult<(Arc<Outline>, Arc<PageIndex>)> {
    let (_, index, _) = index(document)?;
    let (version, parent) = document.reading_parent()?;
    let budget = document.budget();
    let (memo, version) = match &parent {
        // An open transaction that has written nothing reads its parent.
        Some(parent) => (&document.outline, parent),
        None if document.in_transaction() => (&document.draft_outline, &version),
        None => (&document.outline, &version),
    };
    let outline = memo
        .get_or_compute(&(), version, &budget, |reads| {
            walk(reads, None).map_err(Failed)
        })
        .map_err(|Failed(error)| error)?;
    Ok((outline, index))
}

/// Returns the top-level items of the outline, or the children of item
/// `parent`.
#[pyfunction]
#[pyo3(signature = (document, parent=None))]
pub fn outline_children(
    document: &_Document,
    parent: Option<(u32, u16)>,
) -> PyResult<Vec<(u32, u16)>> {
    let (version, budget) = view(document)?;
    let mut reads = Reads::new(&version, &budget);
    let dict = match parent {
        None => match root(&mut reads)? {
            Some((root, _)) => root,
            None => return Ok(Vec::new()),
        },
        Some(id) => item(&mut reads, id)?.ok_or_else(|| stale(id))?,
    };
    children(&mut reads, &dict)
}

/// Returns item `id` as a handle reads it.
#[pyfunction]
pub fn outline_item(document: &_Document, id: (u32, u16)) -> PyResult<ItemRecord> {
    let (version, index, budget) = index(document)?;
    let mut reads = Reads::new(&version, &budget);
    let dict = item(&mut reads, id)?.ok_or_else(|| stale(id))?;
    let mut walker = Walker {
        reads: &mut reads,
        text: Text::default(),
    };
    let title = walker.title(&dict)?;
    let lead = walker.lead(&dict)?;
    let color = walker.color(&dict)?;
    let flags = walker.integer(&dict, "F")?;
    let count = walker.integer(&dict, "Count")?;
    let children = has_children(&mut reads, &dict)?;
    let root = root(&mut reads)?.and_then(|(_, object)| object);
    let level = level(&mut reads, &dict, root)?;
    let target = Places::new(document, index).target(&lead)?;
    Ok((level, title, target, color, flags, count, children))
}

/// Returns every item of the outline in document order.
#[pyfunction]
pub fn outline_entries(document: &_Document) -> PyResult<Vec<EntryRecord>> {
    let (outline, index) = memoized(document)?;
    let mut places = Places::new(document, index);
    outline
        .items
        .iter()
        .map(|item| {
            Ok((
                item.id,
                item.level,
                item.title.clone(),
                places.target(&item.lead)?,
                item.color,
                item.flags,
                item.count,
            ))
        })
        .collect()
}

/// Returns how many outline walks the document's memos reused and how many
/// they computed, for tests.
#[pyfunction]
pub fn outline_walk_counts(document: &_Document) -> (u64, u64) {
    let (outline, drafts) = (document.outline.stats(), document.draft_outline.stats());
    (
        outline.hits + drafts.hits,
        outline.computations + drafts.computations,
    )
}

/// The text that one write gives, in UTF-8 and encoded as PDF text
/// strings, against [`MAX_TEXT_BYTES`].
#[derive(Default)]
struct Input {
    utf8: usize,
    encoded: usize,
}

impl Input {
    fn charge(&mut self, text: &str, label: &str) -> PyResult<()> {
        self.utf8 = self.utf8.saturating_add(text.len());
        if self.utf8 > MAX_TEXT_BYTES {
            return Err(LimitError::new_err((
                "toc_input_size",
                format!("outline {label} input exceeds the {MAX_TEXT_BYTES}-byte safety limit"),
            )));
        }
        let encoded = if text.is_ascii() {
            text.len()
        } else {
            2 + 2 * text.encode_utf16().count()
        };
        self.encoded = self.encoded.saturating_add(encoded);
        if self.encoded > MAX_TEXT_BYTES {
            return Err(LimitError::new_err((
                "toc_input_size",
                format!(
                    "encoded outline {label} input exceeds the {MAX_TEXT_BYTES}-byte encoded-text safety limit"
                ),
            )));
        }
        Ok(())
    }

    fn target(&mut self, target: &Target) -> PyResult<()> {
        for text in [&target.4, &target.5].into_iter().flatten() {
            self.charge(text, "target")?;
        }
        Ok(())
    }
}

/// Returns a color as a `/C` array, refusing components outside 0 to 1.
fn color_value((red, green, blue): Color) -> PyResult<Value> {
    let mut components = Vec::with_capacity(3);
    for value in [red, green, blue] {
        if !(value.is_finite() && (0.0..=1.0).contains(&value)) {
            return Err(PdfError::new_err(format!(
                "outline color components must be from 0 to 1: {value}"
            )));
        }
        components.push(number(value));
    }
    Ok(Value::Array(Array::from(components)))
}

/// An item to write, checked.
struct Prepared {
    level: usize,
    title: Value,
    entry: Option<(&'static str, Value)>,
    color: Option<Value>,
    flags: u32,
    open: bool,
}

/// Replaces the outline with the items of `specs`, all checked before any
/// change, and frees the objects of the outline before that nothing else
/// refers to.
#[pyfunction]
pub fn outline_write(py: Python<'_>, document: &_Document, specs: Vec<Spec>) -> PyResult<()> {
    if specs.len() > MAX_ITEMS {
        return Err(count_error(format!(
            "cannot write more than {MAX_ITEMS} outline items"
        )));
    }
    let (_, index, _) = index(document)?;
    let mut input = Input::default();
    let mut prepared = Vec::with_capacity(specs.len());
    let mut previous = 0;
    for (level, title, target, color, flags, open) in &specs {
        let level = *level as usize;
        if level == 0 || level > previous + 1 {
            return Err(PdfError::new_err(format!(
                "invalid outline level {level}; levels start at 1 and can increase by at most one"
            )));
        }
        if level > MAX_DEPTH {
            return Err(PdfError::new_err(format!(
                "outline depth exceeds the {MAX_DEPTH}-level safety limit"
            )));
        }
        input.charge(title, "title")?;
        input.target(target)?;
        prepared.push(Prepared {
            level,
            title: encode_text(title),
            entry: target_entry(&index, target)?,
            color: color.map(color_value).transpose()?,
            flags: *flags,
            open: *open,
        });
        previous = level;
    }
    drop(specs);
    let count = prepared.len();
    // The parent, the first and last child, and the siblings of each item,
    // by position.
    let mut parent = vec![None; count];
    let mut first = vec![None; count];
    let mut last_child = vec![None; count];
    let mut next = vec![None; count];
    let mut previous_sibling = vec![None; count];
    let mut top: (Option<usize>, Option<usize>) = (None, None);
    let mut chain: Vec<usize> = Vec::with_capacity(MAX_DEPTH);
    for (at, item) in prepared.iter().enumerate() {
        chain.truncate(item.level - 1);
        let before = match chain.last() {
            Some(&owner) => {
                parent[at] = Some(owner);
                first[owner].get_or_insert(at);
                last_child[owner].replace(at)
            }
            None => {
                top.0.get_or_insert(at);
                top.1.replace(at)
            }
        };
        if let Some(before) = before {
            next[before] = Some(at);
            previous_sibling[at] = Some(before);
        }
        chain.push(at);
    }
    // An item comes before its descendants, so a reverse pass finishes
    // each item's count before its parent adds it.
    let mut visible = vec![0_i64; count];
    let mut root_visible = 0_i64;
    for at in (0..count).rev() {
        let shown = 1 + if prepared[at].open { visible[at] } else { 0 };
        match parent[at] {
            Some(owner) => visible[owner] += shown,
            None => root_visible += shown,
        }
    }
    document.edit(py, |transaction, budget| {
        let store = |e: pyropdf_store::Error| store_error(&e);
        let before = transaction.snapshot();
        let mut reads = Reads::new(&before, budget);
        let Some((mut catalog_dict, Some(catalog_object))) = catalog(&mut reads)? else {
            return Err(PdfError::new_err("the catalog is not an object"));
        };
        let old = objects(&mut reads, &catalog_dict)?;
        catalog_dict.remove("Outlines");
        if count > 0 {
            let root = transaction.allocate().map_err(store)?;
            let mut ids = Vec::with_capacity(count);
            for _ in 0..count {
                ids.push(transaction.allocate().map_err(store)?);
            }
            for (at, item) in prepared.into_iter().enumerate() {
                let mut dict = Dict::new();
                dict.insert(Name::from("Title"), item.title);
                let owner = parent[at].map_or(root, |owner| ids[owner]);
                dict.insert(Name::from("Parent"), Value::Reference(owner));
                if let Some(before) = previous_sibling[at] {
                    dict.insert(Name::from("Prev"), Value::Reference(ids[before]));
                }
                if let Some(after) = next[at] {
                    dict.insert(Name::from("Next"), Value::Reference(ids[after]));
                }
                if let (Some(child), Some(final_child)) = (first[at], last_child[at]) {
                    dict.insert(Name::from("First"), Value::Reference(ids[child]));
                    dict.insert(Name::from("Last"), Value::Reference(ids[final_child]));
                    let shown = if item.open { visible[at] } else { -visible[at] };
                    dict.insert(Name::from("Count"), Value::Integer(shown));
                }
                if let Some((key, value)) = item.entry {
                    dict.insert(Name::from(key), value);
                }
                if let Some(color) = item.color {
                    dict.insert(Name::from("C"), color);
                }
                if item.flags != 0 {
                    dict.insert(Name::from("F"), Value::Integer(i64::from(item.flags)));
                }
                transaction
                    .write(ids[at].number(), &Value::Dict(dict), budget)
                    .map_err(store)?;
            }
            if let (Some(child), Some(final_child)) = top {
                let mut dict = Dict::new();
                dict.insert(Name::from("Type"), Value::Name(Name::from("Outlines")));
                dict.insert(Name::from("First"), Value::Reference(ids[child]));
                dict.insert(Name::from("Last"), Value::Reference(ids[final_child]));
                dict.insert(Name::from("Count"), Value::Integer(root_visible));
                transaction
                    .write(root.number(), &Value::Dict(dict), budget)
                    .map_err(store)?;
            }
            catalog_dict.insert(Name::from("Outlines"), Value::Reference(root));
        }
        transaction
            .write(catalog_object, &Value::Dict(catalog_dict), budget)
            .map_err(store)?;
        free(document, transaction, &old, budget)
    })
}

/// Returns the objects of the outline of `catalog`: the objects to its
/// root, and the items that `/First` and `/Next` reach, at most
/// [`MAX_ITEMS`] of them.
fn objects(reads: &mut Reads<'_>, catalog: &Dict) -> PyResult<Vec<u32>> {
    let mut out = Vec::new();
    let mut current = catalog.get("Outlines").cloned();
    for _ in 0..MAX_CHAIN {
        let Some(Value::Reference(reference)) = current else {
            break;
        };
        out.push(reference.number());
        current = reads
            .get(reference.number())
            .map_err(|e| store_error(&e))?
            .map(|object| (**object.value()).clone());
    }
    let Some(Value::Dict(root)) = read(reads, catalog.get("Outlines"))? else {
        return Ok(out);
    };
    let mut stack: Vec<Value> = root.get("First").cloned().into_iter().collect();
    let mut visited = HashSet::new();
    while let Some(value) = stack.pop() {
        let Value::Reference(reference) = value else {
            continue;
        };
        if visited.len() >= MAX_ITEMS || !visited.insert(reference) {
            continue;
        }
        let Some(node) = item(reads, (reference.number(), reference.generation()))? else {
            continue;
        };
        out.push(reference.number());
        stack.extend(
            ["Next", "First"]
                .iter()
                .filter_map(|key| node.get(key).cloned()),
        );
    }
    Ok(out)
}

/// Frees the objects of `candidates`, and those that only they refer to,
/// that nothing refers to after the edits of `transaction`.
fn free(
    document: &_Document,
    transaction: &mut Transaction,
    candidates: &[u32],
    budget: &Budget,
) -> PyResult<()> {
    if candidates.is_empty() {
        return Ok(());
    }
    let store = |e: pyropdf_store::Error| store_error(&e);
    let parent = Arc::clone(transaction.parent());
    let references = document.references.get(&parent, budget).map_err(store)?;
    let after = transaction.snapshot();
    let references = match parent.changes(&after, budget).map_err(store)? {
        Some(changes) if !changes.is_empty() => Arc::new(
            references
                .update(&mut Reads::new(&after, budget), &changes)
                .map_err(store)?,
        ),
        _ => references,
    };
    let freed = refs::unreferenced(&references, &[], candidates, budget).map_err(store)?;
    for &number in freed.iter() {
        transaction.free(number, budget).map_err(store)?;
    }
    Ok(())
}

/// Writes item `id` with `change` made to it, after checking that the
/// change leaves the outline's text within its limit when `text` is set.
fn edit_item(
    py: Python<'_>,
    document: &_Document,
    id: (u32, u16),
    text: bool,
    change: impl FnOnce(&mut Dict),
) -> PyResult<()> {
    document.edit(py, |transaction, budget| {
        let before = transaction.snapshot();
        let mut reads = Reads::new(&before, budget);
        let mut dict = item(&mut reads, id)?.ok_or_else(|| stale(id))?;
        change(&mut dict);
        if text {
            walk(&mut reads, Some((id, &dict)))?;
        }
        transaction
            .write(id.0, &Value::Dict(dict), budget)
            .map_err(|e| store_error(&e))?;
        Ok(())
    })
}

/// Sets the title of item `id`.
#[pyfunction]
pub fn outline_set_title(
    py: Python<'_>,
    document: &_Document,
    id: (u32, u16),
    title: &str,
) -> PyResult<()> {
    Input::default().charge(title, "title")?;
    let title = encode_text(title);
    edit_item(py, document, id, true, |dict| {
        dict.insert(Name::from("Title"), title);
    })
}

/// Makes item `id` lead to `target`.
#[pyfunction]
pub fn outline_set_target(
    py: Python<'_>,
    document: &_Document,
    id: (u32, u16),
    target: Target,
) -> PyResult<()> {
    Input::default().target(&target)?;
    let (_, index, _) = index(document)?;
    let entry = target_entry(&index, &target)?;
    edit_item(py, document, id, true, |dict| {
        dict.remove("Dest");
        dict.remove("A");
        if let Some((key, value)) = entry {
            dict.insert(Name::from(key), value);
        }
    })
}

/// Sets the color and the `/F` flags of item `id`.
#[pyfunction]
pub fn outline_set_style(
    py: Python<'_>,
    document: &_Document,
    id: (u32, u16),
    color: Option<Color>,
    flags: u32,
) -> PyResult<()> {
    let color = color.map(color_value).transpose()?;
    edit_item(py, document, id, false, |dict| {
        match color {
            Some(color) => dict.insert(Name::from("C"), color),
            None => dict.remove("C"),
        };
        if flags == 0 {
            dict.remove("F");
        } else {
            dict.insert(Name::from("F"), Value::Integer(i64::from(flags)));
        }
    })
}

/// Shows or hides the children of item `id`, and counts every `/Count` of
/// the outline again (ISO 32000-2 Table 151).
#[pyfunction]
pub fn outline_set_open(
    py: Python<'_>,
    document: &_Document,
    id: (u32, u16),
    open: bool,
) -> PyResult<()> {
    document.edit(py, |transaction, budget| {
        let store = |e: pyropdf_store::Error| store_error(&e);
        let before = transaction.snapshot();
        let mut reads = Reads::new(&before, budget);
        let dict = item(&mut reads, id)?.ok_or_else(|| stale(id))?;
        if !has_children(&mut reads, &dict)? {
            return Err(PdfError::new_err(
                "an outline item without children has no open state",
            ));
        }
        let Some((root, root_object)) = root(&mut reads)? else {
            return Ok(());
        };
        let counts = counts(&mut reads, &root, (id, open))?;
        for (target, count) in counts {
            match target.map(|id| id.0).or(root_object) {
                Some(number) => {
                    let id = target.unwrap_or((number, generation(&mut reads, number)?));
                    let mut dict = item(&mut reads, id)?.ok_or_else(|| stale(id))?;
                    if dict.get("Count") != Some(&Value::Integer(count)) {
                        dict.insert(Name::from("Count"), Value::Integer(count));
                        transaction
                            .write(number, &Value::Dict(dict), budget)
                            .map_err(store)?;
                    }
                }
                None => {
                    // A direct root lives in the catalog.
                    let Some((mut catalog, Some(object))) = catalog(&mut reads)? else {
                        return Err(PdfError::new_err("the catalog is not an object"));
                    };
                    let mut root = root.clone();
                    root.insert(Name::from("Count"), Value::Integer(count));
                    catalog.insert(Name::from("Outlines"), Value::Dict(root));
                    transaction
                        .write(object, &Value::Dict(catalog), budget)
                        .map_err(store)?;
                }
            }
        }
        Ok(())
    })
}

/// Returns the generation of object `number`.
fn generation(reads: &mut Reads<'_>, number: u32) -> PyResult<u16> {
    Ok(reads
        .get(number)
        .map_err(|e| store_error(&e))?
        .map_or(0, |object| object.generation()))
}

/// The `/Count` of an item, or of the root as `None`.
type Count = (Option<(u32, u16)>, i64);

/// Returns the `/Count` of every item with children, and of the root as
/// `None`, reading each item's open state from the sign of its count but
/// for `change`, whose state it gives.
fn counts(reads: &mut Reads<'_>, root: &Dict, change: ((u32, u16), bool)) -> PyResult<Vec<Count>> {
    // Items in document order with the position of their parent, as the
    // walk visits them, under the same limits.
    let mut order: Vec<((u32, u16), Option<usize>, bool)> = Vec::new();
    let mut stack: Vec<((u32, u16), Option<usize>, usize)> = Vec::new();
    let mut visited = HashSet::new();
    let mut edges = 0;
    let mut push = |reads: &mut Reads<'_>,
                    stack: &mut Vec<((u32, u16), Option<usize>, usize)>,
                    owner: &Dict,
                    parent: Option<usize>,
                    depth: usize|
     -> PyResult<()> {
        let children = children(reads, owner)?;
        edges += children.len();
        if edges > MAX_EDGES {
            return Err(count_error(format!(
                "outline tree exceeds the {MAX_EDGES}-edge safety limit"
            )));
        }
        if !children.is_empty() && depth > MAX_DEPTH {
            return Err(PdfError::new_err(format!(
                "outline tree exceeds the {MAX_DEPTH}-level safety limit"
            )));
        }
        stack.extend(children.into_iter().rev().map(|id| (id, parent, depth)));
        Ok(())
    };
    push(reads, &mut stack, root, None, 1)?;
    while let Some((id, parent, depth)) = stack.pop() {
        if !visited.insert(id) {
            continue;
        }
        if order.len() >= MAX_ITEMS {
            return Err(count_error(format!(
                "outline tree exceeds the {MAX_ITEMS}-node safety limit"
            )));
        }
        let Some(dict) = item(reads, id)? else {
            continue;
        };
        let open = if id == change.0 {
            change.1
        } else {
            matches!(read(reads, dict.get("Count"))?, Some(Value::Integer(count)) if count > 0)
        };
        order.push((id, parent, open));
        let position = order.len() - 1;
        push(reads, &mut stack, &dict, Some(position), depth + 1)?;
    }
    let mut visible = vec![0_i64; order.len()];
    let mut has_children = vec![false; order.len()];
    let mut root_visible = 0_i64;
    for at in (0..order.len()).rev() {
        let (_, parent, open) = order[at];
        let shown = 1 + if open { visible[at] } else { 0 };
        match parent {
            Some(owner) => {
                visible[owner] += shown;
                has_children[owner] = true;
            }
            None => root_visible += shown,
        }
    }
    let mut counts = Vec::with_capacity(order.len() + 1);
    for (at, &(id, _, open)) in order.iter().enumerate() {
        if has_children[at] {
            counts.push((Some(id), if open { visible[at] } else { -visible[at] }));
        }
    }
    counts.push((None, root_visible));
    Ok(counts)
}
