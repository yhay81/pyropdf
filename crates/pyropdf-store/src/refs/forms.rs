//! The rules for the form fields of pages that leave the document: their
//! widgets leave the field tree, and so do the fields that keep no widget.
//! Ported from pylopdf's `FormPagePrune` and `PartialTree` in
//! `rust/src/acroform_import.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321. The planner walks the field
//! tree under the limits of the form's walk, because whether a field goes
//! depends on every widget below it; the index finds the pages that list
//! each widget.

use crate::budget::{Budget, BudgetVec};
use crate::memo::Reads;
use crate::value::{Array, Dict, Reference, Value};
use crate::version::table::Table;
use crate::{Error, Result};

use super::copy::copy_dict_for_edit;
use super::navigation::{catalog, dict_of, is_aliasing, is_in, readable};
use super::{Edits, References, Rule, Step, copy_for_edit};

/// The most field tree nodes that the walk visits.
const MAX_NODES: usize = 4_096;
/// The most `/Kids` entries that the walk reads, with one for each
/// top-level field.
const MAX_EDGES: usize = 8_192;
/// The deepest field tree that the walk reads.
const MAX_DEPTH: usize = 64;
/// The most widget entries that the walk reads.
const MAX_WIDGETS: usize = 4_096;

/// Plans the rules for the form fields of `removed`, the pages that leave;
/// see [`super::remove_pages`].
///
/// A widget belongs to the removed pages when one of them lists it in
/// `/Annots` and no retained page does, or when no page lists it and its
/// `/P` names one of them. Every walked `/Kids` loses those widgets and the
/// fields whose widgets all belonged there, a button field's matching
/// `/Opt` loses the same entries, and such top-level fields leave
/// `/Fields` and `/CO`. A retained widget whose `/P` names no retained page
/// points at the retained page that lists it. A removal reads only the
/// top-level fields that the removed pages reach
/// ([`super::form_impact::affected_form_objects`]), so these repairs stay
/// within them. Fields that never had a widget stay. Refuses, before planning anything, a field tree beyond the
/// limits of the form's walk.
///
/// For a `copy` of the retained pages, a widget that no page lists stays
/// only when its `/P` names a retained page. Returns the top-level fields
/// that hold a widget that stays, in `/Fields` order: those that such a
/// copy takes along.
pub(super) fn plan(
    reads: &mut Reads<'_>,
    index: &References,
    removed: &[u32],
    retained: &[u32],
    copy: bool,
    edits: &mut Edits<'_>,
) -> Result<Vec<u32>> {
    plan_with(reads, index, removed, retained, copy, !copy, edits)
}

