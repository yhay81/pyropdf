//! PDF/UA-2 description and tab order changes for zero-sized widget annotations.

use super::*;
use crate::annotations::Reader;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::tree::number_tree_pairs;

/// ISO 14289-2 §8.9.2.4.13 treats a widget of zero width and height as an artifact.
#[expect(
    clippy::float_cmp,
    reason = "the rule requires exactly equal opposite edges"
)]
pub(super) fn zero_size(version: &Version, dict: &Dict, budget: &Budget) -> Result<bool> {
    let Some(rect) = dict
        .get("Rect")
        .filter(|_| named(dict, "Subtype", b"Widget"))
    else {
        return Ok(false);
    };
    let reader = crate::annotations::Reader::new(version, budget);
    Ok(reader
        .numbers_of(rect, 4)?
        .is_some_and(|coords| matches!(&coords[..], [x0, y0, x1, y1] if x0 == x1 && y0 == y1)))
}

const WIDGET_DESCRIPTION: &str = "the invisible widget lacks a description in Contents";
const PAGE_TABS: &str = "the page containing a widget lacks a valid tab order";

struct WidgetRule {
    reason: &'static str,
    subject: u32,
    cause: u32,
    has_invisible_widget: bool,
}

pub(super) fn changes<'b>(
    opened: &Version,
    current: &Version,
    written: &[u32],
    found: &mut Reasons<'b>,
    budget: &'b Budget,
) -> Result<()> {
    let current = widget_rules(current, written, budget)?;
    if !current.iter().any(|rule| rule.has_invisible_widget) {
        return Ok(());
    }
    let previous = widget_rules(opened, written, budget)?;
    for rule in current.iter() {
        if rule.has_invisible_widget
            && previous
                .binary_search_by_key(&(rule.reason, rule.subject), |r| (r.reason, r.subject))
                .is_err()
        {
            record(found, rule.reason, Some(rule.cause), budget)?;
        }
    }
    Ok(())
}

/// The before and after sets distinguish a newly broken rule from a page or widget
/// which already failed it. A page with annotations already required Tabs, and
/// an undescribed widget already lacked Contents before it was resized to zero.
fn widget_rules<'b>(
    version: &Version,
    written: &[u32],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, WidgetRule>> {
    let mut out = BudgetVec::new(budget);
    let reader = Reader::new(version, budget);
    let mut reads = Reads::new(version, budget);
    let pages = PageIndex::build(&mut reads)?;
    let mut label_parents = None;
    let changed =
        |v: Option<&Value>| reference_number(v).filter(|n| written.binary_search(n).is_ok());
    for page in pages.pages() {
        budget.poll()?;
        let Some(page_number) = page.object().map(|r| r.number()) else {
            continue;
        };
        let dict = page.dict();
        let annots = reader.entry(dict, "Annots")?;
        let Some(Value::Array(items)) = annots.as_deref() else {
            continue;
        };
        let holder = changed(dict.get("Annots")).unwrap_or(page_number);
        let mut has_annotation = false;
        let mut has_invisible_widget = false;
        for item in items.iter() {
            budget.poll()?;
            let annot = reader.resolve(item)?;
            let Some(widget) = value_dict(&annot) else {
                continue;
            };
            has_annotation = true;
            if !named(widget, "Subtype", b"Widget") {
                continue;
            }
            let invisible = zero_size(version, widget, budget)?;
            has_invisible_widget |= invisible;
            let mut lookup = LabelLookup {
                reader: &reader,
                written,
                changed: None,
            };
            if !matches!(
                reader.entry(widget, "Contents")?.as_deref(),
                Some(Value::String(_))
            ) && !has_label(&mut lookup, item, widget, &mut label_parents)?
            {
                let cause = changed(Some(item))
                    .or_else(|| changed(widget.get("Contents")))
                    .or_else(|| changed(widget.get("Rect")))
                    .or(lookup.changed)
                    .unwrap_or(holder);
                out.try_push(WidgetRule {
                    reason: WIDGET_DESCRIPTION,
                    subject: reference_number(Some(item)).unwrap_or(holder),
                    cause,
                    has_invisible_widget: invisible,
                })?;
            }
        }
        if has_annotation
            && !matches!(reader.entry(dict, "Tabs")?.as_deref(),
            Some(Value::Name(name)) if matches!(name.as_bytes(), b"A" | b"W" | b"S"))
        {
            out.try_push(WidgetRule {
                reason: PAGE_TABS,
                subject: page_number,
                cause: changed(dict.get("Tabs")).unwrap_or(holder),
                has_invisible_widget,
            })?;
        }
    }
    out.as_mut_slice()
        .sort_unstable_by_key(|rule| (rule.reason, rule.subject));
    Ok(out)
}

/// A change to a label, its containing structure element, or its role map can
/// invalidate an otherwise untouched widget. Retain the changed reference on this path.
struct LabelLookup<'a, 'b> {
    reader: &'a Reader<'b>,
    written: &'a [u32],
    changed: Option<u32>,
}

impl LabelLookup<'_, '_> {
    fn observe(&mut self, value: Option<&Value>) {
        if self.changed.is_none() {
            self.changed =
                reference_number(value).filter(|n| self.written.binary_search(n).is_ok());
        }
    }

    fn resolve<'v>(&mut self, value: &'v Value) -> Result<crate::annotations::Resolved<'v>> {
        self.observe(Some(value));
        Ok(self.reader.resolve(value)?)
    }

    fn entry<'v>(
        &mut self,
        dict: &'v Dict,
        key: &str,
    ) -> Result<Option<crate::annotations::Resolved<'v>>> {
        self.observe(dict.get(key));
        Ok(self.reader.entry(dict, key)?)
    }
}

