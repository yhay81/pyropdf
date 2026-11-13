//! The outline (ISO 32000-2 section 12.3.3): its items as handles read
//! them, every item in document order, and the writes that replace the
//! outline or edit one item.
//!
//! The walk of every item is memoized with the objects that it read
//! (DESIGN.md section 5). It keeps where each item leads before named
//! destinations and pages resolve, so a page edit that leaves the outline's
//! objects alone keeps it, and reads resolve the targets in the page index
//! of their version. Every read and write shares pylopdf's outline limits,
//! so an outline that one call writes stays readable by the others. Ported
//! from pylopdf's `rust/src/document/outline.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321.

mod count_cache;

pub use count_cache::CountCache;
use count_cache::Verified;

use std::collections::HashSet;
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::{Memo, Reads};
use pyropdf_store::pages::{MAX_CHAIN, PageIndex, PageTree};
use pyropdf_store::refs;
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Object, Transaction, Version};

use crate::display::{encode_text, number};
use crate::forms::text_string;

use super::destinations::{
    Destination, Lead, LeadReader, NamedDestinations, lead, read, settle, target_entry,
};
use super::{Color, EntryRecord, Error, ItemRecord, Result, Spec, Target, visit, with_savepoint};

/// The most items of the outline, and of one write.
pub const MAX_ITEMS: usize = 65_536;
/// The most `/First` and `/Next` entries between the items.
pub const MAX_EDGES: usize = 131_072;
/// The most levels of the outline.
pub const MAX_DEPTH: usize = 64;
/// The bytes of the titles and target texts of the outline.
pub const MAX_TEXT_BYTES: usize = 16 * 1024 * 1024;

fn memory(budget: &Budget, bytes: usize) -> Result<()> {
    budget.charge(LimitCode::MemorySize, bytes as u64)?;
    Ok(())
}

fn copy(value: Option<&Value>, budget: &Budget) -> Result<Option<Value>> {
    value
        .map(|value| refs::copy_for_edit(value, budget))
        .transpose()
        .map_err(Error::from)
}

fn filled<'b, T: Clone>(count: usize, value: T, budget: &'b Budget) -> Result<BudgetVec<'b, T>> {
    let mut items = BudgetVec::new(budget);
    items.try_reserve_exact(count)?;
    for _ in 0..count {
        items.try_push(value.clone())?;
    }
    Ok(items)
}

/// Every item of an outline in document order, as the walk read them.
pub struct Outline {
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

impl Outline {
    /// Returns the memoized outline walk of `version`.
    pub fn memoized(
        memo: &Memo<(), Self>,
        version: &Arc<Version>,
        budget: &Budget,
    ) -> Result<Arc<Self>> {
        memo.get_or_compute(&(), version, budget, |reads| walk(reads, None))
    }
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
    fn charge(&mut self, amount: usize, returned: bool) -> Result<()> {
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
            Error::Limit {
                code: LimitCode::TocInputSize,
                message: format!("outline {label} exceeds the {MAX_TEXT_BYTES}-byte safety limit"),
            }
        } else {
            Error::Limit {
                code: LimitCode::TocTextSize,
                message: format!("TOC {label} exceeds the {MAX_TEXT_BYTES}-byte safety limit"),
            }
        })
    }
}

/// Reads the items of an outline, charging their text.
struct Walker<'r, 'b> {
    reads: &'r mut Reads<'b>,
    text: Text,
}

impl LeadReader for Walker<'_, '_> {
    fn budget(&self) -> &Budget {
        self.reads.budget()
    }

    fn resolved(&mut self, value: Option<&Value>) -> Result<Option<Value>> {
        read(self.reads, value)
    }

    fn text(&mut self, bytes: &[u8], _: &str, decode: bool) -> Result<String> {
        self.text.charge(bytes.len(), false)?;
        memory(self.reads.budget(), bytes.len().saturating_mul(3))?;
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
    fn title(&mut self, item: &Dict) -> Result<String> {
        match self.resolved(item.get("Title"))? {
            Some(Value::String(title)) => self.text(title.as_bytes(), "title", true),
            _ => Ok(String::new()),
        }
    }

    fn lead(&mut self, item: &Dict) -> Result<Lead> {
        let lead = lead(self, item)?;
        if let Lead::Name(name) = &lead {
            self.text(name, "destination name", false)?;
        }
        Ok(lead)
    }

    fn color(&mut self, item: &Dict) -> Result<Option<Color>> {
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

    fn integer(&mut self, item: &Dict, key: &str) -> Result<i64> {
        Ok(match self.resolved(item.get(key))? {
            Some(Value::Integer(value)) => value,
            _ => 0,
        })
    }
}

/// Resolves targets in the page index of their version, indexing the
/// named destinations once.
struct Places<'a> {
    destinations: &'a Memo<(), NamedDestinations>,
    index: &'a PageIndex,
    version: &'a Arc<Version>,
    budget: &'a Budget,
    named: Option<Arc<NamedDestinations>>,
}

impl<'a> Places<'a> {
    const fn new(
        destinations: &'a Memo<(), NamedDestinations>,
        version: &'a Arc<Version>,
        index: &'a PageIndex,
        budget: &'a Budget,
    ) -> Self {
        Self {
            destinations,
            index,
            version,
            budget,
            named: None,
        }
    }

