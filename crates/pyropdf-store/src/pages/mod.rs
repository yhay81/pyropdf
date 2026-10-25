//! The page index: the page objects of a version in order, the index of
//! each page object, and the attributes that pages inherit (ISO 32000-2
//! section 7.7.3).
//!
//! The walk is iterative and bounded: its stack and its lists grow through
//! the operation's budget, `object_depth` bounds the nesting of page-tree
//! nodes, `page_count` the pages, and reference chains end after
//! [`MAX_CHAIN`] references. A node or page reached twice refuses the walk,
//! as a cycle when it is its own ancestor. Like hayro, the walk reads a
//! `/Kids` entry whose object is absent or not a dictionary as absent, and
//! any dictionary that is not a `/Pages` node as a page.
//!
//! The index is memoized with its read set (DESIGN.md section 5), so an
//! edit outside the page tree keeps it: [`PageIndex::memoized`].

#[cfg(test)]
mod tests;

use std::collections::HashSet;
use std::sync::Arc;

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::memo::{Memo, Reads};
use crate::value::{Dict, Reference, Value};
use crate::version::Version;
use crate::{Error, Result};

/// The most references that the walk follows from one entry to its value.
pub const MAX_CHAIN: usize = 16;

/// The attributes that a page inherits from its ancestors, in the order of
/// [`Page::inherited`] (ISO 32000-2 table 31).
const INHERITABLE: [&str; 4] = ["Resources", "MediaBox", "CropBox", "Rotate"];

/// How often the walk polls the budget for its deadline and cancellation.
const POLL_INTERVAL: usize = 256;

/// The key of the page index in a [`Memo`]: it has no settings.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct PageTree;

/// The page index of one version.
#[derive(Debug)]
pub struct PageIndex {
    pages: Box<[Page]>,
    /// The object number of each indirect page, ascending, with its index.
    by_number: Box<[(u32, usize)]>,
}

/// A page: its object, its dictionary, and the inheritable attributes that
/// apply to it.
#[derive(Clone, Debug)]
pub struct Page {
    object: Option<Reference>,
    value: Arc<Value>,
    attributes: [Option<Arc<Value>>; 4],
}

/// A page-tree node whose kids the walk is visiting.
struct Frame {
    /// The node's object number, when it is indirect.
    node: Option<u32>,
    kids: Arc<Value>,
    next: usize,
    attributes: [Option<Arc<Value>>; 4],
}

impl PageIndex {
    /// Returns the page index of `version`, reusing `memo`'s while the
    /// changes since it miss what it read.
    pub fn memoized(
        memo: &Memo<PageTree, Self>,
        version: &Arc<Version>,
        budget: &Budget,
    ) -> Result<Arc<Self>> {
        memo.get_or_compute(&PageTree, version, budget, Self::build)
    }

