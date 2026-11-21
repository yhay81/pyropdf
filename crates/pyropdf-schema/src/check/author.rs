//! Reciprocal links that have to exist when an authored object is attached.
//! The ordinary page, field, thread and structure operations maintain these
//! links while editing. A standalone write must present the same graph.
use std::collections::HashSet;

use super::{Graph, View, Walk};

#[derive(Clone, Copy)]
enum Relation {
    Includes(u32),
    ChildrenOf(u32, &'static [u8]),
}

impl<G: Graph> Walk<'_, G> {
    pub(super) fn backlinks(
        &mut self,
        number: u32,
        value: &G::Value,
        name: &str,
    ) -> Result<(), G::Error> {
        let forward = match name {
            "DPartRoot" => Some(("DPartRootNode", b"Parent".as_slice(), "14.12")),
            "DPart" => Some(("DParts", b"Parent".as_slice(), "14.12")),
            _ if name.starts_with("Field") => Some(("Kids", b"Parent".as_slice(), "12.7.3")),
            _ => None,
        };
        if let Some((key, back, clause)) = forward
            && let Some(children) = self.graph.get(value, key.as_bytes())
            && !self.related(
                children,
                Relation::ChildrenOf(number, back),
                0,
                &mut HashSet::new(),
            )?
        {
            self.invariant(
                Some(number),
                key,
                &format!("{name}/{key}"),
                clause,
                format!("/{key} contains a child that does not point back to {number}"),
            );
        }
        let relationships: &[(&str, &[&str], &str)] = match name {
            "PageObject" | "PageTreeNode" => &[("Parent", &["Kids"], "7.7.3")],
            "StructElem" => &[("P", &["K"], "14.7.2")],
            "Bead" | "BeadFirst" => &[
                ("N", &["V"], "12.4.3"),
                ("V", &["N"], "12.4.3"),
                ("P", &["B"], "12.4.3"),
            ],
            "Thread" => &[("F", &["T"], "12.4.3")],
            "DPart" => &[("Parent", &["DParts", "DPartRootNode"], "14.12")],
            "OutlineItem" => &[("Next", &["Prev"], "12.3.3"), ("Prev", &["Next"], "12.3.3")],
            _ if name.starts_with("Field") || name == "AnnotWidget" => {
                &[("Parent", &["Kids"], "12.7.3")]
            }
            _ => return Ok(()),
        };
        for &(key, accepted, clause) in relationships {
            let Some(raw) = self.graph.get(value, key.as_bytes()) else {
                continue;
            };
            let Some(owner) = self.graph.target(raw)? else {
                continue;
            };
            let mut includes = false;
            for alternate in accepted {
                if let Some(other) = self.graph.get(&owner, alternate.as_bytes())
                    && self.related(other, Relation::Includes(number), 0, &mut HashSet::new())?
                {
                    includes = true;
                    break;
                }
            }
            if !includes {
                self.invariant(
                    Some(number),
                    key,
                    &format!("{name}/{key}"),
                    clause,
                    format!(
                        "/{key} lacks a reciprocal reference to {number} in /{}",
                        accepted.join(" or /")
                    ),
                );
            }
        }
        Ok(())
    }

    /// Search only arrays in an owner's reciprocal entry. An indirect name
    /// tree or arbitrary child dictionary is never treated as a membership
    /// list. Malformed array cycles stop at the descriptor's nesting bound.
    fn related(
        &self,
        raw: &G::Value,
        relation: Relation,
        depth: usize,
        seen: &mut HashSet<u32>,
    ) -> Result<bool, G::Error> {
        if depth >= 32 {
            return Ok(false);
        }
        self.graph.visit(false)?;
        let owned;
        let value = match self.graph.view(raw) {
            View::Reference(id) => {
                owned = self.graph.target(raw)?;
                let Some(value) = owned.as_deref() else {
                    return Ok(false);
                };
                if matches!(relation, Relation::Includes(number) if id == number) {
                    return Ok(true);
                }
                if seen.contains(&id) {
                    return Ok(false);
                }
                self.graph.visit(true)?;
                #[expect(
                    clippy::disallowed_methods,
                    reason = "each new reference visit is charged before insertion"
                )]
                seen.insert(id);
                value
            }
            _ => raw,
        };
        if matches!(self.graph.view(value), View::Array(_)) {
            let any = matches!(relation, Relation::Includes(_));
            for item in self.graph.elements(value) {
                if self.related(item, relation, depth + 1, seen)? == any {
                    return Ok(any);
                }
            }
            return Ok(!any);
        }
        if let Relation::ChildrenOf(parent, key) = relation
            && let Some(raw_parent) = self.graph.get(value, key)
            && self.graph.view(raw_parent) == View::Reference(parent)
        {
            return Ok(self.graph.target(raw_parent)?.is_some());
        }
        Ok(false)
    }
}