    fn target(&mut self, lead: &Lead) -> Result<Target> {
        let index = self.index;
        settle(lead, index, self.budget, &mut |name| {
            if self.named.is_none() {
                self.named = Some(NamedDestinations::memoized(
                    self.destinations,
                    self.version,
                    self.budget,
                )?);
            }
            Ok(self
                .named
                .as_ref()
                .and_then(|named| named.get(name))
                .unwrap_or(Destination::UNRESOLVED))
        })
    }
}

fn count_error(message: String) -> Error {
    Error::Limit {
        code: LimitCode::TocEntryCount,
        message,
    }
}

fn stale((number, generation): (u32, u16)) -> Error {
    Error::Stale(format!(
        "outline item {number} {generation} R no longer exists"
    ))
}

fn outline_dict(dict: &Dict) -> Result<()> {
    if matches!(dict.get("Type"), Some(Value::Name(kind)) if
        matches!(kind.as_bytes(), b"Page" | b"Pages" | b"Catalog"))
    {
        return Err(Error::Malformed(
            "an outline dictionary aliases a page, page tree, or catalog".to_owned(),
        ));
    }
    Ok(())
}

/// Resolves an outline node and retains its actual object identity through aliases.
fn node(reads: &mut Reads<'_>, value: &Value) -> Result<Option<(Dict, Option<Reference>)>> {
    let mut reference = match value {
        Value::Dict(dict) => {
            outline_dict(dict)?;
            let Value::Dict(copied) = refs::copy_for_edit(value, reads.budget())? else {
                unreachable!("checked dictionary variant");
            };
            return Ok(Some((copied, None)));
        }
        Value::Reference(reference) => *reference,
        _ => return Ok(None),
    };
    for _ in 0..MAX_CHAIN {
        let Some(object) = reads
            .get(reference.number())?
            .filter(|object| object.generation() == reference.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Reference(next) => reference = *next,
            Value::Dict(dict) => {
                outline_dict(dict)?;
                let Value::Dict(copied) = refs::copy_for_edit(object.value(), reads.budget())?
                else {
                    unreachable!("checked dictionary variant");
                };
                return Ok(Some((copied, Some(reference))));
            }
            _ => return Ok(None),
        }
    }
    Err(Error::Pdf(format!(
        "a reference cycle or a chain of more than {MAX_CHAIN} references"
    )))
}

/// Returns the catalog and its object.
fn catalog(reads: &mut Reads<'_>) -> Result<Option<(Dict, Option<u32>)>> {
    let root = copy(reads.trailer().get("Root"), reads.budget())?;
    let object = last(reads, root.as_ref())?;
    Ok(match read(reads, root.as_ref())? {
        Some(Value::Dict(catalog)) => Some((catalog, object)),
        _ => None,
    })
}

/// Returns the outline's root dictionary and the object that holds it, or
/// `None` for a document without an outline.
fn root(reads: &mut Reads<'_>) -> Result<Option<(Dict, Option<u32>)>> {
    let Some((catalog, _)) = catalog(reads)? else {
        return Ok(None);
    };
    let value = catalog.get("Outlines");
    match read(reads, value)? {
        None => Ok(None),
        Some(Value::Dict(root)) => {
            outline_dict(&root)?;
            Ok(Some((root, last(reads, value)?)))
        }
        Some(_) => Err(Error::Pdf(
            "catalog Outlines is not a dictionary".to_owned(),
        )),
    }
}

/// Returns the object at the end of the references from `value`, or
/// `None` for a direct value.
fn last(reads: &mut Reads<'_>, value: Option<&Value>) -> Result<Option<u32>> {
    let Some(Value::Reference(current)) = value else {
        return Ok(None);
    };
    let mut current = *current;
    for _ in 0..MAX_CHAIN {
        let next = match reads.get(current.number())? {
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
    Err(Error::Pdf(format!(
        "a reference cycle or a chain of more than {MAX_CHAIN} references"
    )))
}

/// Returns the dictionary of item object `id`, or `None` when it is gone.
fn item(reads: &mut Reads<'_>, (number, generation): (u32, u16)) -> Result<Option<Dict>> {
    Ok(match reads.get(number)? {
        Some(object) if object.generation() == generation => match &**object.value() {
            Value::Dict(dict) => {
                outline_dict(dict)?;
                let Value::Dict(copied) = refs::copy_for_edit(object.value(), reads.budget())?
                else {
                    unreachable!("checked dictionary variant");
                };
                Some(copied)
            }
            _ => None,
        },
        _ => None,
    })
}

/// Returns the items of the `/First` and `/Next` chain of `parent` that
/// are objects, up to the first entry that is no item or that the chain
/// already listed.
fn children(reads: &mut Reads<'_>, parent: &Dict) -> Result<Vec<(u32, u16)>> {
    let budget = reads.budget();
    let mut out = BudgetVec::new(budget);
    let mut seen = HashSet::new();
    let mut next = copy(parent.get("First"), budget)?;
    let mut count = 0;
    while let Some(value) = next {
        let Some((child, reference)) = node(reads, &value)? else {
            break;
        };
        if let Some(reference) = reference
            && !visit(&mut seen, reference, budget)?
        {
            break;
        }
        count += 1;
        if count > MAX_ITEMS {
            return Err(count_error(format!(
                "outline children exceed the {MAX_ITEMS}-entry safety limit"
            )));
        }
        if let Some(reference) = reference {
            out.try_push((reference.number(), reference.generation()))?;
        }
        next = copy(child.get("Next"), budget)?;
    }
    Ok(out.into_vec())
}

/// Returns whether an item has children: whether its `/First` names an
/// item.
fn has_children(reads: &mut Reads<'_>, item_dict: &Dict) -> Result<bool> {
    match item_dict.get("First") {
        Some(value) => Ok(node(reads, value)?.is_some()),
        None => Ok(false),
    }
}

/// Returns an item's level: one more than the `/Parent` steps to the root
/// `root`, at most [`MAX_DEPTH`].
fn level(reads: &mut Reads<'_>, item_dict: &Dict, root: Option<u32>) -> Result<u32> {
    let budget = reads.budget();
    let mut level = 1;
    let mut parent = copy(item_dict.get("Parent"), budget)?;
    let mut seen = HashSet::new();
    while let Some(value) = parent {
        let Some((next, reference)) = node(reads, &value)? else {
            break;
        };
        if let Some(reference) = reference {
            if Some(reference.number()) == root {
                break;
            }
            if !visit(&mut seen, reference, budget)? {
                return Err(Error::Pdf("an outline parent cycle".to_owned()));
            }
        }
        level += 1;
        if level as usize > MAX_DEPTH {
            return Err(Error::Pdf(format!(
                "outline parent chain exceeds the {MAX_DEPTH}-level safety limit"
            )));
        }
        parent = copy(next.get("Parent"), budget)?;
    }
    Ok(level)
}

/// Walks every item of the outline in document order under the outline
/// limits. `edit` stands for the dictionary of one item, to check the text
/// that an edit of it leaves.
fn walk(reads: &mut Reads<'_>, edit: Option<((u32, u16), &Dict)>) -> Result<Outline> {
    let budget = reads.budget();
    let mut items = BudgetVec::new(budget);
    let Some((root, _)) = root(reads)? else {
        return Ok(Outline {
            items: items.into_vec(),
        });
    };
    let Some(first) = copy(root.get("First"), budget)? else {
        return Ok(Outline {
            items: items.into_vec(),
        });
    };
    let mut walker = Walker {
        reads,
        text: Text {
            input: edit.is_some(),
            ..Text::default()
        },
    };
    let mut stack = BudgetVec::new(budget);
    stack.try_push((first, 1_usize))?;
    let mut visited = HashSet::new();
    let (mut nodes, mut edges) = (0_usize, 1_usize);
    while let Some((value, depth)) = stack.pop() {
        let Some((node_dict, reference)) = node(walker.reads, &value)? else {
            continue;
        };
        let id = if let Some(reference) = reference {
            if !visit(&mut visited, reference, budget)? {
                continue;
            }
            (reference.number(), reference.generation())
        } else {
            (0, 0)
        };
        let active = match edit {
            Some((edited, dict)) if id.0 != 0 && edited == id => dict,
            _ => &node_dict,
        };
        if depth > MAX_DEPTH {
            return Err(Error::Pdf(format!(
                "outline tree exceeds the {MAX_DEPTH}-level safety limit"
            )));
        }
        nodes += 1;
        if nodes > MAX_ITEMS {
            return Err(count_error(format!(
                "outline tree exceeds the {MAX_ITEMS}-node safety limit"
            )));
        }
        let title = walker.title(active)?;
        let lead = walker.lead(active)?;
        let color = walker.color(active)?;
        let flags = walker.integer(active, "F")?;
        let count = walker.integer(active, "Count")?;
        items.try_push(Item {
            id,
            level: u32::try_from(depth).unwrap_or(u32::MAX),
            title,
            lead,
            color,
            flags,
            count,
        })?;
        for (key, child_depth) in [("Next", depth), ("First", depth + 1)] {
            let Some(next) = active.get(key) else {
                continue;
            };
            edges += 1;
            if edges > MAX_EDGES {
                return Err(count_error(format!(
                    "outline tree exceeds the {MAX_EDGES}-edge safety limit"
                )));
            }
            // `/First` goes on top, so a child comes before its next sibling.
            let next = refs::copy_for_edit(next, budget)?;
            stack.try_push((next, child_depth))?;
        }
    }
    Ok(Outline {
        items: items.into_vec(),
    })
}

/// Returns the top-level items of the outline, or the children of item
/// `parent`.
pub fn outline_children(
    version: &Version,
    budget: &Budget,
    parent: Option<(u32, u16)>,
) -> Result<Vec<(u32, u16)>> {
    let mut reads = Reads::new(version, budget);
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
pub fn outline_item(
    version: &Arc<Version>,
    index: &PageIndex,
    destinations: &Memo<(), NamedDestinations>,
    budget: &Budget,
    id: (u32, u16),
) -> Result<ItemRecord> {
    let mut reads = Reads::new(version, budget);
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
    let target = Places::new(destinations, version, index, budget).target(&lead)?;
    Ok((level, title, target, color, flags, count, children))
}

/// Returns every item of the outline in document order.
pub fn outline_entries(
    outline_memo: &Memo<(), Outline>,
    pages_memo: &Memo<PageTree, PageIndex>,
    destinations_memo: &Memo<(), NamedDestinations>,
    version: &Arc<Version>,
    budget: &Budget,
) -> Result<Vec<EntryRecord>> {
    let index = PageIndex::memoized(pages_memo, version, budget)?;
    let outline = Outline::memoized(outline_memo, version, budget)?;
    let mut places = Places::new(destinations_memo, version, &index, budget);
    let mut entries = BudgetVec::new(budget);
    for item in &outline.items {
        memory(budget, item.title.len())?;
        entries.try_push((
            item.id,
            item.level,
            item.title.clone(),
            places.target(&item.lead)?,
            item.color,
            item.flags,
            item.count,
        ))?;
    }
    Ok(entries.into_vec())
}

/// Returns how many outline walks the document's memos reused and how many
/// they computed, for tests.
#[must_use]
pub fn outline_walk_counts(
    outline: &Memo<(), Outline>,
    draft_outline: &Memo<(), Outline>,
) -> (u64, u64) {
    let (outline, drafts) = (outline.stats(), draft_outline.stats());
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
    fn charge(&mut self, text: &str, label: &str) -> Result<()> {
        self.utf8 = self.utf8.saturating_add(text.len());
        if self.utf8 > MAX_TEXT_BYTES {
            return Err(Error::Limit {
                code: LimitCode::TocInputSize,
                message: format!(
                    "outline {label} input exceeds the {MAX_TEXT_BYTES}-byte safety limit"
                ),
            });
        }
        let encoded = if text.is_ascii() {
            text.len()
        } else {
            2 + 2 * text.encode_utf16().count()
        };
        self.encoded = self.encoded.saturating_add(encoded);
        if self.encoded > MAX_TEXT_BYTES {
            return Err(Error::Limit {
                code: LimitCode::TocInputSize,
                message: format!(
                    "encoded outline {label} input exceeds the {MAX_TEXT_BYTES}-byte encoded-text safety limit"
                ),
            });
        }
        Ok(())
    }

    fn target(&mut self, target: &Target) -> Result<()> {
        for text in [&target.4, &target.5].into_iter().flatten() {
            self.charge(text, "target")?;
        }
        Ok(())
    }
}

/// Returns a color as a `/C` array, refusing components outside 0 to 1.
#[expect(
    clippy::disallowed_methods,
    reason = "a color contains exactly three components"
)]
fn color_value((red, green, blue): Color) -> Result<Value> {
    let mut components = Vec::with_capacity(3);
    for value in [red, green, blue] {
        if !(value.is_finite() && (0.0..=1.0).contains(&value)) {
            return Err(Error::Pdf(format!(
                "outline color components must be from 0 to 1: {value}"
            )));
        }
        components.push(number(value));
    }
    Ok(Value::Array(Array::from(components)))
}

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
#[expect(
    clippy::disallowed_methods,
    reason = "inserts only the single fixed destination or action key produced by target_entry"
)]
pub fn outline_write(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    references_cache: &refs::Cache,
    budget: &Budget,
    specs: Vec<Spec>,
) -> Result<()> {
    if specs.len() > MAX_ITEMS {
        return Err(count_error(format!(
            "cannot write more than {MAX_ITEMS} outline items"
        )));
    }
    with_savepoint(transaction, |transaction| {
        let before = transaction.snapshot();
        let index = PageIndex::memoized(draft_pages, &before, budget)?;
        memory(budget, specs.len().saturating_mul(size_of::<Spec>()))?;
        let mut input = Input::default();
        let mut prepared = BudgetVec::new(budget);
        let mut previous = 0;
        for (level, title, target, color, flags, open) in &specs {
            let level = *level as usize;
            if level == 0 || level > previous + 1 {
                return Err(Error::Pdf(format!(
                    "invalid outline level {level}; levels start at 1 and can increase by at most one"
                )));
            }
            if level > MAX_DEPTH {
                return Err(Error::Pdf(format!(
                    "outline depth exceeds the {MAX_DEPTH}-level safety limit"
                )));
            }
            input.charge(title, "title")?;
            input.target(target)?;
            memory(budget, title.len().saturating_mul(4).saturating_add(4096))?;
            for text in [&target.4, &target.5].into_iter().flatten() {
                memory(budget, text.len().saturating_mul(4))?;
            }
            prepared.try_push(Prepared {
                level,
                title: encode_text(title),
                entry: target_entry(&index, target)?,
                color: color.map(color_value).transpose()?,
                flags: *flags,
                open: *open,
            })?;
            previous = level;
        }
        drop(specs);
        let count = prepared.len();
        let mut parent = filled(count, None, budget)?;
        let mut first = filled(count, None, budget)?;
        let mut last_child = filled(count, None, budget)?;
        let mut next = filled(count, None, budget)?;
        let mut previous_sibling = filled(count, None, budget)?;
        let mut top: (Option<usize>, Option<usize>) = (None, None);
        let mut chain: BudgetVec<'_, usize> = BudgetVec::new(budget);
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
            chain.try_push(at)?;
        }
        // An item comes before its descendants, so a reverse pass finishes
        // each item's count before its parent adds it.
        let mut visible = filled(count, 0_i64, budget)?;
        let mut root_visible = 0_i64;
        for at in (0..count).rev() {
            budget.poll()?;
            let shown = 1 + if prepared[at].open { visible[at] } else { 0 };
            match parent[at] {
                Some(owner) => visible[owner] += shown,
                None => root_visible += shown,
            }
        }

        let mut reads = Reads::new(&before, budget);
        let Some((mut catalog_dict, Some(catalog_object))) = catalog(&mut reads)? else {
            return Err(Error::Pdf("the catalog is not an object".to_owned()));
        };
        let old = objects(&mut reads, &catalog_dict)?;
        catalog_dict.remove("Outlines");
        if count > 0 {
            let root = transaction.allocate()?;
            let mut ids = BudgetVec::new(budget);
            ids.try_reserve_exact(count)?;
            for _ in 0..count {
                ids.try_push(transaction.allocate()?)?;
            }
            for (at, item) in prepared.into_vec().into_iter().enumerate() {
                let mut dict = Dict::new();
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /Title key"
                )]
                dict.insert(Name::from("Title"), item.title);
                let owner = parent[at].map_or(root, |owner| ids[owner]);
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /Parent key"
                )]
                dict.insert(Name::from("Parent"), Value::Reference(owner));
                if let Some(before) = previous_sibling[at] {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /Prev key"
                    )]
                    dict.insert(Name::from("Prev"), Value::Reference(ids[before]));
                }
                if let Some(after) = next[at] {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /Next key"
                    )]
                    dict.insert(Name::from("Next"), Value::Reference(ids[after]));
                }
                if let (Some(child), Some(final_child)) = (first[at], last_child[at]) {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /First key"
                    )]
                    dict.insert(Name::from("First"), Value::Reference(ids[child]));
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /Last key"
                    )]
                    dict.insert(Name::from("Last"), Value::Reference(ids[final_child]));
                    let shown = if item.open { visible[at] } else { -visible[at] };
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /Count key"
                    )]
                    dict.insert(Name::from("Count"), Value::Integer(shown));
                }
                if let Some((key, value)) = item.entry {
                    dict.insert(Name::from(key), value);
                }
                if let Some(color) = item.color {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /C key"
                    )]
                    dict.insert(Name::from("C"), color);
                }
                if item.flags != 0 {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /F key"
                    )]
                    dict.insert(Name::from("F"), Value::Integer(i64::from(item.flags)));
                }
                transaction.write(ids[at].number(), &Value::Dict(dict), budget)?;
            }
            if let (Some(child), Some(final_child)) = top {
                let mut dict = Dict::new();
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /Type key"
                )]
                dict.insert(Name::from("Type"), Value::Name(Name::from("Outlines")));
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /First key"
                )]
                dict.insert(Name::from("First"), Value::Reference(ids[child]));
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /Last key"
                )]
                dict.insert(Name::from("Last"), Value::Reference(ids[final_child]));
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /Count key"
                )]
                dict.insert(Name::from("Count"), Value::Integer(root_visible));
                transaction.write(root.number(), &Value::Dict(dict), budget)?;
            }
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Outlines key"
            )]
            catalog_dict.insert(Name::from("Outlines"), Value::Reference(root));
        }
        transaction.write(catalog_object, &Value::Dict(catalog_dict), budget)?;
        refs::free_unreferenced(references_cache, transaction, &[], &old, budget)?;
        Ok(())
    })
}