/// [`plan`], reading only the fields that the removed pages reach when
/// `bounded`, or every field without.
pub(super) fn plan_with(
    reads: &mut Reads<'_>,
    index: &References,
    removed: &[u32],
    retained: &[u32],
    copy: bool,
    bounded: bool,
    edits: &mut Edits<'_>,
) -> Result<Vec<u32>> {
    reads.budget().poll()?;
    let Some(form) = form(reads, removed, retained)? else {
        return Ok(Vec::new());
    };
    // A removal reads only the fields that the removed pages reach, and
    // repairs `/P` only there; a copy takes every field that keeps a
    // widget, so it walks them all.
    let affected = if !bounded {
        None
    } else {
        let affected = super::form_impact::affected_form_objects(reads, index, removed, retained)?;
        if affected.is_empty() {
            return Ok(Vec::new());
        }
        Some(affected)
    };
    let fields = form.array(reads, "Fields")?;
    let top_level = top_level(
        reads,
        fields.as_ref().map(|(_, items)| items),
        removed,
        retained,
        affected.as_ref().map(|affected| affected.as_slice()),
    )?;
    if top_level.is_empty() {
        return Ok(Vec::new());
    }
    let mut tree = Tree::new(reads, index, removed, retained, copy);
    tree.walk(reads, &top_level)?;
    let mut planned = Table::new();
    for at in 0..tree.walked.len() {
        reads.budget().poll()?;
        let number = tree.walked[at];
        tree.prune(reads, index, edits, number, &mut planned)?;
    }
    let budget = reads.budget();
    if let Some((location, items)) = &fields
        && !location.planned(&planned)
    {
        for (at, entry) in items.iter().enumerate() {
            if at % 1024 == 0 {
                budget.poll()?;
            }
            if let Value::Reference(reference) = entry
                && tree.coverage.get(reference.number()).is_some()
                && !tree.keeps(reads, entry)?
            {
                location.remove(reads, index, edits, at)?;
                edits.orphan(reference.number())?;
            }
        }
    }
    let mut kept_top_level = BudgetVec::new(budget);
    let mut imported = BudgetVec::new(budget);
    for &reference in top_level.iter() {
        budget.poll()?;
        if tree.keeps(reads, &Value::Reference(reference))? {
            kept_top_level.try_push(reference.number())?;
        }
        if tree
            .coverage
            .get(reference.number())
            .is_some_and(|coverage| coverage.kept)
        {
            imported.try_push(reference.number())?;
        }
    }
    let kept = tree.kept(reads, &kept_top_level)?;
    if let Some((location, items)) = form.array(reads, "CO")?
        && !location.planned(&planned)
    {
        for (at, entry) in items.iter().enumerate() {
            if at % 1024 == 0 {
                budget.poll()?;
            }
            if let Value::Reference(reference) = entry
                && tree.coverage.get(reference.number()).is_some()
                && kept.binary_search(&reference.number()).is_err()
            {
                location.remove(reads, index, edits, at)?;
                edits.orphan(reference.number())?;
            }
        }
    }
    for &number in kept.iter() {
        budget.poll()?;
        tree.point_widgets(reads, index, edits, number)?;
    }
    Ok(imported.into_vec())
}

/// Returns the form's top-level fields, each once and in `/Fields` order,
/// and the references of its calculation order, in order.
pub(super) fn fields(reads: &mut Reads<'_>) -> Result<(Vec<Reference>, Vec<Reference>)> {
    let Some(form) = form(reads, &[], &[])? else {
        return Ok((Vec::new(), Vec::new()));
    };
    let fields = form.array(reads, "Fields")?;
    let top_level = top_level(
        reads,
        fields.as_ref().map(|(_, items)| items),
        &[],
        &[],
        None,
    )?;
    let mut order = BudgetVec::new(reads.budget());
    if let Some((_, items)) = form.array(reads, "CO")? {
        for entry in items.iter() {
            reads.budget().poll()?;
            if let &Value::Reference(reference) = entry {
                order.try_push(reference)?;
            }
        }
    }
    Ok((top_level.into_vec(), order.into_vec()))
}

/// The interactive form dictionary, and where it lies.
struct Form {
    location: Location,
    dict: Dict,
}

impl Form {
    /// Returns the array under `key` of the form, and where it lies: in the
    /// form, or as an array object that the form refers to.
    fn array(&self, reads: &mut Reads<'_>, key: &'static str) -> Result<Option<(Location, Array)>> {
        Ok(match self.dict.get(key) {
            Some(value @ Value::Array(_)) => match copy_for_edit(value, reads.budget())? {
                Value::Array(items) => Some((self.location.with(key), items)),
                _ => unreachable!("a copied array remains an array"),
            },
            Some(Value::Reference(reference)) => array_object(reads, *reference)?
                .map(|items| (Location::object(reference.number()), items)),
            _ => None,
        })
    }
}

/// Returns the catalog's `/AcroForm` dictionary, unless it is absent or a
/// page, a page tree node, or the catalog.
fn form(reads: &mut Reads<'_>, removed: &[u32], retained: &[u32]) -> Result<Option<Form>> {
    let Some(catalog) = catalog(reads)? else {
        return Ok(None);
    };
    let Some(object) = readable(reads, catalog)? else {
        return Ok(None);
    };
    let Some(dict) = dict_of(&object) else {
        return Ok(None);
    };
    let form = match dict.get("AcroForm") {
        Some(Value::Dict(form)) => Form {
            location: Location {
                number: catalog,
                keys: [Some("AcroForm"), None],
            },
            dict: copy_dict_for_edit(form, reads.budget())?,
        },
        Some(&Value::Reference(reference)) => {
            let Some(form) = dict_object(reads, reference)? else {
                return Ok(None);
            };
            if protected(reads, reference.number(), removed, retained)? || is_aliasing(&form) {
                return Err(form_alias());
            }
            Form {
                location: Location::object(reference.number()),
                dict: form,
            }
        }
        _ => return Ok(None),
    };
    if is_aliasing(&form.dict) {
        return Err(form_alias());
    }
    Ok(Some(form))
}

