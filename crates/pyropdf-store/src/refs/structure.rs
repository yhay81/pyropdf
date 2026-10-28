//! The rules for the structure content of pages that leave the document.
//! Ported from pylopdf's `rust/src/structure_prune.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321.
//!
//! A tagged PDF's structure tree names the page of its marked content
//! through `/Pg` and its annotations through an object reference's `/Obj`,
//! and the parent tree and the ID tree of the structure tree root name its
//! elements. When pages leave, the structure content that lay only on them
//! goes: the marked-content and object references to them, the elements
//! left without kids, the entries of the two trees and of `/Ref` that name
//! those elements, and a remaining element's `/Pg` that names a page that
//! leaves. An object reference to an annotation that a page that stays
//! lists names that page in its `/Pg`.
//!
//! The walk reads every element once, whatever cycles the tree holds; a
//! reference cycle reads as absent.

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::memo::Reads;
use crate::value::{Array, Dict, Reference, Value};
use crate::version::table::Table;
use crate::{LimitExceeded, Result};

use super::navigation::{catalog, dict_of, is_aliasing, is_in, readable, type_of};
use super::rules::copy_path;
use super::{Edits, References, Rule, Step, copy_for_edit};

/// The most levels of the parent tree or the ID tree.
const MAX_LOOKUP_TREE_DEPTH: u64 = 32;

/// The most references followed to a value. A detected cycle is absent;
/// a longer acyclic chain refuses instead of hiding structure content.
const MAX_REFERENCE_DEPTH: usize = 32;

/// Plans the rules for the structure content of `removed`, the pages that
/// leave; see [`super::remove_pages`]. Runs after the navigation rules,
/// whose removed links leave the structure tree too.
pub(super) fn plan(
    reads: &mut Reads<'_>,
    index: &References,
    removed: &[u32],
    retained: &[u32],
    edits: &mut Edits<'_>,
) -> Result<()> {
    let budget = reads.budget();
    budget.poll()?;
    if removed.is_empty() {
        return Ok(());
    }
    let Some(catalog) = catalog(reads)? else {
        return Ok(());
    };
    let Some(object) = readable(reads, catalog)? else {
        return Ok(());
    };
    let Some(root) = dict_of(&object).and_then(|dict| dict.get("StructTreeRoot")) else {
        return Ok(());
    };
    let at = Location::object(catalog, budget).child(Step::Key("StructTreeRoot".into()))?;
    let Some((Some(at), Value::Dict(root))) = resolve(reads, Some(at), root)? else {
        return Ok(());
    };
    if is_aliasing(&root) {
        return Err(aliasing());
    }
    let mut links = BudgetVec::new(budget);
    links.try_extend_from_slice(edits.orphans())?;
    let annotations = Annotations::read(reads, removed, retained, links)?;
    let mut walk = Walk {
        index,
        removed,
        annotations: &annotations,
        states: Table::new(),
        walked: Table::new(),
        references: BudgetVec::new(budget),
        budget,
    };
    walk.walk(reads, edits, at.duplicate()?, &root, true)?;
    let parent_leaves = match root.get("ParentTree") {
        Some(tree) => leaves(reads, &at, "ParentTree", tree, "Nums")?,
        None => BudgetVec::new(budget),
    };
    let id_leaves = match root.get("IDTree") {
        Some(tree) => leaves(reads, &at, "IDTree", tree, "Names")?,
        None => BudgetVec::new(budget),
    };
    // A malformed tree names elements that `/K` does not reach; their
    // content keeps its pages all the same.
    for leaf in parent_leaves.iter().chain(id_leaves.iter()) {
        for value in leaf.items.iter().skip(1).step_by(2) {
            walk.detached(reads, edits, value)?;
            if let Some((_, Value::Array(elements))) = resolve(reads, None, value)? {
                for element in elements.iter() {
                    walk.detached(reads, edits, element)?;
                }
            }
        }
    }
    walk.plan_references(reads, edits)?;

    // The parent tree keys of the removed pages and of the annotations that
    // only they list, unless a page or an annotation that stays uses them.
    let keys = Keys::read(reads, removed, retained, &annotations)?;
    for leaf in parent_leaves.iter() {
        walk.pairs(reads, edits, leaf, |reads, walk, key, value| {
            if matches!(key, Value::Integer(key) if keys.is_dropped(*key)) {
                return Ok(true);
            }
            walk.names_only_removed(reads, value)
        })?;
    }
    for leaf in id_leaves.iter() {
        walk.pairs(reads, edits, leaf, |reads, walk, _, value| {
            walk.is_removed(reads, value)
        })?;
    }
    Ok(())
}

