//! Renumbering: the consecutive numbers that a full save gives the objects
//! that it writes, and the references that follow them and the merged
//! objects, on request (DESIGN.md section 4.4).

use super::merge::Plan;
use super::recode::StreamPolicy;
use super::{Format, Result, Row, raw, recode};
use crate::Error as StoreError;
use crate::budget::{Budget, BudgetVec};
use crate::security::Handler;
use crate::value::{Reference, Value};
use crate::version::{Slot, Version};

/// The references of a save that renumbers or merges objects: the
/// version's numbers and generations of the objects that it writes, in
/// ascending order, and the objects that merge into others. A consecutive
/// renumbering gives each written object its position plus one as its new
/// number, with generation 0; otherwise each keeps its own.
pub(super) struct Renumbering<'a> {
    written: BudgetVec<'a, (u32, u16)>,
    /// Each merged object (number and generation) and the object that it merges
    /// into, in ascending order.
    merged: BudgetVec<'a, (u32, u16, u32)>,
    /// The written objects that reference a merged one, in ascending order.
    referrers: BudgetVec<'a, u32>,
    consecutive: bool,
}

impl<'a> Renumbering<'a> {
    /// Returns the renumbering of the in-use rows of `rows`, which holds
    /// the numbers of a full save in ascending order, with the merged objects
    /// of `plan`, whose rows are free, merging into theirs.
    pub(super) fn new(
        rows: &[Row],
        plan: Plan<'a>,
        consecutive: bool,
        budget: &'a Budget,
    ) -> Result<Self> {
        let mut written = BudgetVec::new(budget);
        for row in rows {
            budget.poll()?;
            if row.number != 0 && row.offset.is_some() {
                written.try_push((row.number, row.generation))?;
            }
        }
        Ok(Self {
            written,
            merged: plan.merged,
            referrers: plan.referrers,
            consecutive,
        })
    }

    /// Returns whether the save rewrites the object `number`: every object
    /// of a consecutive renumbering, and otherwise those that reference a
    /// merged object, so that the others keep their bytes.
    pub(super) fn rewrites(&self, number: u32) -> bool {
        self.consecutive || self.referrers.binary_search(&number).is_ok()
    }

    /// Returns the new reference of the object `number`, or none for one
    /// that the save does not write.
    pub(super) fn get(&self, number: u32) -> Option<Reference> {
        let number = match self
            .merged
            .binary_search_by_key(&number, |&(merged, _, _)| merged)
        {
            Ok(at) => self.merged[at].2,
            Err(_) => number,
        };
        let at = self
            .written
            .binary_search_by_key(&number, |&(written, _)| written)
            .ok()?;
        if self.consecutive {
            Some(Reference::new(u32::try_from(at + 1).ok()?, 0))
        } else {
            Some(Reference::new(number, self.written[at].1))
        }
    }

    /// Returns the new reference of `reference` when its generation matches the
    /// live (or merged) object, or `None` for a missing or stale reference
    /// (ISO 32000-2 section 7.3.10).
    fn get_reference(&self, reference: Reference) -> Option<Reference> {
        let target = match self
            .merged
            .binary_search_by_key(&reference.number(), |&(merged, _, _)| merged)
        {
            Ok(at) => {
                if self.merged[at].1 != reference.generation() {
                    return None;
                }
                let target = self.merged[at].2;
                let idx = self
                    .written
                    .binary_search_by_key(&target, |&(written, _)| written)
                    .ok()?;
                return Some(if self.consecutive {
                    Reference::new(u32::try_from(idx + 1).ok()?, 0)
                } else {
                    Reference::new(target, self.written[idx].1)
                });
            }
            Err(_) => reference,
        };
        let at = self
            .written
            .binary_search_by_key(&target.number(), |&(written, _)| written)
            .ok()?;
        if self.written[at].1 != target.generation() {
            return None;
        }
        if self.consecutive {
            Some(Reference::new(u32::try_from(at + 1).ok()?, 0))
        } else {
            Some(Reference::new(target.number(), self.written[at].1))
        }
    }

    /// Read a filter referenced by an already renumbered stream dictionary.
    pub(super) fn lookup(
        &self,
        version: &Version,
        reference: Reference,
        budget: &Budget,
    ) -> crate::Result<Option<Value>> {
        let original = if self.consecutive {
            let Some(&(number, generation)) = reference
                .number()
                .checked_sub(1)
                .and_then(|n| self.written.get(n as usize))
            else {
                return Ok(None);
            };
            Reference::new(number, generation)
        } else {
            reference
        };
        let Some(mut value) = version.filter_object(original, budget)? else {
            return Ok(None);
        };
        self.rewrite(&mut value, budget)?;
        Ok(Some(value))
    }