/// Returns the distinct field dictionaries of `fields`, in order. Some
/// producers list every annotation in `/Fields`; entries that are no field
/// stay out of the walk. With `only`, sorted by number, only the entries
/// that it names are read.
fn top_level<'b>(
    reads: &mut Reads<'b>,
    fields: Option<&Array>,
    removed: &[u32],
    retained: &[u32],
    only: Option<&[Reference]>,
) -> Result<BudgetVec<'b, Reference>> {
    let budget = reads.budget();
    let mut out = BudgetVec::new(budget);
    let mut seen = Table::new();
    for entry in fields.map(Array::iter).into_iter().flatten() {
        budget.poll()?;
        let &Value::Reference(reference) = entry else {
            continue;
        };
        if let Some(only) = only
            && only
                .binary_search_by_key(&reference.number(), |at| at.number())
                .is_err()
        {
            continue;
        }
        let Some(dict) = dict_object(reads, reference)? else {
            continue;
        };
        if protected(reads, reference.number(), removed, retained)? || is_aliasing(&dict) {
            return Err(form_alias());
        }
        if is_field(&dict) && seen.insert(reference.number(), (), budget)?.is_none() {
            out.try_push(reference)?;
        }
    }
    Ok(out)
}

fn is_field(dict: &Dict) -> bool {
    !is_aliasing(dict)
        && (dict.get("FT").is_some()
            || dict.get("T").is_some()
            || dict.get("Kids").is_some()
            || is_widget_subtype(dict))
}

fn is_widget_subtype(dict: &Dict) -> bool {
    matches!(dict.get("Subtype"), Some(Value::Name(name)) if name.as_bytes() == b"Widget")
}

/// Whether a dictionary is a widget annotation: its `/Subtype` is
/// `Widget`, or it has a `/Rect`.
fn looks_like_widget(dict: &Dict) -> bool {
    is_widget_subtype(dict) || dict.get("Rect").is_some()
}

/// An array that the rules remove entries of: in object `number` under
/// the keys that `keys` lists, or the object itself when it lists none.
#[derive(Clone, Copy)]
struct Location {
    number: u32,
    keys: [Option<&'static str>; 2],
}

impl Location {
    const fn object(number: u32) -> Self {
        Self {
            number,
            keys: [None, None],
        }
    }

    /// Returns the location of the array under `key` of the dictionary
    /// here.
    const fn with(self, key: &'static str) -> Self {
        Self {
            number: self.number,
            keys: [self.keys[0], Some(key)],
        }
    }

    /// Returns whether the location is an array object that the rules
    /// already filter as a `/Kids` or `/Opt`, as a malformed form can share
    /// one between them and `/Fields` or `/CO`.
    fn planned(&self, planned: &Table<()>) -> bool {
        self.keys == [None, None] && planned.get(self.number).is_some()
    }