/// Where a value is: the object that holds it and the path to it there.
struct Location<'b> {
    number: u32,
    path: BudgetVec<'b, Step>,
}

impl<'b> Location<'b> {
    const fn object(number: u32, budget: &'b Budget) -> Self {
        Self {
            number,
            path: BudgetVec::new(budget),
        }
    }

    fn child(&self, step: Step) -> Result<Self> {
        let mut path = copy_path(&self.path, self.path.budget())?;
        path.try_push(step)?;
        Ok(Self {
            number: self.number,
            path,
        })
    }

    fn duplicate(&self) -> Result<Self> {
        let path = copy_path(&self.path, self.path.budget())?;
        Ok(Self {
            number: self.number,
            path,
        })
    }

    /// Returns the number of the object that is the value, if it is one.
    fn indirect(&self) -> Option<u32> {
        self.path.is_empty().then_some(self.number)
    }

    /// Plans `rule` for the entry at `step` of the value.
    fn apply(
        &self,
        reads: &mut Reads<'_>,
        index: &References,
        edits: &mut Edits<'_>,
        step: Step,
        rule: Rule,
    ) -> Result<()> {
        let entry = self.child(step)?;
        edits.apply(reads, index, entry.number, &entry.path, rule)
    }
}

/// Follows `value` through references, and returns what it names with its
/// location: `at` for a direct value, or the last object it leads to.
/// Null and missing or cyclic references read as absent. Store errors and
/// chains longer than [`MAX_REFERENCE_DEPTH`] refuse the operation.
fn resolve<'b>(
    reads: &mut Reads<'b>,
    mut at: Option<Location<'b>>,
    value: &Value,
) -> Result<Option<(Option<Location<'b>>, Value)>> {
    let budget = reads.budget();
    let mut value = copy_for_edit(value, budget)?;
    let mut seen = BudgetVec::new(budget);
    while let Value::Reference(reference) = value {
        budget.poll()?;
        if seen.contains(&reference) {
            return Ok(None);
        }
        if seen.len() >= MAX_REFERENCE_DEPTH {
            return Err(LimitExceeded::over(
                LimitCode::StructureTreeCount,
                MAX_REFERENCE_DEPTH as u64,
                (seen.len() + 1) as u64,
            )
            .into());
        }
        seen.try_push(reference)?;
        let Some(object) = readable(reads, reference.number())?
            .filter(|object| object.generation() == reference.generation())
        else {
            return Ok(None);
        };
        at = Some(Location::object(reference.number(), reads.budget()));
        value = copy_for_edit(object.value(), budget)?;
    }
    Ok((!matches!(value, Value::Null)).then_some((at, value)))
}

/// Returns the page that `/Pg` of `dict` names.
fn page_of(reads: &mut Reads<'_>, dict: &Dict) -> Result<Option<u32>> {
    Ok(match dict.get("Pg") {
        Some(Value::Reference(page))
            if reads.generation(page.number())? == Some(page.generation()) =>
        {
            Some(page.number())
        }
        _ => None,
    })
}

fn integer_of(dict: &Dict, key: &str) -> Option<i64> {
    match dict.get(key) {
        Some(&Value::Integer(value)) => Some(value),
        _ => None,
    }
}

/// The annotations of the pages before the edit.
struct Annotations<'b> {
    /// Each annotation that a page that stays lists, with the first such
    /// page.
    retained: Table<Reference>,
    /// The annotations that removed pages list.
    listed: Table<()>,
    /// The links that the navigation rules remove from the pages that stay,
    /// ascending.
    links: BudgetVec<'b, u32>,
}