/// Returns the objects of the outline of `catalog`: the objects to its
/// root, and the items that `/First` and `/Next` reach, at most
/// [`MAX_ITEMS`] of them.
fn objects(reads: &mut Reads<'_>, catalog: &Dict) -> Result<Vec<u32>> {
    let budget = reads.budget();
    let mut out = BudgetVec::new(budget);
    let mut stack = BudgetVec::new(budget);
    if let Some(root) = copy(catalog.get("Outlines"), budget)? {
        stack.try_push((root, 0_usize, 0_usize))?;
    }
    let mut visited = HashSet::new();
    let (mut nodes, mut edges) = (0, 0);
    while let Some((value, depth, chain)) = stack.pop() {
        let dict = match value {
            Value::Reference(reference) => {
                if !visit(&mut visited, reference, budget)? {
                    continue;
                }
                if chain >= MAX_CHAIN {
                    return Err(Error::Pdf(format!(
                        "an outline reference chain exceeds {MAX_CHAIN}"
                    )));
                }
                let Some(object) = reads
                    .get(reference.number())?
                    .filter(|object| object.generation() == reference.generation())
                else {
                    continue;
                };
                out.try_push(reference.number())?;
                if let Some(value) = copy(Some(object.value()), budget)? {
                    stack.try_push((value, depth, chain + 1))?;
                }
                continue;
            }
            Value::Dict(dict) => dict,
            _ => continue,
        };
        outline_dict(&dict)?;
        nodes += usize::from(depth > 0);
        if nodes > MAX_ITEMS {
            return Err(count_error(format!(
                "outline tree exceeds the {MAX_ITEMS}-node safety limit"
            )));
        }
        if depth > MAX_DEPTH {
            return Err(Error::Pdf(format!(
                "outline tree exceeds the {MAX_DEPTH}-level safety limit"
            )));
        }
        for (key, depth) in [("Next", depth), ("First", depth + 1)] {
            if let Some(value) = copy(dict.get(key), budget)? {
                edges += 1;
                if edges > MAX_EDGES {
                    return Err(count_error(format!(
                        "outline tree exceeds the {MAX_EDGES}-edge safety limit"
                    )));
                }
                stack.try_push((value, depth, 0))?;
            }
        }
    }
    Ok(out.into_vec())
}