    /// Plans the removal of the entry at `at`.
    fn remove(
        &self,
        reads: &mut Reads<'_>,
        index: &References,
        edits: &mut Edits<'_>,
        at: usize,
    ) -> Result<()> {
        let mut path = BudgetVec::new(reads.budget());
        for key in self.keys.into_iter().flatten() {
            path.try_push(Step::Key(key.into()))?;
        }
        path.try_push(Step::Index(at))?;
        edits.apply(reads, index, self.number, &path, Rule::RemoveEntry)
    }
}

/// Returns the dictionary that `reference` names, if it is one.
fn dict_object(reads: &mut Reads<'_>, reference: Reference) -> Result<Option<Dict>> {
    Ok(match readable(reads, reference.number())? {
        Some(object) if object.generation() == reference.generation() => match &**object.value() {
            Value::Dict(dict) => Some(copy_dict_for_edit(dict, reads.budget())?),
            _ => None,
        },
        _ => None,
    })
}

/// Returns the array that `reference` names, if it is one.
fn array_object(reads: &mut Reads<'_>, reference: Reference) -> Result<Option<Array>> {
    Ok(match readable(reads, reference.number())? {
        Some(object) if object.generation() == reference.generation() => match &**object.value() {
            value @ Value::Array(_) => match copy_for_edit(value, reads.budget())? {
                Value::Array(items) => Some(items),
                _ => unreachable!("a copied array remains an array"),
            },
            _ => None,
        },
        _ => None,
    })
}

/// Returns a field's `/Kids` array, and where it lies; any other value
/// reads as absent.
fn kids(reads: &mut Reads<'_>, number: u32, dict: &Dict) -> Result<Option<(Location, Array)>> {
    array_entry(reads, number, dict, "Kids")
}

fn array_entry(
    reads: &mut Reads<'_>,
    number: u32,
    dict: &Dict,
    key: &'static str,
) -> Result<Option<(Location, Array)>> {
    Ok(match dict.get(key) {
        Some(value @ Value::Array(_)) => match copy_for_edit(value, reads.budget())? {
            Value::Array(items) => Some((Location::object(number).with(key), items)),
            _ => unreachable!("a copied array remains an array"),
        },
        Some(&Value::Reference(reference)) => array_object(reads, reference)?
            .map(|items| (Location::object(reference.number()), items)),
        _ => None,
    })
}

/// Returns the dictionary of a field tree node that the walk reads: an
/// indirect dictionary that is no page, page tree node, or catalog.
fn node(reads: &mut Reads<'_>, number: u32) -> Result<Option<Dict>> {
    Ok(match readable(reads, number)? {
        Some(object) => match &**object.value() {
            Value::Dict(dict) if is_aliasing(dict) => return Err(form_alias()),
            Value::Dict(dict) => Some(copy_dict_for_edit(dict, reads.budget())?),
            _ => None,
        },
        None => None,
    })
}

/// One `/Kids` entry as the form's walk classifies it.
#[derive(Clone, Copy)]
enum Kid {
    /// A dictionary with its own partial name.
    Field(u32),
    /// A dictionary without a partial name or kids to fold: a widget of
    /// the parent field.
    Widget(u32),
    /// A dictionary without a partial name but with kids, which folds into
    /// the parent field; `widget` when it also looks like a widget.
    Intermediate { number: u32, widget: bool },
    /// Any other value that a reference names.
    Unusable,
    /// A direct entry, a null, or a reference to no object.
    Ignored,
}

/// Classifies a `/Kids` entry as pylopdf's `classify_field_kid` does: a
/// `/T` that is null or names nothing is an omitted partial name.
fn classify(reads: &mut Reads<'_>, kid: &Value, removed: &[u32], retained: &[u32]) -> Result<Kid> {
    let &Value::Reference(reference) = kid else {
        return Ok(Kid::Ignored);
    };
    let Some(object) = readable(reads, reference.number())? else {
        return Ok(Kid::Ignored);
    };
    if object.generation() != reference.generation() {
        return Ok(Kid::Ignored);
    }
    let child = match &**object.value() {
        Value::Dict(child) => child,
        Value::Null => return Ok(Kid::Ignored),
        _ => return Ok(Kid::Unusable),
    };
    let number = reference.number();
    if protected(reads, number, removed, retained)? || is_aliasing(child) {
        return Err(form_alias());
    }
    if let Some(name) = child.get("T")
        && !is_null(reads, name)?
    {
        return Ok(Kid::Field(number));
    }
    Ok(match kids(reads, number, child)? {
        Some((_, kids)) if !kids.is_empty() => Kid::Intermediate {
            number,
            widget: looks_like_widget(child),
        },
        _ => Kid::Widget(number),
    })
}

fn protected(
    reads: &mut Reads<'_>,
    number: u32,
    removed: &[u32],
    retained: &[u32],
) -> Result<bool> {
    Ok(catalog(reads)? == Some(number) || is_in(removed, number) || is_in(retained, number))
}

fn form_alias() -> Error {
    Error::malformed("a form field or form dictionary aliases a page or catalog")
}

/// Returns whether `value` is null, or a reference to a null or to no
/// object.
fn is_null(reads: &mut Reads<'_>, value: &Value) -> Result<bool> {
    Ok(match value {
        Value::Null => true,
        Value::Reference(reference) => match readable(reads, reference.number())? {
            Some(object) => {
                object.generation() != reference.generation()
                    || matches!(**object.value(), Value::Null)
            }
            None => true,
        },
        _ => false,
    })
}

/// Whether a field's subtree holds a widget, and one that stays, and
/// whether the field's resolved type is a button.
#[derive(Clone, Copy, Default)]
struct Coverage {
    widgets: bool,
    kept: bool,
    button: bool,
}

/// Where a widget lies: the retained page that lists it, if any, and
/// whether it stays.
#[derive(Clone, Copy)]
struct Placement {
    page: Option<u32>,
    kept: bool,
}

/// The field tree of a deletion, walked once.
struct Tree<'a, 'b> {
    index: &'a References,
    removed: &'a [u32],
    retained: &'a [u32],
    /// Whether the retained pages are copied, so that a widget that no
    /// page lists stays only when its `/P` names one of them.
    copy: bool,
    coverage: Table<Coverage>,
    /// The walked fields, in the order that the walk leaves them.
    walked: BudgetVec<'b, u32>,
    placements: Table<Placement>,
}

impl<'a, 'b> Tree<'a, 'b> {
    fn new(
        reads: &Reads<'b>,
        index: &'a References,
        removed: &'a [u32],
        retained: &'a [u32],
        copy: bool,
    ) -> Self {
        Self {
            index,
            removed,
            retained,
            copy,
            coverage: Table::new(),
            walked: BudgetVec::new(reads.budget()),
            placements: Table::new(),
        }
    }