/// Reuse the bounded parent number tree for all undescribed widgets in this version.
struct LabelParents<'b> {
    entries: BudgetVec<'b, (i64, Value)>,
    role_map: Option<Value>,
}

impl<'b> LabelParents<'b> {
    fn read(reader: &Reader<'b>) -> Result<Self> {
        let mut entries = BudgetVec::new(reader.budget);
        let mut role_map = None;
        if let Some(catalog) = catalog(reader.version, reader.budget)?
            && let Some(catalog) = value_dict(&catalog)
            && let Some(root) = reader.entry(catalog, "StructTreeRoot")?
            && let Some(root) = value_dict(&root)
        {
            if let Some(tree) = root.get("ParentTree") {
                number_tree_pairs(reader.version, tree, &mut entries, reader.budget)?;
                entries.as_mut_slice().sort_unstable_by_key(|(key, _)| *key);
            }
            role_map = root
                .get("RoleMap")
                .map(|v| copy_for_edit(v, reader.budget))
                .transpose()?;
        }
        Ok(Self { entries, role_map })
    }
}

/// The field name and a StructParent number alone are not a widget label.
/// ISO 14289-2 §8.10.2.2 places the label's real content in a Lbl child of the
/// widget's structure parent. K can hold one child or an array.
fn has_label<'b>(
    reader: &mut LabelLookup<'_, 'b>,
    reference: &Value,
    widget: &Dict,
    parents: &mut Option<LabelParents<'b>>,
) -> Result<bool> {
    let key = reader.entry(widget, "StructParent")?;
    let Some(Value::Integer(key)) = key.as_deref() else {
        return Ok(false);
    };
    if parents.is_none() {
        *parents = Some(LabelParents::read(reader.reader)?);
    }
    let Some(parents) = parents else {
        return Ok(false);
    };
    let Ok(index) = parents.entries.binary_search_by_key(key, |(key, _)| *key) else {
        return Ok(false);
    };
    let parent = reader.resolve(&parents.entries[index].1)?;
    let Some(parent) = value_dict(&parent) else {
        return Ok(false);
    };
    let Some(kids) = reader.entry(parent, "K")? else {
        return Ok(false);
    };
    let role_map = parents
        .role_map
        .as_ref()
        .map(|v| reader.resolve(v))
        .transpose()?;
    let roles = role_map.as_deref().and_then(value_dict);
    let (Value::Reference(widget_ref), Value::Array(children)) = (reference, &*kids) else {
        // A label and an object reference require two distinct children.
        return Ok(false);
    };
    let mut owns_widget = false;
    let mut has_label = false;
    for child in children.iter() {
        reader.reader.budget.poll()?;
        let value = reader.resolve(child)?;
        if let Some(dict) = value_dict(&value) {
            owns_widget |= named(dict, "Type", b"OBJR")
                && matches!(dict.get("Obj"), Some(Value::Reference(target)) if target == widget_ref);
            has_label |= is_label(reader, dict, roles)?;
        }
        if owns_widget && has_label {
            return Ok(true);
        }
    }
    Ok(false)
}

fn is_label(reader: &mut LabelLookup<'_, '_>, child: &Dict, roles: Option<&Dict>) -> Result<bool> {
    let contents = reader.entry(child, "K")?;
    if !matches!(
        contents.as_deref(),
        Some(Value::Integer(_) | Value::Dict(_))
    ) && !matches!(contents.as_deref(), Some(Value::Array(items)) if !items.is_empty())
    {
        return Ok(false);
    }
    let role = reader.entry(child, "S")?;
    let Some(Value::Name(name)) = role.as_deref() else {
        return Ok(false);
    };
    let namespace = reader.entry(child, "NS")?;
    label_role(
        reader,
        name.as_bytes(),
        namespace.as_deref().and_then(value_dict),
        roles,
        0,
    )
}

/// Resolve a standard structure role through legacy or namespace-specific maps.
fn label_role(
    reader: &mut LabelLookup<'_, '_>,
    name: &[u8],
    namespace: Option<&Dict>,
    roles: Option<&Dict>,
    depth: usize,
) -> Result<bool> {
    reader.reader.budget.poll()?;
    if depth >= MAX_DEPTH {
        return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
    }
    let ns_map;
    let map = if let Some(ns) = namespace {
        if matches!(reader.entry(ns, "NS")?.as_deref(), Some(Value::String(uri))
            if uri.as_bytes().len() <= 48
                && matches!(text_string(uri.as_bytes()).as_str(), "http://iso.org/pdf2/ssn" | "http://iso.org/pdf/ssn"))
        {
            return Ok(name == b"Lbl");
        }
        ns_map = reader.entry(ns, "RoleMapNS")?;
        ns_map.as_deref().and_then(value_dict)
    } else if name == b"Lbl" {
        return Ok(true);
    } else {
        roles
    };
    let Some(mapping) = map.and_then(|m| m.get(name)) else {
        return Ok(false);
    };
    let mapping = reader.resolve(mapping)?;
    match &*mapping {
        Value::Name(target) => label_role(reader, target.as_bytes(), None, roles, depth + 1),
        Value::Array(pair) if pair.len() == 2 => {
            let Some((target, ns)) = pair.get(0).zip(pair.get(1)) else {
                return Ok(false);
            };
            let target = reader.resolve(target)?;
            let ns = reader.resolve(ns)?;
            let (Value::Name(target), Some(ns)) = (&*target, value_dict(&ns)) else {
                return Ok(false);
            };
            label_role(reader, target.as_bytes(), Some(ns), roles, depth + 1)
        }
        _ => Ok(false),
    }
}