fn edit_item(
    transaction: &mut Transaction,
    budget: &Budget,
    id: (u32, u16),
    text: bool,
    change: impl FnOnce(&mut Dict, &Arc<Version>, &Budget) -> Result<()>,
) -> Result<()> {
    with_savepoint(transaction, |transaction| {
        let before = transaction.snapshot();
        let mut reads = Reads::new(&before, budget);
        let mut dict = item(&mut reads, id)?.ok_or_else(|| stale(id))?;
        change(&mut dict, &before, budget)?;
        if text {
            walk(&mut reads, Some((id, &dict)))?;
        }
        transaction.write(id.0, &Value::Dict(dict), budget)?;
        Ok(())
    })
}

/// Sets the title of item `id`.
pub fn outline_set_title(
    transaction: &mut Transaction,
    budget: &Budget,
    id: (u32, u16),
    title: &str,
) -> Result<()> {
    Input::default().charge(title, "title")?;
    edit_item(transaction, budget, id, true, |dict, _, budget| {
        memory(budget, title.len().saturating_mul(3))?;
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Title key"
        )]
        dict.insert(Name::from("Title"), encode_text(title));
        Ok(())
    })
}

/// Makes item `id` lead to `target`.
#[expect(
    clippy::disallowed_methods,
    reason = "inserts only the single destination or action key produced by target_entry"
)]
pub fn outline_set_target(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    id: (u32, u16),
    target: &Target,
) -> Result<()> {
    Input::default().target(target)?;
    edit_item(transaction, budget, id, true, |dict, version, budget| {
        let index = PageIndex::memoized(draft_pages, version, budget)?;
        for text in [&target.4, &target.5].into_iter().flatten() {
            memory(budget, text.len().saturating_mul(3))?;
        }
        memory(budget, 4096)?;
        let entry = target_entry(&index, target)?;
        dict.remove("Dest");
        dict.remove("A");
        if let Some((key, value)) = entry {
            dict.insert(Name::from(key), value);
        }
        Ok(())
    })
}