    /// Walks every field below `top_level` depth-first and records, after
    /// a node's kids, whether its subtree holds a widget and one that
    /// stays. Charges nodes, edges, widgets, and levels as the form's walk
    /// does; a kid that leads back to a visited node adds nothing.
    fn walk(&mut self, reads: &mut Reads<'b>, top_level: &[Reference]) -> Result<()> {
        enum Frame {
            Enter(u32, usize, bool),
            Exit(u32, bool),
        }
        let budget = reads.budget();
        let mut stack = BudgetVec::new(budget);
        for reference in top_level.iter().rev() {
            stack.try_push(Frame::Enter(reference.number(), 1, false))?;
        }
        let mut visited = Table::new();
        let mut nodes = 0;
        let mut edges = top_level.len();
        check_edges(edges)?;
        let mut widgets = 0;
        while let Some(frame) = stack.pop() {
            budget.poll()?;
            match frame {
                Frame::Enter(number, depth, inherited) => {
                    if visited.insert(number, (), budget)?.is_some() {
                        continue;
                    }
                    nodes += 1;
                    if nodes > MAX_NODES {
                        return Err(Error::malformed(format!(
                            "AcroForm field tree exceeds the {MAX_NODES}-node safety limit"
                        )));
                    }
                    let Some(dict) = node(reads, number)? else {
                        continue;
                    };
                    let button = match dict.get("FT") {
                        Some(kind) => match resolved_button(reads, kind)? {
                            Some(button) => button,
                            None => inherited,
                        },
                        None => inherited,
                    };
                    stack.try_push(Frame::Exit(number, button))?;
                    let Some((_, kids)) = kids(reads, number, &dict)? else {
                        continue;
                    };
                    edges = edges.saturating_add(kids.len());
                    check_edges(edges)?;
                    for kid in kids.iter().rev() {
                        budget.poll()?;
                        let kid = classify(reads, kid, self.removed, self.retained)?;
                        if matches!(
                            kid,
                            Kid::Widget(_) | Kid::Unusable | Kid::Intermediate { widget: true, .. }
                        ) {
                            widgets += 1;
                            if widgets > MAX_WIDGETS {
                                return Err(Error::malformed(format!(
                                    "AcroForm field tree exceeds the {MAX_WIDGETS}-widget safety limit"
                                )));
                            }
                        }
                        let (Kid::Field(kid) | Kid::Intermediate { number: kid, .. }) = kid else {
                            continue;
                        };
                        if visited.get(kid).is_some() {
                            continue;
                        }
                        if depth >= MAX_DEPTH {
                            return Err(Error::malformed(format!(
                                "AcroForm field tree exceeds the {MAX_DEPTH}-level safety limit"
                            )));
                        }
                        stack.try_push(Frame::Enter(kid, depth + 1, button))?;
                    }
                }
                Frame::Exit(number, button) => {
                    let Some(dict) = node(reads, number)? else {
                        continue;
                    };
                    let own = looks_like_widget(&dict);
                    let mut covered = Coverage {
                        widgets: own,
                        kept: own && self.placement(reads, number)?.kept,
                        button,
                    };
                    if let Some((_, kids)) = kids(reads, number, &dict)? {
                        for kid in kids.iter() {
                            budget.poll()?;
                            match classify(reads, kid, self.removed, self.retained)? {
                                Kid::Widget(kid) => {
                                    covered.widgets = true;
                                    covered.kept |= self.placement(reads, kid)?.kept;
                                }
                                Kid::Field(kid) | Kid::Intermediate { number: kid, .. } => {
                                    if let Some(kid) = self.coverage.get(kid) {
                                        covered.widgets |= kid.widgets;
                                        covered.kept |= kid.kept;
                                    }
                                }
                                Kid::Unusable | Kid::Ignored => {}
                            }
                        }
                    }
                    self.coverage.insert(number, covered, budget)?;
                    self.walked.try_push(number)?;
                }
            }
        }
        Ok(())
    }

