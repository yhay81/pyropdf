//! Object streams: the objects that a save packs into them on request
//! (DESIGN.md section 4.4, ISO 32000-2 section 7.5.7).

use super::recode::StreamPolicy;
use super::renumber::Renumbering;
use super::{Format, Mode, Output, Result, Row, Streams, raw, recode};
use crate::budget::{Budget, BudgetVec};
use crate::security;
use crate::value::{Bytes, Dict, Name, Reference, Stream, Value};
use crate::version::{Slot, Version};
use crate::{Error as StoreError, ErrorKind};

/// The members of one object stream, as lopdf and pylopdf pack them.
const MEMBERS: u32 = 100;

/// Fills object streams with the objects of a save, and writes each one
/// when it is full.
pub(super) struct Packer<'a> {
    budget: &'a Budget,
    /// The Flate level of the object streams, or none for uncompressed ones.
    level: Option<u8>,
    /// Output formatting (ASCII safety and pretty printing).
    format: Format,
    /// The next object number that the save has not given out.
    next: u64,
    /// The object number of the stream being filled, once it has a member.
    number: Option<u32>,
    /// The table of the stream being filled: the object number and the
    /// offset of each member.
    table: BudgetVec<'a, u8>,
    /// The members' values, each followed by a line feed.
    values: BudgetVec<'a, u8>,
    count: u32,
    /// The cross-reference rows of the object streams written.
    rows: BudgetVec<'a, Row>,
}

impl<'a> Packer<'a> {
    /// Returns a packer whose object streams take the numbers from `next`
    /// on.
    pub(super) fn new(budget: &'a Budget, level: Option<u8>, next: u64, format: Format) -> Self {
        Self {
            budget,
            level,
            format,
            next,
            number: None,
            table: BudgetVec::new(budget),
            values: BudgetVec::new(budget),
            count: 0,
            rows: BudgetVec::new(budget),
        }
    }

    /// Packs the object of `row` into an object stream and returns true, or
    /// returns false for an object that an object stream may not hold: a
    /// stream, an object with a generation other than 0, the `/Encrypt`
    /// dictionary `encrypt`, and an object that does not parse. A
    /// `renumbering` save packs each member under its new reference, whose
    /// generation decides.
    pub(super) fn pack(
        &mut self,
        version: &Version,
        row: &mut Row,
        encrypt: Option<u32>,
        renumbering: Option<&Renumbering<'_>>,
        out: &mut Output<'_>,
        mode: &Mode<'_>,
    ) -> Result<bool> {
        let budget = self.budget;
        if row.offset.is_none() || Some(row.number) == encrypt {
            return Ok(false);
        }
        let reference = Reference::new(row.number, row.generation);
        let member = match renumbering {
            Some(renumbering) => renumbering.number(row.number)?,
            None => reference,
        };
        if member.generation() != 0 {
            return Ok(false);
        }
        let object = match version.get(row.number, budget) {
            Ok(Some(object)) if !matches!(**object.value(), Value::Stream(_)) => object,
            Ok(_) => return Ok(false),
            Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => return Ok(false),
            Err(error) => return Err(error.into()),
        };
        let member = member.number();
        let number = match self.number {
            Some(number) => number,
            None => {
                let number = take(&mut self.next)?;
                self.number = Some(number);
                number
            }
        };
        let offset = self.values.len();
        // A member is never encrypted on its own: its object stream is.
        match (renumbering, version.slot(row.number)) {
            (Some(renumbering), _) => {
                let keep = StreamPolicy::new(Streams::Keep, &[], budget)?;
                let bytes = renumbering.bytes(
                    version,
                    reference,
                    &keep,
                    self.format,
                    false,
                    None,
                    budget,
                )?;
                self.values.try_extend_from_slice(&bytes)?;
            }
            (None, _) if self.format.rewrites_syntax() => {
                self.format.write_value(&mut self.values, object.value())?;
            }
            (None, Some(Slot::Written(object))) => {
                self.values.try_extend_from_slice(object.bytes())?;
            }
            (None, _) => {
                let bytes = raw::body(&**version.base(), reference, budget).map_err(|error| {
                    error.context(format!("saving object {} {}", row.number, row.generation))
                })?;
                self.values.try_extend_from_slice(&bytes)?;
            }
        }
        self.values.try_push(b'\n')?;
        self.table
            .try_extend_from_slice(format!("{member} {offset} ").as_bytes())?;
        row.member = Some((number, self.count));
        self.count += 1;
        if self.count == MEMBERS {
            self.flush(out, mode)?;
        }
        Ok(true)
    }

    /// Writes the object stream being filled, if it has a member.
    pub(super) fn flush(&mut self, out: &mut Output<'_>, mode: &Mode<'_>) -> Result<()> {
        let Some(number) = self.number.take() else {
            return Ok(());
        };
        let budget = self.budget;
        out.budget.poll()?;
        let first = self.table.len();
        let mut data = BudgetVec::new(budget);
        data.try_extend_from_slice(&self.table)?;
        data.try_extend_from_slice(&self.values)?;
        self.table.clear();
        self.values.clear();
        let entries = [
            (Name::from("Type"), Value::Name(Name::from("ObjStm"))),
            (Name::from("N"), Value::Integer(i64::from(self.count))),
            (
                Name::from("First"),
                Value::Integer(i64::try_from(first).unwrap_or(i64::MAX)),
            ),
        ];
        self.count = 0;
        #[expect(
            clippy::disallowed_methods,
            reason = "the dictionary of an object stream has three entries"
        )]
        let dict: Dict = entries.into_iter().collect();
        let data_guard =
            recode::MemoryGuard::new(budget, u64::try_from(data.capacity()).unwrap_or(u64::MAX));
        let mut stream = recode::RecodedStream::new_with_guard(
            Stream::new(dict, Bytes::new(data.into_vec())),
            data_guard,
        );
        if let Some(level) = self.level
            && let Some(compressed) = recode::compressed_temp(stream.stream(), level, budget)?
        {
            stream = compressed;
        }
        if self.format.ascii && !recode::is_printable_ascii(stream.stream().data()) {
            stream = recode::wrap_ascii85_temp(stream.stream(), &|_, _| Ok(None), budget)?;
        }
        let plain = stream.into_value_guard();
        let encrypted = mode
            .handler()
            .map(|handler| {
                security::encrypt_value(handler, Reference::new(number, 0), plain.value(), budget)
            })
            .transpose()?;
        let mut bytes = BudgetVec::new(budget);
        self.format.write_value(
            &mut bytes,
            encrypted.as_ref().unwrap_or_else(|| plain.value()),
        )?;
        drop(encrypted);
        drop(plain);
        self.rows.try_push(Row {
            number,
            generation: 0,
            offset: Some(out.offset()?),
            next_free: 0,
            member: None,
        })?;
        out.display(format_args!("{number} 0 obj\n"))?;
        out.bytes(&bytes)?;
        out.bytes(b"\nendobj\n")
    }

    /// Returns the next object number that the save has not given out, and
    /// the rows of the object streams written, after [`Packer::flush`].
    pub(super) fn finish(self) -> (u64, BudgetVec<'a, Row>) {
        (self.next, self.rows)
    }
}

/// Gives out the object number `next` for an object that the save adds.
pub(super) fn take(next: &mut u64) -> Result<u32> {
    let number = u32::try_from(*next)
        .map_err(|_| StoreError::malformed("no object number is left for the save's streams"))?;
    *next += 1;
    Ok(number)
}