/// Sets the color and the `/F` flags of item `id`.
#[expect(
    clippy::disallowed_methods,
    reason = "inserts only the fixed color key for the item"
)]
pub fn outline_set_style(
    transaction: &mut Transaction,
    budget: &Budget,
    id: (u32, u16),
    color: Option<Color>,
    flags: Option<u32>,
    flag: Option<(u32, bool)>,
) -> Result<()> {
    edit_item(transaction, budget, id, false, |dict, version, budget| {
        if flag.is_none() {
            memory(budget, 3 * size_of::<Value>())?;
            match color.map(color_value).transpose()? {
                Some(color) => dict.insert(Name::from("C"), color),
                None => dict.remove("C"),
            };
        }
        let flags = if let Some((mask, set)) = flag {
            let mut reads = Reads::new(version, budget);
            let current = match read(&mut reads, dict.get("F"))? {
                Some(Value::Integer(flags)) => flags,
                _ => 0,
            };
            Some(if set {
                current | i64::from(mask)
            } else {
                current & !i64::from(mask)
            })
        } else {
            flags.map(i64::from)
        };
        if let Some(flags) = flags {
            if flags == 0 {
                dict.remove("F");
            } else {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /F key"
                )]
                dict.insert(Name::from("F"), Value::Integer(flags));
            }
        }
        Ok(())
    })
}

/// Shows or hides the children of item `id`, recounting the outline (ISO
/// 32000-2 Table 151). Use [`CountCache::set_open`] to reuse a verified recount
/// for subsequent parent-chain updates.
pub fn outline_set_open(
    transaction: &mut Transaction,
    budget: &Budget,
    id: (u32, u16),
    open: bool,
) -> Result<()> {
    CountCache::default().set_open(transaction, budget, id, open)
}

fn set_open(
    cache: &CountCache,
    transaction: &mut Transaction,
    budget: &Budget,
    id: (u32, u16),
    open: bool,
) -> Result<()> {
    with_savepoint(transaction, |transaction| {
        let before = transaction.snapshot();
        let mut reads = Reads::new(&before, budget);
        let dict = item(&mut reads, id)?.ok_or_else(|| stale(id))?;
        if !has_children(&mut reads, &dict)? {
            return Err(Error::Pdf(
                "an outline item without children has no open state".to_owned(),
            ));
        }
        let Some((root, root_object)) = root(&mut reads)? else {
            return Ok(());
        };
        let location = match root_object {
            Some(number) => Location {
                id: (number, generation(&mut reads, number)?),
                path: Vec::new(),
            },
            None => {
                let Some((_, Some(number))) = catalog(&mut reads)? else {
                    return Err(Error::Pdf("the catalog is not an object".to_owned()));
                };
                let mut path = BudgetVec::new(budget);
                path.try_push("Outlines")?;
                Location {
                    id: (number, generation(&mut reads, number)?),
                    path: path.into_vec(),
                }
            }
        };
        let verified = cache.get(&before, budget)?;
        let delta = if verified
            .as_ref()
            .is_some_and(|verified| verified.members.binary_search(&id).is_ok())
        {
            delta_counts(&mut reads, &dict, &location, id, open)?
        } else {
            None
        };
        let (updates, verified) = match delta {
            Some(updates) => (updates, verified),
            None => {
                let plan = counts(&mut reads, &root, location, (id, open))?;
                let verified = if let Some(members) = plan.members {
                    memory(budget, size_of::<Verified>() + 2 * size_of::<usize>())?;
                    Some(Arc::new(Verified {
                        reads: reads.finish()?,
                        members,
                    }))
                } else {
                    None
                };
                (plan.updates, verified)
            }
        };
        for (location, count) in updates {
            // Read previous writes from this operation, so several direct nodes
            // in one holder cannot overwrite each other's updated counts.
            let Some(object) = transaction
                .version()
                .get(location.id.0, budget)?
                .filter(|object| object.generation() == location.id.1)
            else {
                return Err(stale(location.id));
            };
            let mut value = refs::copy_for_edit(object.value(), budget)?;
            let mut current = &mut value;
            for key in &location.path {
                budget.poll()?;
                let Value::Dict(dict) = current else {
                    return Err(Error::Pdf(
                        "an outline count path is not a dictionary".to_owned(),
                    ));
                };
                current = dict
                    .get_mut(key)
                    .ok_or_else(|| Error::Pdf("an outline count path is missing".to_owned()))?;
            }
            let Value::Dict(dict) = current else {
                return Err(Error::Pdf("an outline item is not a dictionary".to_owned()));
            };
            if dict.get("Count") != Some(&Value::Integer(count)) {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /Count key"
                )]
                dict.insert(Name::from("Count"), Value::Integer(count));
                transaction.write(location.id.0, &value, budget)?;
            }
        }
        if let Some(verified) = verified {
            // The recount or verified delta establishes the same invariant in
            // the resulting version. Keep all of its original dependencies.
            let after = transaction.snapshot();
            cache.put(&after, verified);
            transaction.retain_derived_version(after);
        }
        Ok(())
    })
}

/// Resolves an indirect reference (through up to [`MAX_CHAIN`] aliases) to an
/// outline dictionary object and its resolved [`Reference`], without copying
/// the dictionary.
fn indirect_dict(
    reads: &mut Reads<'_>,
    mut reference: Reference,
) -> Result<Option<(Object, Reference)>> {
    for _ in 0..MAX_CHAIN {
        let Some(object) = reads
            .get(reference.number())?
            .filter(|object| object.generation() == reference.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Reference(next) => reference = *next,
            Value::Dict(dict) => {
                outline_dict(dict)?;
                return Ok(Some((object, reference)));
            }
            _ => return Ok(None),
        }
    }
    Err(Error::Pdf(format!(
        "a reference cycle or a chain of more than {MAX_CHAIN} references"
    )))
}