    /// Walks the page tree of the version that `reads` reads, from the
    /// trailer's `/Root`.
    ///
    /// Refuses with a malformed input error a document without a catalog
    /// or a page tree, a reference chain longer than [`MAX_CHAIN`], and a
    /// tree that reaches a node or a page twice; with `object_depth` nodes
    /// nested deeper than the budget allows, and with `page_count` more
    /// pages.
    pub fn build(reads: &mut Reads<'_>) -> Result<Self> {
        let budget = reads.budget();
        let trailer = reads.trailer();
        let catalog = resolve(reads, trailer.get("Root"), "/Root")?;
        let Some((_, catalog)) = catalog.filter(|(_, value)| matches!(**value, Value::Dict(_)))
        else {
            return Err(Error::malformed("the trailer names no catalog dictionary"));
        };
        let tree = resolve(reads, dictionary(&catalog).get("Pages"), "/Pages")?;
        let Some((chain, root)) = tree.filter(|(_, value)| matches!(**value, Value::Dict(_)))
        else {
            return Err(Error::malformed("the catalog names no page tree"));
        };
        let mut visited = Visited::new(budget);
        let mut stack: BudgetVec<'_, Frame> = BudgetVec::counting(budget, LimitCode::ObjectDepth);
        for &number in chain.numbers() {
            visited.insert(number, &stack)?;
        }
        let root_frame = node(reads, chain.first(), dictionary(&root), &Default::default())?;
        stack.try_push(root_frame)?;
        let mut pages: BudgetVec<'_, Page> = BudgetVec::counting(budget, LimitCode::PageCount);
        let mut steps = 0usize;
        while let Some(frame) = stack.last_mut() {
            steps = steps.wrapping_add(1);
            if steps.is_multiple_of(POLL_INTERVAL) {
                budget.poll()?;
            }
            let Value::Array(kids) = &*frame.kids else {
                unreachable!("a frame's kids are an array");
            };
            let Some(kid) = kids.get(frame.next).cloned() else {
                stack.pop();
                continue;
            };
            frame.next += 1;
            let inherited = frame.attributes.clone();
            let Some((chain, value)) = resolve(reads, Some(&kid), "a /Kids entry")? else {
                continue;
            };
            let Value::Dict(dict) = &*value else {
                continue;
            };
            if matches!(dict.get("Type"), Some(Value::Name(name)) if name.as_bytes() == b"Pages") {
                for &number in chain.numbers() {
                    visited.insert(number, &stack)?;
                }
                let frame = node(reads, chain.first(), dict, &inherited)?;
                stack.try_push(frame)?;
            } else {
                if let Some(number) = chain.first() {
                    visited.insert(number, &stack)?;
                }
                let mut attributes = inherited;
                for (slot, key) in attributes.iter_mut().zip(INHERITABLE) {
                    if let Some(own) = resolve(reads, dict.get(key), key)? {
                        *slot = Some(own.1);
                    }
                }
                let object = chain
                    .first_reference()
                    .map(|reference| Reference::new(reference.number(), reference.generation()));
                pages.try_push(Page {
                    object,
                    value: Arc::clone(&value),
                    attributes,
                })?;
            }
        }
        drop(stack);
        drop(visited);
        let mut by_number = BudgetVec::new(budget);
        for (at, page) in pages.iter().enumerate() {
            if let Some(object) = page.object {
                by_number.try_push((object.number(), at))?;
            }
        }
        by_number.sort_unstable();
        Ok(Self {
            pages: pages.into_vec().into_boxed_slice(),
            by_number: by_number.into_vec().into_boxed_slice(),
        })
    }

    /// Returns the number of pages.
    #[must_use]
    pub const fn len(&self) -> usize {
        self.pages.len()
    }

    /// Returns whether there are no pages.
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.pages.is_empty()
    }

    /// Returns the pages in order.
    #[must_use]
    pub const fn pages(&self) -> &[Page] {
        &self.pages
    }

    /// Returns page `index`, counted from 0.
    #[must_use]
    pub fn get(&self, index: usize) -> Option<&Page> {
        self.pages.get(index)
    }

    /// Returns the index of the page whose object `reference` names, in
    /// time logarithmic in the number of pages.
    #[must_use]
    pub fn index_of(&self, reference: Reference) -> Option<usize> {
        let at = self
            .by_number
            .binary_search_by_key(&reference.number(), |&(number, _)| number)
            .ok()?;
        let index = self.by_number[at].1;
        (self.pages[index].object == Some(reference)).then_some(index)
    }
}

impl Page {
    /// Returns the page's object, or `None` for a page dictionary written
    /// directly in `/Kids`.
    #[must_use]
    pub const fn object(&self) -> Option<Reference> {
        self.object
    }

    /// Returns the page dictionary.
    #[must_use]
    pub fn dict(&self) -> &Dict {
        dictionary(&self.value)
    }

    /// Returns the inheritable attribute `key`, `Resources`, `MediaBox`,
    /// `CropBox`, or `Rotate`, of the page or else of its nearest ancestor
    /// that has it, resolved, or `None` if none has it.
    #[must_use]
    pub fn inherited(&self, key: &str) -> Option<&Value> {
        let at = INHERITABLE.iter().position(|&name| name == key)?;
        self.attributes[at].as_deref()
    }

    /// Returns the resources that apply to the page.
    #[must_use]
    pub fn resources(&self) -> Option<&Value> {
        self.attributes[0].as_deref()
    }

    /// Returns the media box that applies to the page.
    #[must_use]
    pub fn media_box(&self) -> Option<&Value> {
        self.attributes[1].as_deref()
    }

    /// Returns the crop box that applies to the page.
    #[must_use]
    pub fn crop_box(&self) -> Option<&Value> {
        self.attributes[2].as_deref()
    }

