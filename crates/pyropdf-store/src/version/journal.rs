//! The bytes that a document's history is saved as and restored from
//! (#428).
//!
//! A saved history holds the versions of the history, oldest first, each
//! as what it holds differently from the version before it, and the oldest
//! as what it holds differently from the base. It holds no object of the
//! base: restoring it needs the document it was saved from, which the
//! fingerprint of the base names. The objects that transactions wrote are
//! kept as the bytes the writer wrote, so a restored version holds exactly
//! what the saved one held, unencrypted.
//!
//! The bytes are, with integers little-endian:
//!
//! ```text
//! magic        b"PYROPDF-HISTORY\n"
//! format       u32, 1
//! fingerprint  32 bytes: SHA-256 of the base
//! position     u32: the index of the current version
//! versions     u32 count, then for each version:
//!   label      u32 length, UTF-8: the name of the step that made it
//!   next       u64: the version's next-number bound
//!   trailer    u8 0, or u8 1 and u32 length and the bytes of the trailer
//!   objects    u32 count, then for each: u32 number, then u8 0, u16
//!              generation, u32 length, and the bytes of the value, or
//!              u8 1 and the u16 generation of a free entry
//!   marks      u32 count, then for each: u8 structure, u32 number
//! checksum     32 bytes: SHA-256 of every byte before it
//! ```

use std::sync::Arc;

use sha2::{Digest, Sha256};

use super::{Base, Slot, Stale, Trailer, Version, Written, difference};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::settle::{Mark, Structure};
use crate::value::{Bytes, Reference, Value};
use crate::{Error, Result, write};

const MAGIC: &[u8; 16] = b"PYROPDF-HISTORY\n";
const FORMAT: u32 = 1;
const HASH: usize = 32;

/// A version that restoring builds, with the bytes that it holds and the
/// version before it does not share, and the converse.
pub(super) struct Built {
    pub(super) version: Arc<Version>,
    pub(super) added: u64,
    pub(super) released: u64,
}

/// What restoring a saved history builds before it touches the document.
pub(super) struct Restored<'b> {
    pub(super) versions: BudgetVec<'b, Built>,
    /// The names of the steps: one fewer than the versions.
    pub(super) labels: BudgetVec<'b, String>,
    pub(super) position: usize,
}

/// Returns the SHA-256 of `base`: of its file's bytes, or of its objects
/// and trailer for a base held in memory.
pub(super) fn fingerprint(base: &dyn Base, budget: &Budget) -> Result<[u8; HASH]> {
    let mut hash = Sha256::new();
    if let Some(index) = base.file() {
        hash.update(b"file");
        for chunk in index.bytes().chunks(1 << 20) {
            budget.poll()?;
            hash.update(chunk);
        }
        return Ok(hash.finalize().into());
    }
    hash.update(b"memory");
    let mut out = BudgetVec::new(budget);
    write::value(&mut out, &Value::Dict(base.trailer().clone()))?;
    hash.update((out.len() as u64).to_le_bytes());
    hash.update(out.as_slice());
    let mut numbers = BudgetVec::new(budget);
    base.numbers(&mut numbers)?;
    numbers.sort_unstable();
    let mut last = None;
    for &number in numbers.iter() {
        if last == Some(number) {
            continue;
        }
        last = Some(number);
        budget.poll()?;
        hash.update(number.to_le_bytes());
        match base.generation(number) {
            Some(generation) => {
                let bytes = base
                    .object(Reference::new(number, generation), budget)?
                    .unwrap_or_default();
                hash.update(generation.to_le_bytes());
                hash.update((bytes.len() as u64).to_le_bytes());
                hash.update(&bytes[..]);
            }
            None => hash.update(base.free_generation(number).unwrap_or(0).to_le_bytes()),
        }
    }
    Ok(hash.finalize().into())
}