/// Computes `O(depth)` `/Count` updates along `id`'s `/Parent` chain when `id`
/// and every ancestor up to `root_location` have already been verified by a
/// full recount. The local guards alone cannot establish consistency.
/// Returns `Ok(None)` when any link or count is missing,
/// cyclic, deeper than [`MAX_DEPTH`], or inconsistent, falling back to [`counts`].
fn delta_counts(
    reads: &mut Reads<'_>,
    dict: &Dict,
    root_location: &Location,
    id: (u32, u16),
    open: bool,
) -> Result<Option<Vec<Count>>> {
    let max_items = i64::try_from(MAX_ITEMS).unwrap_or(i64::MAX);
    if !root_location.path.is_empty() || root_location.id == id {
        return Ok(None);
    }
    if matches!(dict.get("Type"), Some(Value::Name(kind)) if kind.as_bytes() == b"Outlines") {
        return Ok(None);
    }
    let Some(&Value::Integer(old_count)) = dict.get("Count") else {
        return Ok(None);
    };
    let Some(magnitude) = old_count.checked_abs() else {
        return Ok(None);
    };
    if magnitude == 0 || magnitude > max_items {
        return Ok(None);
    }
    let Some(&Value::Reference(mut parent)) = dict.get("Parent") else {
        return Ok(None);
    };

    let budget = reads.budget();
    let was_open = old_count > 0;
    let mut updates = BudgetVec::new(budget);
    let delta = if was_open == open {
        0
    } else {
        let signed = if open { magnitude } else { -magnitude };
        updates.try_push((
            Location {
                id,
                path: Vec::new(),
            },
            signed,
        ))?;
        signed
    };

    let mut seen = HashSet::new();
    visit(&mut seen, id, budget)?;
    let mut level = 1_usize;
    let mut steps = 0_i64;
    let mut propagating = true;

    loop {
        budget.poll()?;
        steps += 1;
        let Some((anc_obj, reference)) = indirect_dict(reads, parent)? else {
            return Ok(None);
        };
        let anc_id = (reference.number(), reference.generation());
        if !visit(&mut seen, anc_id, budget)? {
            return Ok(None);
        }
        let Value::Dict(anc_dict) = &**anc_obj.value() else {
            unreachable!("indirect_dict returns a dictionary object");
        };
        let Some(&Value::Integer(anc_count)) = anc_dict.get("Count") else {
            return Ok(None);
        };

        let min_before = if propagating {
            steps + if was_open { magnitude } else { 0 }
        } else {
            1
        };

        if anc_id == root_location.id {
            if anc_count < min_before || anc_count > max_items {
                return Ok(None);
            }
            if propagating && delta != 0 {
                let new_root = anc_count + delta;
                if new_root < 1 || new_root > max_items {
                    return Ok(None);
                }
                updates.try_push((
                    Location {
                        id: anc_id,
                        path: Vec::new(),
                    },
                    new_root,
                ))?;
            }
            return Ok(Some(updates.into_vec()));
        }

        level += 1;
        if level > MAX_DEPTH {
            return Ok(None);
        }
        if matches!(anc_dict.get("Type"), Some(Value::Name(kind)) if kind.as_bytes() == b"Outlines")
        {
            return Ok(None);
        }
        if anc_dict.get("First").is_none() {
            return Ok(None);
        }
        let Some(anc_mag) = anc_count.checked_abs() else {
            return Ok(None);
        };
        if anc_mag < min_before || anc_mag > max_items {
            return Ok(None);
        }

        if propagating {
            if anc_count > 0 {
                if delta != 0 {
                    let new_anc = anc_count + delta;
                    if new_anc < 1 || new_anc > max_items {
                        return Ok(None);
                    }
                    updates.try_push((
                        Location {
                            id: anc_id,
                            path: Vec::new(),
                        },
                        new_anc,
                    ))?;
                }
            } else {
                if delta != 0 {
                    let new_mag = anc_mag + delta;
                    if new_mag < 1 || new_mag > max_items {
                        return Ok(None);
                    }
                    updates.try_push((
                        Location {
                            id: anc_id,
                            path: Vec::new(),
                        },
                        -new_mag,
                    ))?;
                }
                propagating = false;
            }
        }

        let Some(&Value::Reference(next_parent)) = anc_dict.get("Parent") else {
            return Ok(None);
        };
        parent = next_parent;
    }
}

fn generation(reads: &mut Reads<'_>, number: u32) -> Result<u16> {
    Ok(reads.get(number)?.map_or(0, |object| object.generation()))
}

struct Location {
    id: (u32, u16),
    path: Vec<&'static str>,
}

impl Location {
    fn child(&self, key: &'static str, budget: &Budget) -> Result<Self> {
        let mut path = BudgetVec::new(budget);
        path.try_reserve_exact(self.path.len() + 1)?;
        path.try_extend_from_slice(&self.path)?;
        path.try_push(key)?;
        Ok(Self {
            id: self.id,
            path: path.into_vec(),
        })
    }
}

type Count = (Location, i64);
type PendingCount = (Value, Location, Option<usize>, usize);

struct CountPlan {
    updates: Vec<Count>,
    /// Only canonical indirect trees may reuse the parent-chain shortcut.
    members: Option<Vec<(u32, u16)>>,
}

