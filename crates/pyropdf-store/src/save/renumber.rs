//! Renumbering: the consecutive numbers that a full save gives the objects
//! that it writes, on request (DESIGN.md section 4.4).

use super::{Result, Row, Streams, raw, recode};
use crate::budget::{Budget, BudgetVec};
use crate::value::{Bytes, Reference, Value};
use crate::version::{Slot, Version};
use crate::{Error as StoreError, write};

/// The numbers of a renumbering save: the version's numbers of the objects
/// that it writes, in ascending order. The new number of each is its
/// position plus one, with generation 0.
pub(super) struct Renumbering<'a> {
    numbers: BudgetVec<'a, u32>,
}

impl<'a> Renumbering<'a> {
    /// Returns the renumbering of the in-use rows of `rows`, which holds
    /// the numbers of a full save in ascending order.
    pub(super) fn new(rows: &[Row], budget: &'a Budget) -> Result<Self> {
        let mut numbers = BudgetVec::new(budget);
        for row in rows {
            budget.poll()?;
            if row.number != 0 && row.offset.is_some() {
                numbers.try_push(row.number)?;
            }
        }
        Ok(Self { numbers })
    }

    /// Returns the new number of the object `number`, or none for one that
    /// the save does not write.
    pub(super) fn get(&self, number: u32) -> Option<u32> {
        let at = self.numbers.binary_search(&number).ok()?;
        u32::try_from(at + 1).ok()
    }

    /// Returns the new number of the object `number`, which the save
    /// writes.
    pub(super) fn number(&self, number: u32) -> Result<u32> {
        self.get(number)
            .ok_or_else(|| StoreError::malformed("a written object has no new number").into())
    }

    /// Returns the number of objects that the save renumbers, which is the
    /// last new number.
    pub(super) fn len(&self) -> u64 {
        self.numbers.len() as u64
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
                self.number(row.number)?
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
    pub(super) fn rewrite(&self, value: &mut Value, budget: &Budget) -> Result<()> {
        let mut stack = BudgetVec::new(budget);
        stack.try_push(value)?;
        while let Some(value) = stack.pop() {
            match value {
                Value::Reference(reference) => {
                    *value = match self.get(reference.number()) {
                        Some(number) => Value::Reference(Reference::new(number, 0)),
                        None => Value::Null,
                    };
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

    /// Returns a copy of `value` with its references renumbered, read again
    /// from its bytes so that the copy grows through `budget`.
    pub(super) fn copy(&self, value: &Value, budget: &Budget) -> Result<Value> {
        let mut bytes = BudgetVec::new(budget);
        write::value(&mut bytes, value)?;
        let mut value = Value::parse(&Bytes::new(bytes.into_vec()), budget)?;
        self.rewrite(&mut value, budget)?;
        Ok(value)
    }

    /// Returns the plain bytes of the object `reference` of `version` with
    /// its references renumbered, and its stream recoded as `streams` asks
    /// where [`full_with`](super::full_with) would recode it. The value is
    /// read from the object's bytes, so that it grows through `budget`.
    pub(super) fn bytes(
        &self,
        version: &Version,
        reference: Reference,
        streams: Streams,
        budget: &Budget,
    ) -> Result<Bytes> {
        self.read(version, reference, streams, budget)
            .map_err(|error| {
                error.context(format!(
                    "saving object {} {}",
                    reference.number(),
                    reference.generation()
                ))
            })
    }

    fn read(
        &self,
        version: &Version,
        reference: Reference,
        streams: Streams,
        budget: &Budget,
    ) -> Result<Bytes> {
        let base = version.base();
        let source = match version.slot(reference.number()) {
            Some(Slot::Written(object)) => object.bytes().clone(),
            _ => raw::body(&**base, reference, budget)?,
        };
        let mut value = Value::parse_with_lengths(&source, budget, &|reference| {
            base.object(reference, budget)
        })?;
        self.rewrite(&mut value, budget)?;
        let mut plain = BudgetVec::new(budget);
        write::value(&mut plain, &value)?;
        if streams != Streams::Keep
            && let Some(recoded) = recode::recoded(&value, streams, budget)?
        {
            let mut bytes = BudgetVec::new(budget);
            write::value(&mut bytes, &recoded)?;
            // A compressed stream replaces the data only where it is shorter.
            if streams == Streams::Decompress || bytes.len() < plain.len() {
                plain = bytes;
            }
        }
        Ok(Bytes::new(plain.into_vec()))
    }
}