/// Returns the bytes of the history whose versions are `versions`, oldest
/// first, with the current one at `position`. `labels` name the steps as
/// [`super::Document::save_history`] says.
pub(super) fn save(
    versions: &[Arc<Version>],
    position: usize,
    (labels, undo): (&[String], usize),
    budget: &Budget,
) -> Result<Vec<u8>> {
    let Some(oldest) = versions.first() else {
        return Err(Error::malformed("a history has at least one version"));
    };
    let mut out = Out(BudgetVec::new(budget));
    out.bytes(MAGIC)?;
    out.u32(FORMAT)?;
    out.bytes(&fingerprint(oldest.base().as_ref(), budget)?)?;
    out.count(position)?;
    out.count(versions.len())?;
    // Step `step` makes version `step + 1`, and the step that undo takes
    // first is step `position - 1`, which `labels[undo - 1]` names.
    let label = |step: usize| {
        (step + undo)
            .checked_sub(position)
            .and_then(|at| labels.get(at))
            .map_or("", String::as_str)
    };
    let opened = oldest.opened();
    for (index, version) in versions.iter().enumerate() {
        let (label, before) = match index.checked_sub(1) {
            Some(step) => (label(step), versions[step].as_ref()),
            None => ("", &opened),
        };
        out.blob(label.as_bytes())?;
        out.bytes(&version.next.to_le_bytes())?;
        let changes = difference(before, version, budget)?.changes;
        match (changes.trailer(), &version.trailer) {
            (true, Some(trailer)) => {
                out.bytes(&[1])?;
                out.blob(&trailer.bytes)?;
            }
            _ => out.bytes(&[0])?,
        }
        out.count(changes.objects().len())?;
        for &number in changes.objects() {
            budget.poll()?;
            out.u32(number)?;
            match version.slot(number) {
                Some(Slot::Written(object)) => {
                    out.bytes(&[0])?;
                    out.bytes(&object.generation.to_le_bytes())?;
                    out.blob(&object.bytes)?;
                }
                Some(&Slot::Free(generation)) => {
                    out.bytes(&[1])?;
                    out.bytes(&generation.to_le_bytes())?;
                }
                None => {
                    return Err(Error::malformed(format!(
                        "object {number} changed between versions of the history without a slot"
                    )));
                }
            }
        }
        let mut marks = BudgetVec::new(budget);
        marks.try_extend(version.stale.marks())?;
        out.count(marks.len())?;
        for mark in marks.iter() {
            out.bytes(&[u8::try_from(mark.structure.index()).unwrap_or(u8::MAX)])?;
            out.u32(mark.number)?;
        }
    }
    let checksum: [u8; HASH] = Sha256::digest(out.0.as_slice()).into();
    out.bytes(&checksum)?;
    Ok(out.0.into_vec())
}

/// Reads the history that `data` holds for the document whose opened
/// version is `opened`, and builds its versions from that version, without
/// touching the document. Returns `None` if the history belongs to another
/// document. Refuses as malformed what is not a saved history, and with a
/// limit what the budget does not allow.
pub(super) fn restore<'b>(
    opened: &Arc<Version>,
    data: &[u8],
    budget: &'b Budget,
) -> Result<Option<Restored<'b>>> {
    let Some(body) = data.len().checked_sub(HASH).map(|end| &data[..end]) else {
        return Err(corrupt("it is too short"));
    };
    let checksum: [u8; HASH] = Sha256::digest(body).into();
    if checksum[..] != data[body.len()..] {
        return Err(corrupt("its checksum does not match"));
    }
    let mut input = In(body);
    if input.take(MAGIC.len())? != MAGIC {
        return Err(corrupt("it does not begin as one"));
    }
    if input.u32()? != FORMAT {
        return Err(corrupt("its format is not 1"));
    }
    if input.take(HASH)? != fingerprint(opened.base().as_ref(), budget)? {
        return Ok(None);
    }
    let position = input.count()?;
    let count = input.count()?;
    if count == 0 || position >= count {
        return Err(corrupt("its position is not one of its versions"));
    }
    let mut versions: BudgetVec<'b, Built> = BudgetVec::new(budget);
    let mut labels = BudgetVec::new(budget);
    for index in 0..count {
        let label =
            std::str::from_utf8(input.blob()?).map_err(|_| corrupt("a step name is not UTF-8"))?;
        if index > 0 {
            budget.charge(LimitCode::MemorySize, label.len() as u64)?;
            labels.try_push(label.to_owned())?;
        }
        let before = versions.last().map_or(opened, |built| &built.version);
        let version = read_version(before, &mut input, budget)?;
        let difference = difference(before, &version, budget)?;
        if index > 0 && difference.changes.is_empty() && !difference.stale {
            return Err(corrupt("a step changes nothing"));
        }
        let mut version = version;
        version.written = difference.changes;
        budget.charge(LimitCode::MemorySize, Version::OVERHEAD)?;
        versions.try_push(Built {
            version: Arc::new(version),
            added: difference.second,
            released: difference.first,
        })?;
    }
    if !input.0.is_empty() {
        return Err(corrupt("bytes follow its last version"));
    }
    Ok(Some(Restored {
        versions,
        labels,
        position,
    }))
}