/// Counts direct and indirect nodes alike, while only an indirect identity
/// can select the changed item. Every count retains its exact write location.
fn counts(
    reads: &mut Reads<'_>,
    root: &Dict,
    root_location: Location,
    change: ((u32, u16), bool),
) -> Result<CountPlan> {
    let budget = reads.budget();
    let mut order: BudgetVec<'_, (Location, Option<usize>, bool)> = BudgetVec::new(budget);
    let mut stack: BudgetVec<'_, PendingCount> = BudgetVec::new(budget);
    let mut visited = HashSet::new();
    let mut cacheable = root_location.path.is_empty();
    let mut members = BudgetVec::new(budget);
    if root_location.path.is_empty() {
        visit(&mut visited, root_location.id, budget)?;
    }
    if let Some(first) = copy(root.get("First"), budget)? {
        let location = root_location.child("First", budget)?;
        stack.try_push((first, location, None, 1))?;
    }
    let mut edges = stack.len();
    while let Some((value, mut location, parent, depth)) = stack.pop() {
        let Some((dict, reference)) = node(reads, &value)? else {
            cacheable = false;
            continue;
        };
        if let Some(reference) = reference {
            let id = (reference.number(), reference.generation());
            if !visit(&mut visited, id, budget)? {
                cacheable = false;
                continue;
            }
            location = Location {
                id,
                path: Vec::new(),
            };
            members.try_push(id)?;
        } else {
            cacheable = false;
        }
        let expected_parent = parent.map_or(root_location.id, |owner| order[owner].0.id);
        if !matches!(dict.get("Parent"), Some(Value::Reference(reference))
            if (reference.number(), reference.generation()) == expected_parent)
        {
            cacheable = false;
        }
        if order.len() >= MAX_ITEMS {
            return Err(count_error(format!(
                "outline tree exceeds the {MAX_ITEMS}-node safety limit"
            )));
        }
        if depth > MAX_DEPTH {
            return Err(Error::Pdf(format!(
                "outline tree exceeds the {MAX_DEPTH}-level safety limit"
            )));
        }
        let open = if location.path.is_empty() && location.id == change.0 {
            change.1
        } else {
            matches!(read(reads, dict.get("Count"))?, Some(Value::Integer(count)) if count > 0)
        };
        let position = order.len();
        for (key, owner, depth) in [
            ("Next", parent, depth),
            ("First", Some(position), depth + 1),
        ] {
            if let Some(value) = copy(dict.get(key), budget)? {
                edges += 1;
                if edges > MAX_EDGES {
                    return Err(count_error(format!(
                        "outline tree exceeds the {MAX_EDGES}-edge safety limit"
                    )));
                }
                let child = location.child(key, budget)?;
                stack.try_push((value, child, owner, depth))?;
            }
        }
        order.try_push((location, parent, open))?;
    }
    let mut visible = filled(order.len(), 0_i64, budget)?;
    let mut has_children = filled(order.len(), false, budget)?;
    let mut root_visible = 0_i64;
    for at in (0..order.len()).rev() {
        budget.poll()?;
        let (_, parent, open) = &order[at];
        let shown = 1 + if *open { visible[at] } else { 0 };
        match *parent {
            Some(owner) => {
                visible[owner] += shown;
                has_children[owner] = true;
            }
            None => root_visible += shown,
        }
    }
    let mut counts = BudgetVec::new(budget);
    for (at, (location, _, open)) in order.into_vec().into_iter().enumerate() {
        if has_children[at] {
            counts.try_push((location, if open { visible[at] } else { -visible[at] }))?;
        }
    }
    counts.try_push((root_location, root_visible))?;
    let mut members = members.into_vec();
    members.sort_unstable();
    Ok(CountPlan {
        updates: counts.into_vec(),
        members: cacheable.then_some(members),
    })
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;
    use pyropdf_store::budget::{CancellationToken, DocumentLimits};
    use pyropdf_store::index::Index;
    use pyropdf_store::save::FileBase;
    use pyropdf_store::value::Bytes;
    use pyropdf_store::version::Document;

    use crate::navigation::none;

    use super::*;

    fn minimal_doc() -> Document {
        let objects = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        ];
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

    fn root_and_item_counts(version: &Arc<Version>, budget: &Budget) -> (i64, Vec<Option<i64>>) {
        let mut reads = Reads::new(version, budget);
        let (root_dict, _) = root(&mut reads).unwrap().expect("outline root exists");
        let Some(&Value::Integer(root_count)) = root_dict.get("Count") else {
            panic!("missing integer /Outlines /Count");
        };
        let outline = walk(&mut reads, None).unwrap();
        let mut item_counts = Vec::with_capacity(outline.items.len());
        for item in &outline.items {
            let obj = version.get(item.id.0, budget).unwrap().unwrap();
            let Value::Dict(d) = &**obj.value() else {
                panic!("item is dict");
            };
            let count = match d.get("Count") {
                Some(&Value::Integer(c)) => Some(c),
                None => None,
                other => panic!("unexpected /Count: {other:?}"),
            };
            item_counts.push(count);
        }
        (root_count, item_counts)
    }

    fn commit_with_counts(
        transaction: Transaction,
        cache: &CountCache,
        budget: &Budget,
    ) -> pyropdf_store::Result<Arc<Version>> {
        let anchor = transaction.derived_anchor().cloned();
        let committed = transaction.commit(budget)?;
        cache.carry_to(&committed, budget);
        drop(anchor);
        Ok(committed)
    }

    #[test]
    fn delta_counts_reads_and_writes_in_o_depth_and_stops_above_closed_ancestors() {
        let doc = minimal_doc();
        let budget = Budget::default();
        let cache = CountCache::default();
        let pages_memo = Memo::new(1);
        let outline_memo = Memo::new(1);
        let dests_memo = Memo::new(1);
        let refs_cache = refs::Cache::new();

        // Build 1,024 items across 4 levels: 16 parts * (1 + 7 chapters * (1 + 2 sections * (1 + 3 leaves))) = 16 * (1 + 7 * 9) = 1,024.
        let mut specs: Vec<Spec> = Vec::with_capacity(1024);
        for p in 0..16 {
            specs.push((1, format!("P{p}"), none(), None, 0, true));
            for c in 0..7 {
                specs.push((2, format!("C{p}.{c}"), none(), None, 0, true));
                for s in 0..2 {
                    specs.push((3, format!("S{p}.{c}.{s}"), none(), None, 0, true));
                    for l in 0..3 {
                        specs.push((4, format!("L{p}.{c}.{s}.{l}"), none(), None, 0, false));
                    }
                }
            }
        }
        assert_eq!(specs.len(), 1024);

        let mut tx = doc.begin();
        outline_write(&mut tx, &pages_memo, &refs_cache, &budget, specs).unwrap();
        let base_v = commit_with_counts(tx, &cache, &budget).unwrap();

        let entries =
            outline_entries(&outline_memo, &pages_memo, &dests_memo, &base_v, &budget).unwrap();
        let part0 = entries[0].0;
        let chap0 = entries[1].0;
        let sec0 = entries[2].0;

        let mut probe_reads = Reads::new(&base_v, &budget);
        let (_, Some(root_num)) = root(&mut probe_reads).unwrap().unwrap() else {
            panic!("indirect outline root");
        };
        let root_loc = Location {
            id: (root_num, 0),
            path: Vec::new(),
        };
        let sec0_dict = item(&mut probe_reads, sec0).unwrap().unwrap();

        // Direct `delta_counts` probe on a 1,024-item outline reads only the 3 ancestors along `/Parent`.
        let mut fast_reads = Reads::new(&base_v, &budget);
        let fast = delta_counts(&mut fast_reads, &sec0_dict, &root_loc, sec0, false)
            .unwrap()
            .expect("well-formed outline uses fast path");
        assert_eq!(fast.len(), 4); // sec0, chap0, part0, root
        let read_set = fast_reads.finish().unwrap();
        assert_eq!(read_set.objects().len(), 3); // chap0, part0, root

        // Unverified counts must not use the shortcut even when their numbers
        // look plausible. A budget too small for the walk refuses atomically.
        let cold_budget =
            Budget::new(&DocumentLimits::default()).with_limit(LimitCode::MemorySize, Some(16_384));
        let mut tx = doc.begin();
        let err = cache
            .set_open(&mut tx, &cold_budget, sec0, false)
            .unwrap_err();
        assert_eq!(err.limit_code(), Some(LimitCode::MemorySize));
        assert!(
            tx.snapshot()
                .changes(&base_v, &budget)
                .unwrap()
                .unwrap()
                .is_empty()
        );
        assert!(cache.get(&tx.snapshot(), &budget).unwrap().is_none());
        drop(tx);

        let token = CancellationToken::new();
        token.cancel();
        let cancelled = Budget::default().with_cancellation(token);
        let mut tx = doc.begin();
        let err = cache
            .set_open(&mut tx, &cancelled, sec0, false)
            .unwrap_err();
        assert_eq!(err.limit_code(), Some(LimitCode::Cancelled));
        assert!(
            tx.snapshot()
                .changes(&base_v, &budget)
                .unwrap()
                .unwrap()
                .is_empty()
        );
        assert!(cache.get(&tx.snapshot(), &budget).unwrap().is_none());
        drop(tx);

        // 1. No-op (`sec0` is already open) writes zero objects and reuses `base_v`.
        let mut tx = doc.begin();
        cache.set_open(&mut tx, &budget, sec0, true).unwrap();
        let v_noop = commit_with_counts(tx, &cache, &budget).unwrap();
        assert!(cache.get(&v_noop, &budget).unwrap().is_some());
        assert!(Arc::ptr_eq(&v_noop, &base_v));
        assert!(
            v_noop
                .changes(&base_v, &budget)
                .unwrap()
                .unwrap()
                .is_empty()
        );

        // 2. Collapse `part0` (level 1): writes only `part0` and `root_num`.
        let mut tx = doc.begin();
        cache.set_open(&mut tx, &budget, part0, false).unwrap();
        let v_part_closed = commit_with_counts(tx, &cache, &budget).unwrap();
        let mut expected = vec![part0.0, root_num];
        expected.sort_unstable();
        assert_eq!(v_part_closed.written().objects(), expected.as_slice());

        // 3. Collapse `sec0` (level 3) while `chap0` (level 2) is open and `part0` (level 1) is closed:
        //    updates `sec0`, `chap0`, and `part0` (negative magnitude), stopping BEFORE `root_num`.
        let mut tx = doc.begin();
        cache.set_open(&mut tx, &budget, sec0, false).unwrap();
        let v_under_closed_part = commit_with_counts(tx, &cache, &budget).unwrap();
        let mut expected = vec![sec0.0, chap0.0, part0.0];
        expected.sort_unstable();
        assert_eq!(v_under_closed_part.written().objects(), expected.as_slice());
        assert!(!v_under_closed_part.written().contains(root_num));

        // 4. Collapse `chap0` (level 2), then re-open `sec0` (level 3):
        //    now `chap0` is closed, so propagation stops at `chap0` without writing `part0` or `root_num`.
        let mut tx = doc.begin();
        cache.set_open(&mut tx, &budget, chap0, false).unwrap();
        commit_with_counts(tx, &cache, &budget).unwrap();

        let mut tx = doc.begin();
        cache.set_open(&mut tx, &budget, sec0, true).unwrap();
        let v_under_closed_chap = commit_with_counts(tx, &cache, &budget).unwrap();
        let mut expected = vec![sec0.0, chap0.0];
        expected.sort_unstable();
        assert_eq!(v_under_closed_chap.written().objects(), expected.as_slice());
        assert!(!v_under_closed_chap.written().contains(part0.0));
        assert!(!v_under_closed_chap.written().contains(root_num));

        // 5. Budget charging: a 16 KiB memory budget succeeds on the 1,024-item outline
        //    (whereas full `counts()` requires > 32 KiB), while a 1-byte budget refuses.
        let small_budget =
            Budget::new(&DocumentLimits::default()).with_limit(LimitCode::MemorySize, Some(16_384));
        let mut tx = doc.begin();
        cache.set_open(&mut tx, &small_budget, sec0, false).unwrap();
        commit_with_counts(tx, &cache, &small_budget).unwrap();

        let tiny_budget =
            Budget::new(&DocumentLimits::default()).with_limit(LimitCode::MemorySize, Some(1));
        let mut tx = doc.begin();
        let err = cache
            .set_open(&mut tx, &tiny_budget, sec0, true)
            .unwrap_err();
        assert_eq!(err.limit_code(), Some(LimitCode::MemorySize));
        assert!(
            tx.snapshot()
                .changes(&doc.current(), &budget)
                .unwrap()
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn verified_counts_are_document_local_and_do_not_retain_versions() {
        let budget = Budget::default();
        let cache = CountCache::default();
        let doc = minimal_doc();
        let mut tx = doc.begin();
        outline_write(
            &mut tx,
            &Memo::new(1),
            &refs::Cache::new(),
            &budget,
            vec![
                (1, "Parent".into(), none(), None, 0, true),
                (2, "Child".into(), none(), None, 0, false),
            ],
        )
        .unwrap();
        let base = tx.commit(&budget).unwrap();
        let mut reads = Reads::new(&base, &budget);
        let outline = walk(&mut reads, None).unwrap();
        let id = outline.items[0].id;
        let mut tx = doc.begin();
        cache.set_open(&mut tx, &budget, id, false).unwrap();
        let verified = commit_with_counts(tx, &cache, &budget).unwrap();
        assert!(cache.get(&verified, &budget).unwrap().is_some());
        let other = minimal_doc();
        assert!(cache.get(&other.current(), &budget).unwrap().is_none());
        let weak = Arc::downgrade(&verified);
        drop(verified);
        drop(doc);
        assert!(weak.upgrade().is_none());
    }

    type OutlinePlan = (Vec<(u32, bool)>, Vec<(usize, bool)>);

    fn outline_strategy() -> impl Strategy<Value = OutlinePlan> {
        proptest::collection::vec((0_u8..=2, any::<bool>()), 2..=32).prop_flat_map(|raw| {
            let mut nodes = Vec::with_capacity(raw.len());
            let mut prev_level = 0_u32;
            for (step, open) in raw {
                let level = match step {
                    0 => 1,
                    1 => prev_level.max(1),
                    _ => (prev_level + 1).min(5),
                };
                nodes.push((level, open));
                prev_level = level;
            }
            let len = nodes.len();
            (
                Just(nodes),
                proptest::collection::vec((0..len, any::<bool>()), 1..=24),
            )
        })
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(48))]

        #[test]
        fn outline_set_open_matches_outline_write_across_multi_level_toggles(
            (nodes, toggles) in outline_strategy()
        ) {
            let doc = minimal_doc();
            let budget = Budget::default();
            let cache = CountCache::default();
            let pages_memo = Memo::new(1);
            let outline_memo = Memo::new(1);
            let dests_memo = Memo::new(1);
            let refs_cache = refs::Cache::new();

            let has_child: Vec<bool> = (0..nodes.len())
                .map(|i| i + 1 < nodes.len() && nodes[i + 1].0 > nodes[i].0)
                .collect();

            let mut current_open: Vec<bool> = nodes
                .iter()
                .enumerate()
                .map(|(i, &(_, open))| has_child[i] && open)
                .collect();

            let initial_specs: Vec<Spec> = nodes
                .iter()
                .enumerate()
                .map(|(i, &(level, _))| {
                    (level, format!("Item {i}"), none(), None, 0, current_open[i])
                })
                .collect();

            let mut tx = doc.begin();
            outline_write(&mut tx, &pages_memo, &refs_cache, &budget, initial_specs).unwrap();
            let v0 = commit_with_counts(tx, &cache, &budget).unwrap();

            let initial_entries =
                outline_entries(&outline_memo, &pages_memo, &dests_memo, &v0, &budget).unwrap();
            let ids: Vec<(u32, u16)> = initial_entries.iter().map(|e| e.0).collect();

            let mut tx = doc.begin();
            for (idx, open) in toggles {
                if has_child[idx] {
                    cache.set_open(&mut tx, &budget, ids[idx], open).unwrap();
                    current_open[idx] = open;
                } else {
                    let err = cache.set_open(&mut tx, &budget, ids[idx], open).unwrap_err();
                    prop_assert!(matches!(err, Error::Pdf(_)));
                }
            }
            let mutated = commit_with_counts(tx, &cache, &budget).unwrap();

            let oracle_doc = minimal_doc();
            let final_specs: Vec<Spec> = nodes
                .iter()
                .enumerate()
                .map(|(i, &(level, _))| {
                    (level, format!("Item {i}"), none(), None, 0, current_open[i])
                })
                .collect();
            let mut oracle_tx = oracle_doc.begin();
            outline_write(&mut oracle_tx, &pages_memo, &refs_cache, &budget, final_specs).unwrap();
            let expected = oracle_tx.commit(&budget).unwrap();

            prop_assert_eq!(
                root_and_item_counts(&mutated, &budget),
                root_and_item_counts(&expected, &budget)
            );
        }
    }
}