    /// Returns whether the walk keeps `kid`, a `/Kids` or `/Fields` entry:
    /// a widget that stays, a field with such a widget or with no widget at
    /// all, and every entry that is no widget or field.
    fn keeps(&mut self, reads: &mut Reads<'_>, kid: &Value) -> Result<bool> {
        Ok(match classify(reads, kid, self.removed, self.retained)? {
            Kid::Widget(number) => self.placement(reads, number)?.kept,
            Kid::Field(number) | Kid::Intermediate { number, .. } => self
                .coverage
                .get(number)
                .is_none_or(|coverage| coverage.kept || !coverage.widgets),
            Kid::Unusable | Kid::Ignored => true,
        })
    }

    /// Plans the removal of the entries of field `number`'s `/Kids` that
    /// the walk does not keep, and of the same entries of a button field's
    /// `/Opt`, which holds one export value per kid (ISO 32000-2
    /// §12.7.5.2.4) when its length matches. Fields that leave are pruned
    /// too, because a retained widget's `/Parent` can still reach one.
    fn prune(
        &mut self,
        reads: &mut Reads<'b>,
        index: &References,
        edits: &mut Edits<'_>,
        number: u32,
        planned: &mut Table<()>,
    ) -> Result<()> {
        let budget = reads.budget();
        let Some(dict) = node(reads, number)? else {
            return Ok(());
        };
        let Some((location, kids)) = kids(reads, number, &dict)? else {
            return Ok(());
        };
        let mut gone = BudgetVec::new(budget);
        for (at, kid) in kids.iter().enumerate() {
            if at % 1024 == 0 {
                budget.poll()?;
            }
            if !self.keeps(reads, kid)? {
                gone.try_push(at)?;
            }
        }
        if gone.is_empty() {
            return Ok(());
        }
        if self
            .coverage
            .get(number)
            .is_some_and(|coverage| coverage.button)
            && let Some((options, items)) = array_entry(reads, number, &dict, "Opt")?
            && items.len() == kids.len()
            && first_plan(&options, planned, budget)?
        {
            for &at in gone.iter() {
                options.remove(reads, index, edits, at)?;
            }
        }
        if first_plan(&location, planned, budget)? {
            for &at in gone.iter() {
                location.remove(reads, index, edits, at)?;
                if let Some(Value::Reference(kid)) = kids.get(at) {
                    edits.orphan(kid.number())?;
                }
            }
        }
        Ok(())
    }

