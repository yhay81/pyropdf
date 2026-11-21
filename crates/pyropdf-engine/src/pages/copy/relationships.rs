//! Copy only the paths from an annotation or page action to targets that actually moved.
//! Files, appearances and model data stay shared unless their own dictionary needs new links.

use super::{
    Budget, Dict, Error, HashMap, LimitCode, LimitExceeded, Name, Reference, Result, Transaction,
    Value, copy_for_edit, locate_reference, map_put,
};
use pyropdf_store::value::MAX_NESTING;

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
enum Role {
    Action,
    AdditionalActions,
    Target,
    ExData,
    Model,
    View,
    Measure,
    RichContent,
    RichSettings,
    Activation,
}

pub(super) struct Links<'a> {
    annotations: &'a HashMap<Reference, Reference>,
    budget: &'a Budget,
    // A completed unchanged subtree is cached too. A node on the current path
    // signals a cycle before any partially rewritten object can be published.
    copies: HashMap<(Reference, Role), Visit>,
}

#[derive(Clone, Copy)]
enum Visit {
    Visiting,
    Done(Option<Reference>),
}

impl<'a> Links<'a> {
    pub(super) fn new(annotations: &'a HashMap<Reference, Reference>, budget: &'a Budget) -> Self {
        Self {
            annotations,
            budget,
            copies: HashMap::new(),
        }
    }

    pub(super) fn page_actions(&mut self, tx: &mut Transaction, page: &mut Dict) -> Result<()> {
        self.field(tx, page, "AA", Role::AdditionalActions, 1)?;
        Ok(())
    }