    /// Returns the new reference of the object `number`, which the save
    /// writes.
    pub(super) fn number(&self, number: u32) -> Result<Reference> {
        self.get(number)
            .ok_or_else(|| StoreError::malformed("a written object has no new number").into())
    }

    /// Returns the number of objects that the save writes, which is the last
    /// new number of a consecutive renumbering.
    pub(super) fn len(&self) -> u64 {
        self.written.len() as u64
    }

    /// Returns the cross-reference rows of the save: row 0, and each in-use
    /// row of `rows` with its new number and generation 0. Free rows leave,
    /// as the save frees no number but 0.
    pub(super) fn rows<'b>(&self, rows: &[Row], budget: &'b Budget) -> Result<BudgetVec<'b, Row>> {
        let mut renumbered = BudgetVec::new(budget);
        for row in rows {
            budget.poll()?;
            let number = if row.number == 0 {
                0
            } else if row.offset.is_some() {
                self.number(row.number)?.number()
            } else {
                continue;
            };
            renumbered.try_push(Row {
                number,
                generation: if number == 0 { row.generation } else { 0 },
                offset: row.offset,
                next_free: 0,
                member: row.member,
            })?;
        }
        Ok(renumbered)
    }

    /// Renumbers each reference inside `value`, and replaces a reference to
    /// an object that the save does not write with null, as ISO 32000-2
    /// section 7.3.10 reads it. The walk keeps a stack of the values still
    /// to visit, whatever their depth.
    pub(super) fn rewrite(&self, value: &mut Value, budget: &Budget) -> crate::Result<()> {
        let mut stack = BudgetVec::new(budget);
        stack.try_push(value)?;
        while let Some(value) = stack.pop() {
            match value {
                Value::Reference(reference) => {
                    *value = self
                        .get_reference(*reference)
                        .map_or(Value::Null, Value::Reference);
                }
                Value::Array(array) => stack.try_extend(array.iter_mut())?,
                Value::Dict(dict) => stack.try_extend(dict.iter_mut().map(|(_, item)| item))?,
                Value::Stream(stream) => {
                    stack.try_extend(stream.dict_mut().iter_mut().map(|(_, item)| item))?;
                }
                _ => {}
            }
        }
        Ok(())
    }

    /// Returns a copy of `value` with its references renumbered, copied
    /// through `budget`.
    pub(super) fn copy(&self, value: &Value, budget: &Budget) -> Result<Value> {
        let mut value = crate::value::copy_for_edit(value, budget)?;
        self.rewrite(&mut value, budget)?;
        Ok(value)
    }

    /// Returns the serialized bytes of the object `reference` of `version` with
    /// its references renumbered, its stream recoded as `policy` and `format`
    /// ask, encrypted when `encrypt` is given, and formatted once per `format`.
    /// Returned in a [`BudgetVec`] so per-object serialization memory releases
    /// on drop.
    #[expect(
        clippy::too_many_arguments,
        reason = "internal save helper passing renumbering, policy, format, encryption, and budget"
    )]
    pub(super) fn bytes<'b>(
        &self,
        version: &Version,
        reference: Reference,
        policy: &StreamPolicy<'_>,
        format: Format,
        clear: bool,
        encrypt: Option<(&Handler, Reference)>,
        budget: &'b Budget,
    ) -> Result<BudgetVec<'b, u8>> {
        self.read(version, reference, policy, format, clear, encrypt, budget)
            .map_err(|error| {
                error.context(format!(
                    "saving object {} {}",
                    reference.number(),
                    reference.generation()
                ))
            })
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "internal save helper passing renumbering, policy, format, encryption, and budget"
    )]
    fn read<'b>(
        &self,
        version: &Version,
        reference: Reference,
        policy: &StreamPolicy<'_>,
        format: Format,
        clear: bool,
        encrypt: Option<(&Handler, Reference)>,
        budget: &'b Budget,
    ) -> Result<BudgetVec<'b, u8>> {
        let base = version.base();
        let source = match version.slot(reference.number()) {
            Some(Slot::Written(object)) => object.bytes().clone(),
            _ => raw::body(&**base, reference, budget)?,
        };
        let mut value = Value::parse_with_lengths(&source, budget, &|reference| {
            base.object(reference, budget)
        })?;
        if clear {
            crate::security::filters::remove_explicit(
                &mut value,
                &|r, b| version.filter_object(r, b),
                budget,
            )?;
        } else {
            version.check_stream_filters(&value, budget)?;
        }
        self.rewrite(&mut value, budget)?;
        recode::write_formatted(
            &value,
            reference.number(),
            policy,
            format,
            encrypt.map(|(handler, target)| (handler, reference, target)),
            &|r, b| self.lookup(version, r, b),
            budget,
        )
    }
}
