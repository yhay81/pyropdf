//! Default visibility of optional-content groups copied into a retained page Form.

use std::collections::HashMap;
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, try_insert_map};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use super::{PageError, Result, import, resolve, target, tree::Root};

/// The catalog's ordered group references and their effective default states.
#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct States {
    groups: Vec<Reference>,
    visible: HashMap<Reference, bool>,
}

fn catalog(version: &Version, budget: &Budget) -> Result<(Reference, Arc<Value>)> {
    let Some(&Value::Reference(root)) = version.trailer().get("Root") else {
        return Err(PageError::Malformed(
            "the trailer has no indirect catalog".into(),
        ));
    };
    let Some(value) = target(version, root, budget)? else {
        return Err(PageError::Malformed("the catalog is missing".into()));
    };
    Ok((root, value))
}

fn dictionary(version: &Version, value: Option<&Value>, budget: &Budget) -> Result<Dict> {
    match resolve(version, value, budget)? {
        Some(Value::Dict(dict)) => Ok(dict),
        None => Ok(Dict::new()),
        _ => Err(PageError::Malformed(
            "optional-content properties require a dictionary".into(),
        )),
    }
}

fn array(version: &Version, value: Option<&Value>, budget: &Budget) -> Result<Array> {
    match resolve(version, value, budget)? {
        Some(Value::Array(array)) => {
            budget.check(LimitCode::OptionalContentCount, array.len() as u64)?;
            Ok(array)
        }
        None => Ok(Array::new()),
        _ => Err(PageError::Malformed(
            "optional-content group lists require an array".into(),
        )),
    }
}

impl States {
    pub(super) fn read(version: &Version, budget: &Budget) -> Result<Self> {
        let (_, catalog) = catalog(version, budget)?;
        let Value::Dict(catalog) = &*catalog else {
            return Err(PageError::Malformed(
                "the catalog is not a dictionary".into(),
            ));
        };
        let props = dictionary(version, catalog.get("OCProperties"), budget)?;
        let default = dictionary(version, props.get("D"), budget)?;
        let base = !matches!(resolve(version, default.get("BaseState"), budget)?, Some(Value::Name(name)) if name.as_bytes() == b"OFF");
        let mut groups = BudgetVec::new(budget);
        let mut visible = HashMap::new();
        for item in array(version, props.get("OCGs"), budget)?.iter() {
            budget.poll()?;
            if let Value::Reference(group) = item
                && !visible.contains_key(group)
            {
                groups.try_push(*group)?;
                try_insert_map(&mut visible, *group, base, budget)?;
            }
        }
        for (key, state) in [("ON", true), ("OFF", false)] {
            for item in array(version, default.get(key), budget)?.iter() {
                budget.poll()?;
                if let Value::Reference(group) = item
                    && let Some(current) = visible.get_mut(group)
                {
                    *current = state;
                }
            }
        }
        Ok(Self {
            groups: groups.into_vec(),
            visible,
        })
    }

    pub(super) fn empty(&self) -> bool {
        self.groups.is_empty()
    }

    pub(super) fn matches(
        version: &Version,
        copied: &[(Reference, bool)],
        budget: &Budget,
    ) -> Result<bool> {
        if copied.is_empty() {
            return Ok(true);
        }
        let target = Self::read(version, budget)?;
        Ok(copied
            .iter()
            .all(|(group, state)| target.visible.get(group) == Some(state)))
    }

    /// Import the reachable resource graph once and register just the groups
    /// actually reached. Each old group maps to the same new reference in both
    /// the Form resources and the destination's default configuration.
    pub(super) fn copy(
        &self,
        transaction: &mut Transaction,
        version: &Version,
        resources: &Value,
        group: Option<&Value>,
        budget: &Budget,
    ) -> Result<(Reference, Option<Value>, Vec<(Reference, bool)>)> {
        let root = Root::read(transaction.version(), budget)?.reference;
        let values = [
            copy_for_edit(resources, budget)?,
            copy_for_edit(group.unwrap_or(&Value::Null), budget)?,
        ];
        let along = import::Along {
            values: &values,
            found: &self.groups,
            ..import::Along::default()
        };
        let imported = import::import(transaction, version, Vec::new(), &[], along, root, budget)?;
        let mut copied = BudgetVec::new(budget);
        for (source, target) in self.groups.iter().zip(imported.found) {
            if let Some(target) = target {
                copied.try_push((target, self.visible[source]))?;
            }
        }
        let copied = copied.into_vec();
        if !copied.is_empty() {
            register(transaction, &copied, budget)?;
        }
        let reference = transaction.allocate()?;
        let mut values = imported.values.into_iter();
        let resources = values.next().unwrap_or(Value::Null);
        transaction.write(reference.number(), &resources, budget)?;
        let group = values.next().filter(|value| *value != Value::Null);
        Ok((reference, group, copied))
    }
}

#[expect(
    clippy::disallowed_methods,
    reason = "one fixed optional-content array per call"
)]
fn append(
    tx: &mut Transaction,
    dict: &mut Dict,
    key: &'static str,
    groups: &[(Reference, bool)],
    state: Option<bool>,
    budget: &Budget,
) -> Result<()> {
    let mut items = BudgetVec::new(budget);
    for item in array(tx.version(), dict.get(key), budget)?.iter() {
        items.try_push(copy_for_edit(item, budget)?)?;
    }
    for &(group, visible) in groups {
        if state.is_none_or(|state| state == visible) {
            items.try_push(Value::Reference(group))?;
        }
    }
    budget.check(LimitCode::OptionalContentCount, items.len() as u64)?;
    dict.insert(Name::from(key), Value::Array(Array::from(items.into_vec())));
    Ok(())
}

#[expect(
    clippy::disallowed_methods,
    reason = "the two fixed optional-content dictionary entries"
)]
fn register(tx: &mut Transaction, groups: &[(Reference, bool)], budget: &Budget) -> Result<()> {
    use crate::annotations::version::{PdfVersion, require_version};
    require_version(tx, PdfVersion(1, 5), budget)?;
    let (root, object) = catalog(tx.version(), budget)?;
    let Value::Dict(mut catalog) = copy_for_edit(&object, budget)? else {
        return Err(PageError::Malformed(
            "the catalog is not a dictionary".into(),
        ));
    };
    let mut props = dictionary(tx.version(), catalog.get("OCProperties"), budget)?;
    let mut default = dictionary(tx.version(), props.get("D"), budget)?;
    append(tx, &mut props, "OCGs", groups, None, budget)?;
    append(tx, &mut default, "ON", groups, Some(true), budget)?;
    append(tx, &mut default, "OFF", groups, Some(false), budget)?;
    props.insert(Name::from("D"), Value::Dict(default));
    catalog.insert(Name::from("OCProperties"), Value::Dict(props));
    tx.write(root.number(), &Value::Dict(catalog), budget)?;
    Ok(())
}