/// Reads one version, as what it holds differently from `before`.
fn read_version(before: &Version, input: &mut In<'_>, budget: &Budget) -> Result<Version> {
    let mut version = before.draft();
    let next = u64::from_le_bytes(input.array()?);
    if next > 1 << 32 {
        return Err(corrupt("a next-number bound is above 4294967296"));
    }
    version.next = version.next.max(next);
    match input.take(1)? {
        [0] => {}
        [1] => {
            let bytes = owned(input.blob()?, budget)?;
            let Value::Dict(value) = Value::parse(&bytes, budget)? else {
                return Err(corrupt("a trailer is not a dictionary"));
            };
            budget.charge(LimitCode::MemorySize, Trailer::OVERHEAD)?;
            version.trailer = Some(Arc::new(Trailer { value, bytes }));
        }
        _ => return Err(corrupt("a trailer flag is neither 0 nor 1")),
    }
    let mut last = None;
    for _ in 0..input.count()? {
        budget.poll()?;
        let number = input.u32()?;
        if number == 0 || last.is_some_and(|last| number <= last) {
            return Err(corrupt("its object numbers are not ascending above 0"));
        }
        last = Some(number);
        let slot = match input.take(1)? {
            [0] => {
                let generation = u16::from_le_bytes(input.array()?);
                let bytes = owned(input.blob()?, budget)?;
                Value::parse(&bytes, budget)
                    .map_err(|error| error.context(format!("object {number} {generation}")))?;
                budget.charge(LimitCode::MemorySize, Written::OVERHEAD)?;
                Slot::Written(Arc::new(Written::new(generation, bytes)))
            }
            [1] => Slot::Free(u16::from_le_bytes(input.array()?)),
            _ => return Err(corrupt("an object kind is neither 0 nor 1")),
        };
        version.table.insert(number, slot, budget)?;
        version.next = version.next.max(u64::from(number) + 1);
    }
    let mut stale = Stale::default();
    for _ in 0..input.count()? {
        let [structure] = input.array()?;
        let structure = *Structure::ALL
            .get(usize::from(structure))
            .ok_or_else(|| corrupt("a stale mark names no structure"))?;
        let number = input.u32()?;
        stale.insert(Mark { structure, number }, budget)?;
    }
    version.stale = stale;
    Ok(version)
}

/// Returns a copy of `bytes`, charged to the budget.
fn owned(bytes: &[u8], budget: &Budget) -> Result<Bytes> {
    budget.charge(LimitCode::MemorySize, bytes.len() as u64)?;
    Ok(Bytes::new(Box::<[u8]>::from(bytes)))
}

fn corrupt(what: &str) -> Error {
    Error::malformed(format!("the data is not a saved history: {what}"))
}

/// The bytes being written, charged to the budget.
struct Out<'b>(BudgetVec<'b, u8>);

impl Out<'_> {
    fn bytes(&mut self, bytes: &[u8]) -> Result<()> {
        Ok(self.0.try_extend_from_slice(bytes)?)
    }

    fn u32(&mut self, value: u32) -> Result<()> {
        self.bytes(&value.to_le_bytes())
    }

    fn count(&mut self, count: usize) -> Result<()> {
        self.u32(
            u32::try_from(count).map_err(|_| {
                Error::malformed("the history holds more than 4294967295 of something")
            })?,
        )
    }

    fn blob(&mut self, bytes: &[u8]) -> Result<()> {
        self.count(bytes.len())?;
        self.bytes(bytes)
    }
}

/// The bytes being read. Every read takes bytes, so every loop over a count
/// ends within the input.
struct In<'a>(&'a [u8]);

impl<'a> In<'a> {
    fn take(&mut self, length: usize) -> Result<&'a [u8]> {
        if length > self.0.len() {
            return Err(corrupt("it ends early"));
        }
        let (head, tail) = self.0.split_at(length);
        self.0 = tail;
        Ok(head)
    }

    fn array<const N: usize>(&mut self) -> Result<[u8; N]> {
        let mut array = [0; N];
        array.copy_from_slice(self.take(N)?);
        Ok(array)
    }

    fn u32(&mut self) -> Result<u32> {
        Ok(u32::from_le_bytes(self.array()?))
    }

    fn count(&mut self) -> Result<usize> {
        usize::try_from(self.u32()?).map_err(|_| corrupt("a count does not fit"))
    }

    fn blob(&mut self) -> Result<&'a [u8]> {
        let length = self.count()?;
        self.take(length)
    }
}