impl<'b> Annotations<'b> {
    fn read(
        reads: &mut Reads<'b>,
        removed: &[u32],
        retained: &[u32],
        mut links: BudgetVec<'b, u32>,
    ) -> Result<Self> {
        let budget = reads.budget();
        let mut annotations = Self {
            retained: Table::new(),
            listed: Table::new(),
            links: BudgetVec::new(budget),
        };
        for (pages, stays) in [(retained, true), (removed, false)] {
            for &page in pages {
                budget.poll()?;
                let Some(object) = readable(reads, page)? else {
                    continue;
                };
                let Some(annots) = dict_of(&object).and_then(|dict| dict.get("Annots")) else {
                    continue;
                };
                let Some((_, Value::Array(items))) = resolve(reads, None, annots)? else {
                    continue;
                };
                let listing = Reference::new(page, object.generation());
                for item in items.iter() {
                    let Value::Reference(annotation) = item else {
                        continue;
                    };
                    let number = annotation.number();
                    if reads.generation(number)? != Some(annotation.generation()) {
                        continue;
                    }
                    if !stays {
                        annotations.listed.insert(number, (), budget)?;
                    } else if annotations.retained.get(number).is_none() {
                        annotations.retained.insert(number, listing, budget)?;
                    }
                }
            }
        }
        links.sort_unstable();
        annotations.links = links;
        Ok(annotations)
    }

    /// Returns the page that stays and lists `annotation`, if one does.
    fn listing(&self, annotation: u32) -> Option<Reference> {
        if is_in(&self.links, annotation) {
            return None;
        }
        self.retained.get(annotation).copied()
    }

    /// Returns whether `annotation` leaves: only removed pages list it, or
    /// it is a link that the navigation rules remove.
    fn leaves(&self, annotation: u32) -> bool {
        is_in(&self.links, annotation)
            || (self.listed.get(annotation).is_some() && self.retained.get(annotation).is_none())
    }
}

/// The parent tree keys that the removed pages and their annotations use,
/// and those that the pages and annotations that stay use.
struct Keys<'b> {
    dropped: BudgetVec<'b, i64>,
    retained: BudgetVec<'b, i64>,
}

impl<'b> Keys<'b> {
    fn read(
        reads: &mut Reads<'b>,
        removed: &[u32],
        retained: &[u32],
        annotations: &Annotations<'_>,
    ) -> Result<Self> {
        let budget = reads.budget();
        let mut keys = Self {
            dropped: BudgetVec::new(budget),
            retained: BudgetVec::new(budget),
        };
        for (pages, stays) in [(retained, true), (removed, false)] {
            for &page in pages {
                keys.add(reads, page, "StructParents", stays)?;
            }
        }
        for (annotation, _) in annotations.retained.iter() {
            if annotations.listing(annotation).is_some() {
                keys.add(reads, annotation, "StructParent", true)?;
            }
        }
        for (annotation, _) in annotations.listed.iter() {
            if annotations.leaves(annotation) && !is_in(&annotations.links, annotation) {
                keys.add(reads, annotation, "StructParent", false)?;
            }
        }
        for &link in annotations.links.iter() {
            keys.add(reads, link, "StructParent", false)?;
        }
        keys.dropped.sort_unstable();
        keys.retained.sort_unstable();
        budget.poll()?;
        Ok(keys)
    }

    fn add(&mut self, reads: &mut Reads<'_>, number: u32, key: &str, stays: bool) -> Result<()> {
        reads.budget().poll()?;
        let Some(object) = readable(reads, number)? else {
            return Ok(());
        };
        let Some(value) = dict_of(&object).and_then(|dict| integer_of(dict, key)) else {
            return Ok(());
        };
        if stays {
            self.retained.try_push(value)?;
        } else {
            self.dropped.try_push(value)?;
        }
        Ok(())
    }

    fn is_dropped(&self, key: i64) -> bool {
        self.dropped.binary_search(&key).is_ok() && self.retained.binary_search(&key).is_err()
    }
}

/// A leaf of the parent tree or the ID tree: its array of keys and values.
struct Leaf<'b> {
    at: Location<'b>,
    items: Array,
}