    /// Returns the fields that the retained top-level fields reach through
    /// kept `/Kids` entries, ascending.
    fn kept(&mut self, reads: &mut Reads<'b>, top_level: &[u32]) -> Result<BudgetVec<'b, u32>> {
        let budget = reads.budget();
        let mut kept = BudgetVec::new(budget);
        let mut seen = Table::new();
        let mut stack = BudgetVec::new(budget);
        stack.try_extend_from_slice(top_level)?;
        while let Some(number) = stack.pop() {
            budget.poll()?;
            if seen.insert(number, (), budget)?.is_some() {
                continue;
            }
            kept.try_push(number)?;
            let Some(dict) = node(reads, number)? else {
                continue;
            };
            let Some((_, kids)) = kids(reads, number, &dict)? else {
                continue;
            };
            for entry in kids.iter() {
                budget.poll()?;
                if let Kid::Field(kid) | Kid::Intermediate { number: kid, .. } =
                    classify(reads, entry, self.removed, self.retained)?
                    && self.keeps(reads, entry)?
                {
                    stack.try_push(kid)?;
                }
            }
        }
        kept.sort_unstable();
        Ok(kept)
    }

    /// Points the retained widgets of field `number`, itself included when
    /// it is merged with its widget, whose `/P` names no retained page at
    /// the retained page that lists them.
    fn point_widgets(
        &mut self,
        reads: &mut Reads<'_>,
        index: &References,
        edits: &mut Edits<'_>,
        number: u32,
    ) -> Result<()> {
        let Some(dict) = node(reads, number)? else {
            return Ok(());
        };
        if looks_like_widget(&dict) {
            self.point(reads, index, edits, number)?;
        }
        if let Some((_, kids)) = kids(reads, number, &dict)? {
            for kid in kids.iter() {
                reads.budget().poll()?;
                if let Kid::Widget(kid) = classify(reads, kid, self.removed, self.retained)? {
                    self.point(reads, index, edits, kid)?;
                }
            }
        }
        Ok(())
    }

    fn point(
        &mut self,
        reads: &mut Reads<'_>,
        index: &References,
        edits: &mut Edits<'_>,
        widget: u32,
    ) -> Result<()> {
        let Some(page) = self.placement(reads, widget)?.page else {
            return Ok(());
        };
        let Some(dict) = node(reads, widget)? else {
            return Ok(());
        };
        let stale = match dict.get("P") {
            Some(Value::Reference(to)) => {
                !is_in(self.retained, to.number())
                    || reads.generation(to.number())? != Some(to.generation())
            }
            Some(_) => true,
            None => false,
        };
        let Some(object) = readable(reads, page)? else {
            return Ok(());
        };
        if stale {
            let to = Reference::new(page, object.generation());
            edits.apply(
                reads,
                index,
                widget,
                &[Step::Key("P".into())],
                Rule::Retarget(to),
            )?;
        }
        Ok(())
    }

    /// Returns where widget `number` lies. It stays when a retained page
    /// lists it in `/Annots`; otherwise it goes when a removed page lists
    /// it, or when no page does and its `/P` names a removed page.
    fn placement(&mut self, reads: &mut Reads<'_>, number: u32) -> Result<Placement> {
        if let Some(&placement) = self.placements.get(number) {
            return Ok(placement);
        }
        let mut page = None;
        let mut listed = false;
        for &holder in self.index.referrers(number) {
            reads.budget().poll()?;
            if self.is_page(holder) {
                if lists(reads, holder, number)? {
                    listed = true;
                    if page.is_none() && is_in(self.retained, holder) {
                        page = Some(holder);
                    }
                }
                continue;
            }
            for &owner in self.index.referrers(holder) {
                reads.budget().poll()?;
                if self.is_page(owner)
                    && annotations_object(reads, owner, holder)?
                    && holds(reads, holder, number)?
                {
                    listed = true;
                    if page.is_none() && is_in(self.retained, owner) {
                        page = Some(owner);
                    }
                }
            }
        }
        let kept = page.is_some()
            || (!listed
                && match node(reads, number)?.as_ref().and_then(|dict| dict.get("P")) {
                    Some(&Value::Reference(to)) => {
                        let current = reads.generation(to.number())? == Some(to.generation());
                        if self.copy {
                            current && is_in(self.retained, to.number())
                        } else {
                            !(current && is_in(self.removed, to.number()))
                        }
                    }
                    _ => !self.copy,
                });
        let placement = Placement { page, kept };
        self.placements.insert(number, placement, reads.budget())?;
        Ok(placement)
    }

    fn is_page(&self, number: u32) -> bool {
        is_in(self.retained, number) || is_in(self.removed, number)
    }
}

/// Records that the rules filter the array at `location`, and returns
/// whether they did not already.
fn first_plan(location: &Location, planned: &mut Table<()>, budget: &Budget) -> Result<bool> {
    if location.keys != [None, None] {
        return Ok(true);
    }
    Ok(planned.insert(location.number, (), budget)?.is_none())
}

fn check_edges(edges: usize) -> Result<()> {
    if edges > MAX_EDGES {
        return Err(Error::malformed(format!(
            "AcroForm field tree exceeds the {MAX_EDGES}-edge safety limit"
        )));
    }
    Ok(())
}

/// Returns the name that `value`, or the object it refers to, is.
fn resolved_button(reads: &mut Reads<'_>, value: &Value) -> Result<Option<bool>> {
    Ok(match value {
        Value::Name(name) => Some(name.as_bytes() == b"Btn"),
        Value::Reference(reference) => match readable(reads, reference.number())? {
            Some(object) if object.generation() == reference.generation() => {
                match &**object.value() {
                    Value::Name(name) => Some(name.as_bytes() == b"Btn"),
                    _ => None,
                }
            }
            _ => None,
        },
        _ => None,
    })
}

/// Returns whether page `page`'s own `/Annots` array lists object `number`.
fn lists(reads: &mut Reads<'_>, page: u32, number: u32) -> Result<bool> {
    let Some(dict) = node_or_page(reads, page)? else {
        return Ok(false);
    };
    Ok(match dict.get("Annots") {
        Some(Value::Array(items)) => references(reads, items, number)?,
        _ => false,
    })
}

/// Returns whether page `page`'s `/Annots` is array object `array`.
fn annotations_object(reads: &mut Reads<'_>, page: u32, array: u32) -> Result<bool> {
    let Some(dict) = node_or_page(reads, page)? else {
        return Ok(false);
    };
    Ok(match dict.get("Annots") {
        Some(Value::Reference(to)) if to.number() == array => {
            reads.generation(array)? == Some(to.generation())
        }
        _ => false,
    })
}

/// Returns whether array object `array` holds a reference to `number`.
fn holds(reads: &mut Reads<'_>, array: u32, number: u32) -> Result<bool> {
    Ok(match readable(reads, array)? {
        Some(object) => match &**object.value() {
            Value::Array(items) => references(reads, items, number)?,
            _ => false,
        },
        None => false,
    })
}

fn references(reads: &mut Reads<'_>, items: &Array, number: u32) -> Result<bool> {
    let Some(generation) = reads.generation(number)? else {
        return Ok(false);
    };
    for (index, item) in items.iter().enumerate() {
        if index % 1024 == 0 {
            reads.budget().poll()?;
        }
        if matches!(item, Value::Reference(to) if *to == Reference::new(number, generation)) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn node_or_page(reads: &mut Reads<'_>, number: u32) -> Result<Option<Dict>> {
    match readable(reads, number)? {
        Some(object) => dict_of(&object)
            .map(|dict| copy_dict_for_edit(dict, reads.budget()))
            .transpose(),
        None => Ok(None),
    }
}