    /// Returns the rotation that applies to the page.
    #[must_use]
    pub fn rotate(&self) -> Option<&Value> {
        self.attributes[3].as_deref()
    }
}

/// Returns the frame of node `dict`, whose attributes override `inherited`.
/// A node without a `/Kids` array has no kids.
fn node(
    reads: &mut Reads<'_>,
    number: Option<u32>,
    dict: &Dict,
    inherited: &[Option<Arc<Value>>; 4],
) -> Result<Frame> {
    let mut attributes = inherited.clone();
    for (slot, key) in attributes.iter_mut().zip(INHERITABLE) {
        if let Some((_, own)) = resolve(reads, dict.get(key), key)? {
            *slot = Some(own);
        }
    }
    let kids = match resolve(reads, dict.get("Kids"), "/Kids")? {
        Some((_, kids)) if matches!(*kids, Value::Array(_)) => kids,
        _ => Arc::new(Value::Array(crate::value::Array::new())),
    };
    Ok(Frame {
        node: number,
        kids,
        next: 0,
        attributes,
    })
}

/// The references that led to a value, the first one first.
#[derive(Default)]
struct Chain {
    references: [Option<Reference>; MAX_CHAIN],
    numbers: [u32; MAX_CHAIN],
    len: usize,
}

impl Chain {
    fn numbers(&self) -> &[u32] {
        &self.numbers[..self.len]
    }

    fn first(&self) -> Option<u32> {
        self.numbers().first().copied()
    }

    fn first_reference(&self) -> Option<Reference> {
        self.references[0]
    }
}

/// Follows `value` through references to a direct value. A null, a
/// reference to an absent object, and a missing entry are absent (ISO
/// 32000-2 sections 7.3.7 and 7.3.10).
fn resolve(
    reads: &mut Reads<'_>,
    value: Option<&Value>,
    what: &str,
) -> Result<Option<(Chain, Arc<Value>)>> {
    let mut chain = Chain::default();
    let mut current = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(value) => return Ok(Some((chain, Arc::new(value.clone())))),
    };
    loop {
        if chain.len == MAX_CHAIN {
            return Err(Error::malformed(format!(
                "{what} contains a reference cycle or a chain of more than {MAX_CHAIN} references"
            )));
        }
        chain.references[chain.len] = Some(current);
        chain.numbers[chain.len] = current.number();
        chain.len += 1;
        let Some(object) = reads.get(current.number())? else {
            return Ok(None);
        };
        if object.generation() != current.generation() {
            return Ok(None);
        }
        match &**object.value() {
            Value::Null => return Ok(None),
            Value::Reference(next) => current = *next,
            _ => return Ok(Some((chain, Arc::clone(object.value())))),
        }
    }
}

/// Returns the dictionary of a value that the walk checked is one.
fn dictionary(value: &Value) -> &Dict {
    match value {
        Value::Dict(dict) => dict,
        _ => unreachable!("the walk keeps only dictionaries"),
    }
}

/// The object numbers that the walk has reached, charged to the budget's
/// `memory_size`.
struct Visited<'b> {
    numbers: HashSet<u32>,
    budget: &'b Budget,
    charged: u64,
}

impl<'b> Visited<'b> {
    /// What one number costs in the set, roughly: the number and its share
    /// of the table's control bytes and spare capacity.
    const ENTRY: u64 = 16;

    fn new(budget: &'b Budget) -> Self {
        Self {
            numbers: HashSet::new(),
            budget,
            charged: 0,
        }
    }

    /// Records `number`, refusing a number that the walk has reached
    /// before: a cycle when a node on `stack` has it.
    fn insert(&mut self, number: u32, stack: &[Frame]) -> Result<()> {
        if self.numbers.contains(&number) {
            let reason = if stack.iter().any(|frame| frame.node == Some(number)) {
                "contains a cycle through"
            } else {
                "reuses object"
            };
            return Err(Error::malformed(format!("the page tree {reason} {number}")));
        }
        self.budget.charge(LimitCode::MemorySize, Self::ENTRY)?;
        self.charged += Self::ENTRY;
        #[expect(
            clippy::disallowed_methods,
            reason = "each insert is charged to the budget's memory_size first"
        )]
        self.numbers.insert(number);
        Ok(())
    }
}

impl Drop for Visited<'_> {
    fn drop(&mut self) {
        self.budget.release(LimitCode::MemorySize, self.charged);
    }
}