/// Returns the leaves of the number tree (`pairs` is `Nums`) or the name
/// tree (`Names`) at `key` of the structure tree root at `root`. Refuses a
/// tree deeper than [`MAX_LOOKUP_TREE_DEPTH`] levels.
fn leaves<'b>(
    reads: &mut Reads<'b>,
    root: &Location<'b>,
    key: &str,
    tree: &Value,
    pairs: &str,
) -> Result<BudgetVec<'b, Leaf<'b>>> {
    let budget = reads.budget();
    let mut leaves = BudgetVec::new(budget);
    let mut seen: Table<()> = Table::new();
    let mut pending = BudgetVec::new(budget);
    pending.try_push((
        Some(root.child(Step::Key(key.into()))?),
        copy_for_edit(tree, budget)?,
        1,
    ))?;
    while let Some((at, value, depth)) = pending.pop() {
        budget.poll()?;
        let Some((Some(at), Value::Dict(node))) = resolve(reads, at, &value)? else {
            continue;
        };
        if at
            .indirect()
            .is_some_and(|number| seen.get(number).is_some())
        {
            continue;
        }
        if is_aliasing(&node) {
            return Err(aliasing());
        }
        if let Some(number) = at.indirect() {
            seen.insert(number, (), budget)?;
        }
        if let Some(entries) = node.get(pairs)
            && let Some((resolved, Value::Array(items))) = resolve(reads, None, entries)?
        {
            let array = match resolved {
                Some(array) if seen.get(array.number).is_some() => None,
                Some(array) => {
                    seen.insert(array.number, (), budget)?;
                    Some(array)
                }
                None => Some(at.child(Step::Key(pairs.into()))?),
            };
            if let Some(at) = array {
                leaves.try_push(Leaf { at, items })?;
            }
        }
        if let Some(kids) = node.get("Kids")
            && let Some((_, Value::Array(items))) = resolve(reads, None, kids)?
        {
            for item in items.iter() {
                budget.poll()?;
                if let Value::Reference(reference) = item {
                    if seen.get(reference.number()).is_some() {
                        continue;
                    }
                    if depth >= MAX_LOOKUP_TREE_DEPTH {
                        return Err(LimitExceeded::over(
                            LimitCode::StructureTreeCount,
                            MAX_LOOKUP_TREE_DEPTH,
                            depth + 1,
                        )
                        .into());
                    }
                    pending.try_push((None, Value::Reference(*reference), depth + 1))?;
                }
            }
        }
    }
    Ok(leaves)
}

/// How far the walk of an element has come.
#[derive(Clone, Copy, PartialEq, Eq)]
enum State {
    Walking,
    Kept,
    Removed,
}

/// The kids of an element being walked.
enum Kids<'b> {
    None,
    /// A `/K` that is no array.
    Single(Option<Value>),
    Array {
        at: Location<'b>,
        items: Array,
        next: usize,
    },
}

/// What a kid of an element amounts to.
enum Kid<'b> {
    Kept,
    Removed,
    /// An object reference that stays, whose `/Pg` names a removed page,
    /// and the page that stays and lists its annotation.
    Retarget(Location<'b>, Reference),
    Element(Location<'b>, Dict),
}

/// The `/Ref` of a walked element, which names other elements.
enum Named<'b> {
    Array {
        element: Location<'b>,
        array: Location<'b>,
        items: Array,
    },
    Single {
        element: Location<'b>,
        value: Value,
    },
}

/// An element, or the structure tree root, being walked.
struct Frame<'b> {
    at: Location<'b>,
    page: Option<u32>,
    root: bool,
    kids: Kids<'b>,
    /// The position of the kid whose element is being walked.
    current: usize,
    count: usize,
    kept: bool,
    removed: BudgetVec<'b, usize>,
}

impl Frame<'_> {
    fn record(&mut self, position: usize, removed: bool) -> Result<()> {
        self.count += 1;
        if removed {
            self.removed.try_push(position)?;
        } else {
            self.kept = true;
        }
        Ok(())
    }
}

struct Walk<'w, 'b> {
    index: &'w References,
    removed: &'w [u32],
    annotations: &'w Annotations<'b>,
    /// The states of the elements that are objects.
    states: Table<State>,
    /// The arrays, as objects, of `/K` and `/Ref` that the walk planned.
    walked: Table<()>,
    references: BudgetVec<'b, Named<'b>>,
    budget: &'b Budget,
}