    pub(super) fn annotation(
        &mut self,
        tx: &mut Transaction,
        dict: &mut Dict,
        page: Reference,
    ) -> Result<()> {
        use Role::*;
        #[expect(
            clippy::disallowed_methods,
            reason = "the annotation grows by at most one page reference"
        )]
        dict.insert(Name::from("P"), Value::Reference(page));
        for (key, role) in [
            ("Popup", Target),
            ("Parent", Target),
            ("IRT", Target),
            ("ExData", ExData),
        ] {
            self.field(tx, dict, key, role, 1)?;
        }
        match name(dict, "Subtype") {
            b"Link" => {
                self.field(tx, dict, "A", Action, 1)?;
            }
            b"Screen" | b"Widget" => {
                self.field(tx, dict, "A", Action, 1)?;
                self.field(tx, dict, "AA", AdditionalActions, 1)?;
            }
            b"3D" => {
                self.field(tx, dict, "3DD", Model, 1)?;
                self.field(tx, dict, "3DV", View, 1)?;
            }
            b"RichMedia" => {
                self.field(tx, dict, "RichMediaContent", RichContent, 1)?;
                self.field(tx, dict, "RichMediaSettings", RichSettings, 1)?;
            }
            _ => {}
        }
        Ok(())
    }

    fn field(
        &mut self,
        tx: &mut Transaction,
        dict: &mut Dict,
        key: &str,
        role: Role,
        depth: usize,
    ) -> Result<bool> {
        if let Some(value) = dict.get(key)
            && let Some(new) = self.rewrite(tx, value, role, depth)?
        {
            *dict.get_mut(key).expect("the changed field exists") = new;
            return Ok(true);
        }
        Ok(false)
    }

    fn rewrite(
        &mut self,
        tx: &mut Transaction,
        value: &Value,
        role: Role,
        depth: usize,
    ) -> Result<Option<Value>> {
        self.budget.poll()?;
        self.budget.check(LimitCode::ObjectDepth, depth as u64)?;
        if depth > MAX_NESTING {
            return Err(LimitExceeded::over(
                LimitCode::ObjectDepth,
                MAX_NESTING as u64,
                depth as u64,
            )
            .into());
        }
        let Value::Reference(reference) = value else {
            return self.body(tx, value, role, depth);
        };
        if let Some(visit) = self.existing(*reference, role) {
            return visit.result();
        }
        let Some((old, object)) = locate_reference(tx, *reference, self.budget)? else {
            return Ok(None);
        };
        if let Some(visit) = self.existing(old, role) {
            return visit.result();
        }
        map_put(&mut self.copies, (old, role), Visit::Visiting, self.budget)?;
        let new = match self.body(tx, object.value(), role, depth)? {
            Some(body) => {
                let new = tx.allocate()?;
                tx.write(new.number(), &body, self.budget)?;
                Some(new)
            }
            None => None,
        };
        *self
            .copies
            .get_mut(&(old, role))
            .expect("visited indirect object") = Visit::Done(new);
        Ok(new.map(Value::Reference))
    }

    fn existing(&self, reference: Reference, role: Role) -> Option<Visit> {
        if role == Role::Target
            && let Some(&target) = self.annotations.get(&reference)
        {
            return Some(Visit::Done(Some(target)));
        }
        self.copies.get(&(reference, role)).copied()
    }

    fn body(
        &mut self,
        tx: &mut Transaction,
        value: &Value,
        role: Role,
        depth: usize,
    ) -> Result<Option<Value>> {
        match value {
            Value::Array(_)
                if matches!(
                    role,
                    Role::Action | Role::View | Role::Measure | Role::Target
                ) => {}
            Value::Dict(_) if role != Role::Target => {}
            Value::Stream(_) if role == Role::Model => {}
            _ => return Ok(None),
        }
        let mut body = copy_for_edit(value, self.budget)?;
        let changed = match &mut body {
            Value::Array(array) => {
                let mut changed = false;
                for slot in array.iter_mut() {
                    if let Some(new) = self.rewrite(tx, slot, role, depth + 1)? {
                        *slot = new;
                        changed = true;
                    }
                }
                changed
            }
            Value::Dict(dict) => self.dictionary(tx, dict, role, depth + 1)?,
            Value::Stream(stream) if role == Role::Model => {
                let views = self.field(tx, stream.dict_mut(), "VA", Role::View, depth + 1)?;
                self.field(tx, stream.dict_mut(), "DV", Role::View, depth + 1)? || views
            }
            _ => false,
        };
        Ok(changed.then_some(body))
    }

    fn dictionary(
        &mut self,
        tx: &mut Transaction,
        d: &mut Dict,
        role: Role,
        depth: usize,
    ) -> Result<bool> {
        use Role::*;
        let mut changed = false;
        let fields: &[(&str, Role)] = match role {
            Action => {
                changed = self.field(tx, d, "Next", Action, depth)?;
                match name(d, "S") {
                    b"Hide" => &[("T", Target)],
                    b"Movie" => &[("Annotation", Target)],
                    b"Rendition" => &[("AN", Target)],
                    b"RichMediaExecute" => &[("TA", Target)],
                    b"GoTo3DView" => {
                        changed |= self.targeted_view(tx, d, ("TA", "V"), depth)?;
                        &[]
                    }
                    _ => &[],
                }
            }
            AdditionalActions => {
                for key in [
                    "E", "X", "D", "U", "Fo", "Bl", "PO", "PC", "PV", "PI", "O", "C",
                ] {
                    changed |= self.field(tx, d, key, Action, depth)?;
                }
                &[]
            }
            ExData => match name(d, "Subtype") {
                b"3DM" => &[("M3DREF", Measure)],
                b"Markup3D" => {
                    changed = self.targeted_view(tx, d, ("3DA", "3DV"), depth)?;
                    &[]
                }
                _ => &[],
            },
            Model => &[("3DD", Model)],
            View => &[("MA", Measure)],
            Measure => &[("S", Target)],
            RichContent => &[("Views", View)],
            RichSettings => &[("Activation", Activation)],
            Activation => &[("View", View)],
            Target => &[],
        };
        for &(key, child) in fields {
            changed |= self.field(tx, d, key, child, depth)?;
        }
        Ok(changed)
    }

    fn targeted_view(
        &mut self,
        tx: &mut Transaction,
        dict: &mut Dict,
        (target, view): (&str, &str),
        depth: usize,
    ) -> Result<bool> {
        let moved = self.field(tx, dict, target, Role::Target, depth)?;
        if moved {
            self.field(tx, dict, view, Role::View, depth)?;
        }
        Ok(moved)
    }
}

impl Visit {
    fn result(self) -> Result<Option<Value>> {
        match self {
            Self::Done(new) => Ok(new.map(Value::Reference)),
            Self::Visiting => Err(Error::malformed(
                "cyclic annotation action or media relationship while copying a page",
            )),
        }
    }
}

fn name<'a>(dict: &'a Dict, key: &str) -> &'a [u8] {
    match dict.get(key) {
        Some(Value::Name(name)) => name.as_bytes(),
        _ => b"",
    }
}