impl<'b> Walk<'_, 'b> {
    fn dropped(&self, page: Option<u32>) -> bool {
        page.is_some_and(|page| is_in(self.removed, page))
    }

    fn on(&self, page: Option<u32>) -> Kid<'b> {
        if self.dropped(page) {
            Kid::Removed
        } else {
            Kid::Kept
        }
    }

    /// Returns whether `value` refers to an element that the walk removed.
    fn is_removed(&self, reads: &mut Reads<'_>, value: &Value) -> Result<bool> {
        Ok(matches!(value, Value::Reference(element)
            if self.states.get(element.number()) == Some(&State::Removed)
                && reads.generation(element.number())? == Some(element.generation())))
    }

    /// Returns whether the array is new to the walk, and records it.
    fn once(&mut self, number: u32) -> Result<bool> {
        if self.walked.get(number).is_some() {
            return Ok(false);
        }
        self.walked.insert(number, (), self.budget)?;
        Ok(true)
    }

    fn frame(
        &mut self,
        reads: &mut Reads<'b>,
        at: Location<'b>,
        dict: &Dict,
        root: bool,
    ) -> Result<Frame<'b>> {
        let mut kids = Kids::None;
        if let Some(value) = dict.get("K") {
            match resolve(reads, None, value)? {
                None => {}
                Some((resolved, Value::Array(items))) => {
                    let array = match resolved {
                        // A malformed tree shares the array with an element
                        // that the walk planned already.
                        Some(array) => self.once(array.number)?.then_some(array),
                        None => Some(at.child(Step::Key("K".into()))?),
                    };
                    if let Some(array) = array {
                        kids = Kids::Array {
                            at: array,
                            items,
                            next: 0,
                        };
                    }
                }
                Some(_) => kids = Kids::Single(Some(copy_for_edit(value, self.budget)?)),
            }
        }
        if !root && let Some(value) = dict.get("Ref") {
            let named = match resolve(reads, None, value)? {
                Some((resolved, Value::Array(items))) => {
                    let array = match resolved {
                        Some(array) => self.once(array.number)?.then_some(array),
                        None => Some(at.child(Step::Key("Ref".into()))?),
                    };
                    match array {
                        Some(array) => Some(Named::Array {
                            element: at.duplicate()?,
                            array,
                            items,
                        }),
                        None => None,
                    }
                }
                _ => Some(Named::Single {
                    element: at.duplicate()?,
                    value: copy_for_edit(value, self.budget)?,
                }),
            };
            if let Some(named) = named {
                self.references.try_push(named)?;
            }
        }
        Ok(Frame {
            page: if root { None } else { page_of(reads, dict)? },
            at,
            root,
            kids,
            current: 0,
            count: 0,
            kept: false,
            removed: BudgetVec::new(self.budget),
        })
    }

    fn classify(
        &self,
        reads: &mut Reads<'b>,
        frame: &Frame<'b>,
        position: Option<usize>,
        value: &Value,
    ) -> Result<Kid<'b>> {
        let here = || match (&frame.kids, position) {
            (Kids::Array { at, .. }, Some(position)) => at.child(Step::Index(position)),
            _ => frame.at.child(Step::Key("K".into())),
        };
        if matches!(value, Value::Integer(_)) {
            return Ok(self.on(frame.page));
        }
        let Some((at, Value::Dict(dict))) = resolve(reads, None, value)? else {
            return Ok(Kid::Kept);
        };
        if is_aliasing(&dict) {
            return Err(aliasing());
        }
        match type_of(&dict) {
            Some(b"MCR") => Ok(self.on(page_of(reads, &dict)?.or(frame.page))),
            Some(b"OBJR") => {
                let own = page_of(reads, &dict)?;
                if let Some(Value::Reference(target)) = dict.get("Obj")
                    && reads.generation(target.number())? == Some(target.generation())
                {
                    let target = target.number();
                    if let Some(listing) = self.annotations.listing(target) {
                        if !self.dropped(own) {
                            return Ok(Kid::Kept);
                        }
                        let at = match at {
                            Some(at) => at,
                            None => here()?,
                        };
                        return Ok(Kid::Retarget(at, listing));
                    }
                    if self.annotations.leaves(target) || self.unlisted_on_removed(reads, target)? {
                        return Ok(Kid::Removed);
                    }
                }
                Ok(self.on(own.or(frame.page)))
            }
            _ if dict.get("S").is_some() => {
                let at = match at {
                    Some(at) => at,
                    None => here()?,
                };
                Ok(Kid::Element(at, dict))
            }
            _ => Ok(Kid::Kept),
        }
    }

    /// Returns whether `annotation`, which no page that stays lists, names a
    /// removed page in `/P`.
    fn unlisted_on_removed(&self, reads: &mut Reads<'b>, annotation: u32) -> Result<bool> {
        if self.annotations.retained.get(annotation).is_some() {
            return Ok(false);
        }
        let Some(object) = readable(reads, annotation)? else {
            return Ok(false);
        };
        Ok(matches!(
            dict_of(&object).and_then(|dict| dict.get("P")),
            Some(Value::Reference(page)) if is_in(self.removed, page.number())
                && reads.generation(page.number())? == Some(page.generation())
        ))
    }

    /// Walks an element that only the parent tree or the ID tree names.
    fn detached(
        &mut self,
        reads: &mut Reads<'b>,
        edits: &mut Edits<'_>,
        value: &Value,
    ) -> Result<()> {
        let Value::Reference(element) = value else {
            return Ok(());
        };
        let number = element.number();
        if self.states.get(number).is_some() {
            return Ok(());
        }
        let Some(object) =
            readable(reads, number)?.filter(|object| object.generation() == element.generation())
        else {
            return Ok(());
        };
        let Value::Dict(dict) = &**object.value() else {
            return Ok(());
        };
        if is_aliasing(dict) {
            return Err(aliasing());
        }
        if dict.get("S").is_none() {
            return Ok(());
        }
        self.states.insert(number, State::Walking, self.budget)?;
        self.walk(
            reads,
            edits,
            Location::object(number, self.budget),
            dict,
            false,
        )
    }

    /// Walks the element or the structure tree root at `start` and the
    /// elements below it, and plans their edits. Refuses elements nested
    /// deeper than the limit of `structure_tree_count`.
    fn walk(
        &mut self,
        reads: &mut Reads<'b>,
        edits: &mut Edits<'_>,
        start: Location<'b>,
        dict: &Dict,
        root: bool,
    ) -> Result<()> {
        let mut stack = BudgetVec::new(self.budget);
        let frame = self.frame(reads, start, dict, root)?;
        stack.try_push(frame)?;
        loop {
            self.budget.poll()?;
            let depth = stack.len();
            let Some(frame) = stack.last_mut() else {
                break;
            };
            let next = match &mut frame.kids {
                Kids::None => None,
                Kids::Single(value) => value.take().map(|value| (None, value)),
                Kids::Array { items, next, .. } => {
                    let item = items
                        .get(*next)
                        .map(|value| {
                            copy_for_edit(value, self.budget).map(|value| (Some(*next), value))
                        })
                        .transpose()?;
                    *next += 1;
                    item
                }
            };
            let Some((position, value)) = next else {
                let Some(frame) = stack.pop() else {
                    break;
                };
                let removed = self.finish(reads, edits, frame)?;
                if let Some(parent) = stack.last_mut() {
                    let current = parent.current;
                    parent.record(current, removed)?;
                }
                continue;
            };
            let at = position.unwrap_or(0);
            let removed = match self.classify(reads, frame, position, &value)? {
                Kid::Kept => false,
                Kid::Removed => true,
                Kid::Retarget(object, page) => {
                    object.apply(
                        reads,
                        self.index,
                        edits,
                        Step::Key("Pg".into()),
                        Rule::Retarget(page),
                    )?;
                    false
                }
                Kid::Element(element, dict) => {
                    let number = element.indirect();
                    match number.and_then(|number| self.states.get(number).copied()) {
                        Some(State::Removed) => true,
                        Some(State::Walking | State::Kept) => false,
                        None => {
                            frame.current = at;
                            self.budget.check(
                                LimitCode::StructureTreeCount,
                                u64::try_from(depth.saturating_add(1)).unwrap_or(u64::MAX),
                            )?;
                            if let Some(number) = number {
                                self.states.insert(number, State::Walking, self.budget)?;
                            }
                            let child = self.frame(reads, element, &dict, false)?;
                            stack.try_push(child)?;
                            continue;
                        }
                    }
                }
            };
            frame.record(at, removed)?;
        }
        Ok(())
    }

    /// Plans the edits of a walked element; returns whether it goes.
    fn finish(
        &mut self,
        reads: &mut Reads<'b>,
        edits: &mut Edits<'_>,
        frame: Frame<'b>,
    ) -> Result<bool> {
        let removed = !frame.root && frame.count > 0 && !frame.kept;
        let number = frame.at.indirect();
        if let Some(number) = number
            && self.states.get(number).is_some()
        {
            let state = if removed { State::Removed } else { State::Kept };
            self.states.insert(number, state, self.budget)?;
        }
        if removed {
            if let Some(number) = number {
                edits.orphan(number)?;
            }
            return Ok(true);
        }
        if !frame.removed.is_empty() {
            match &frame.kids {
                Kids::Array { at, .. } => {
                    for &position in frame.removed.iter() {
                        at.apply(
                            reads,
                            self.index,
                            edits,
                            Step::Index(position),
                            Rule::RemoveEntry,
                        )?;
                    }
                }
                // Only the root keeps a lone `/K` that goes.
                _ => frame.at.apply(
                    reads,
                    self.index,
                    edits,
                    Step::Key("K".into()),
                    Rule::RemoveEntry,
                )?,
            }
        }
        if self.dropped(frame.page) {
            frame.at.apply(
                reads,
                self.index,
                edits,
                Step::Key("Pg".into()),
                Rule::RemoveEntry,
            )?;
        }
        Ok(false)
    }

    /// Plans the removal of the removed elements from the `/Ref` of the
    /// elements that stay.
    fn plan_references(&mut self, reads: &mut Reads<'b>, edits: &mut Edits<'_>) -> Result<()> {
        let references = std::mem::replace(&mut self.references, BudgetVec::new(self.budget));
        for named in references.iter() {
            self.budget.poll()?;
            let (element, gone) = match named {
                Named::Array {
                    element,
                    array,
                    items,
                } => {
                    if self.is_gone(element) {
                        continue;
                    }
                    let mut gone = BudgetVec::new(self.budget);
                    for (position, item) in items.iter().enumerate() {
                        self.budget.poll()?;
                        if self.is_removed(reads, item)? {
                            gone.try_push(position)?;
                        }
                    }
                    if gone.is_empty() {
                        continue;
                    }
                    if gone.len() < items.len() {
                        for &position in gone.iter() {
                            array.apply(
                                reads,
                                self.index,
                                edits,
                                Step::Index(position),
                                Rule::RemoveEntry,
                            )?;
                        }
                        continue;
                    }
                    (element, true)
                }
                Named::Single { element, value } => (
                    element,
                    !self.is_gone(element) && self.is_removed(reads, value)?,
                ),
            };
            if gone {
                element.apply(
                    reads,
                    self.index,
                    edits,
                    Step::Key("Ref".into()),
                    Rule::RemoveEntry,
                )?;
            }
        }
        Ok(())
    }

    /// Returns whether the element at `at` is an object that the walk
    /// removed.
    fn is_gone(&self, at: &Location<'_>) -> bool {
        at.indirect()
            .is_some_and(|number| self.states.get(number) == Some(&State::Removed))
    }

    /// Returns whether a parent tree value names removed elements and
    /// nothing else but nulls.
    fn names_only_removed(&self, reads: &mut Reads<'b>, value: &Value) -> Result<bool> {
        if self.is_removed(reads, value)? {
            return Ok(true);
        }
        let Some((_, Value::Array(items))) = resolve(reads, None, value)? else {
            return Ok(false);
        };
        let mut any = false;
        for item in items.iter() {
            self.budget.poll()?;
            match item {
                Value::Null => {}
                item if self.is_removed(reads, item)? => any = true,
                _ => return Ok(false),
            }
        }
        Ok(any)
    }

    /// Plans the removal of the pairs of `leaf` that `goes` selects.
    fn pairs(
        &self,
        reads: &mut Reads<'b>,
        edits: &mut Edits<'_>,
        leaf: &Leaf<'b>,
        mut goes: impl FnMut(&mut Reads<'b>, &Self, &Value, &Value) -> Result<bool>,
    ) -> Result<()> {
        let mut items = leaf.items.iter();
        let mut position = 0;
        while let (Some(key), Some(value)) = (items.next(), items.next()) {
            self.budget.poll()?;
            if goes(reads, self, key, value)? {
                for at in [position, position + 1] {
                    leaf.at
                        .apply(reads, self.index, edits, Step::Index(at), Rule::RemoveEntry)?;
                }
            }
            position += 2;
        }
        Ok(())
    }
}

/// Refuse aliases before page freeing can leave the structure tree dangling.
fn aliasing() -> crate::Error {
    crate::Error::malformed("structure tree cannot alias a page or catalog dictionary")
}
