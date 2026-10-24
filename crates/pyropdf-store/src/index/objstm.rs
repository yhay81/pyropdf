//! Object streams, each decoded once (ISO 32000-2 section 7.5.7).

use std::borrow::Cow;
use std::ops::Range;
use std::sync::OnceLock;

use hayro_syntax::object as syntax;

use super::{Entry, Location, Source, Ticker, decoded, find, lex};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Bytes, Dict, Reference, Value};
use crate::{Error, Result};

/// An object stream, decoded: its data, and the member table at its start.
#[derive(Debug)]
pub struct ObjectStream {
    data: Bytes,
    /// The members that the table lists, in its order: the object number
    /// that it gives each, and where the member begins in `data`.
    members: Vec<(u32, usize)>,
    /// The object numbers of the entries that name a member of the stream
    /// which the table does not confirm, ascending.
    mismatched: Vec<u32>,
}

/// An object stream's data and member table, before the check against the
/// entries that name it.
pub(super) struct Decoded {
    data: Bytes,
    members: Vec<(u32, usize)>,
}

impl ObjectStream {
    /// Returns the stream of `decoded`, checked against `named`: the index
    /// in the table and the object number of each entry that names the
    /// stream.
    pub(super) fn new(decoded: Decoded, named: &[(u32, u32)], budget: &Budget) -> Result<Self> {
        let mut mismatched = BudgetVec::new(budget);
        for &(index, number) in named {
            if lookup(&decoded.members, index).is_none_or(|(listed, _)| listed != number) {
                mismatched.try_push(number)?;
            }
        }
        mismatched.sort_unstable();
        Ok(Self {
            data: decoded.data,
            members: decoded.members,
            mismatched: mismatched.into_vec(),
        })
    }

    /// Returns the decoded data of the stream.
    #[must_use]
    pub const fn data(&self) -> &Bytes {
        &self.data
    }

    /// Returns the members that the table lists, in its order: the object
    /// number that it gives each, and where the member begins in the data.
    #[must_use]
    pub fn members(&self) -> &[(u32, usize)] {
        &self.members
    }

    /// Returns the object numbers of the entries that name a member of the
    /// stream which the table does not confirm, ascending: the table lists
    /// another number at the entry's index, or lists no member there. Such
    /// an entry names no object.
    #[must_use]
    pub fn mismatched(&self) -> &[u32] {
        &self.mismatched
    }

    /// Returns the bytes of the member at `index`, from where its value
    /// begins to the end of the data, if the table lists object `number`
    /// there.
    pub(super) fn member(&self, index: u32, number: u32) -> Option<Bytes> {
        member(&self.data, &self.members, index, number)
    }
}

impl Decoded {
    /// Returns the members that the table lists, in its order.
    pub(super) fn members(&self) -> &[(u32, usize)] {
        &self.members
    }

    /// Returns the bytes of the member at `index`, as
    /// [`ObjectStream::member`] does.
    pub(super) fn member(&self, index: u32, number: u32) -> Option<Bytes> {
        member(&self.data, &self.members, index, number)
    }
}

/// Returns the bytes of the member at `index` of `members`, from where its
/// value begins in `data` to the end of the data, if `members` lists object
/// `number` there.
fn member(data: &Bytes, members: &[(u32, usize)], index: u32, number: u32) -> Option<Bytes> {
    let (listed, start) = lookup(members, index)?;
    if listed != number {
        return None;
    }
    let start = lex::skip_space(data, start);
    data.slice(start..data.len())
}

/// Returns the member at `index` of `members`.
fn lookup(members: &[(u32, usize)], index: u32) -> Option<(u32, usize)> {
    members.get(usize::try_from(index).ok()?).copied()
}

/// The object streams that the entries of an index name, each decoded once.
#[derive(Debug, Default)]
pub(super) struct Streams {
    /// The streams, ascending by object number.
    containers: Vec<Container>,
    /// The index in the table and the object number of each compressed
    /// entry, grouped by stream in the order of `containers`, and ascending
    /// by index within each.
    named: Vec<(u32, u32)>,
}

/// An object stream that entries name.
#[derive(Debug)]
pub(super) struct Container {
    number: u32,
    /// The entries of [`Streams::named`] that name the stream.
    named: Range<usize>,
    /// What decoding the stream found, once it has been decoded.
    outcome: OnceLock<Result<ObjectStream>>,
}

impl Streams {
    /// Returns the object streams that the compressed entries among
    /// `entries` name, none decoded yet.
    pub(super) fn new(entries: &[(u32, Entry)], budget: &Budget) -> Result<Self> {
        let mut named = BudgetVec::new(budget);
        for &(number, entry) in entries {
            if let Location::Member { stream, index } = entry.location {
                named.try_push((stream, index, number))?;
            }
        }
        named.sort_unstable();
        let mut containers = BudgetVec::new(budget);
        let mut start = 0;
        for (end, &(stream, ..)) in named.iter().enumerate() {
            if named.get(end + 1).is_none_or(|&(next, ..)| next != stream) {
                containers.try_push(Container {
                    number: stream,
                    named: start..end + 1,
                    outcome: OnceLock::new(),
                })?;
                start = end + 1;
            }
        }
        let mut pairs = BudgetVec::new(budget);
        pairs.try_reserve_exact(named.len())?;
        pairs.try_extend(named.iter().map(|&(_, index, number)| (index, number)))?;
        Ok(Self {
            containers: containers.into_vec(),
            named: pairs.into_vec(),
        })
    }

    /// Returns object stream `number`, if an entry names it.
    pub(super) fn container(&self, number: u32) -> Option<&Container> {
        self.containers
            .binary_search_by_key(&number, |container| container.number)
            .ok()
            .map(|index| &self.containers[index])
    }

    /// Returns the index in the table and the object number of each entry
    /// that names `container`.
    pub(super) fn named(&self, container: &Container) -> &[(u32, u32)] {
        &self.named[container.named.clone()]
    }

    /// Keeps `decoded` as object stream `number`, which a scan decoded, if
    /// an entry names it.
    pub(super) fn fill(&self, number: u32, decoded: Decoded, budget: &Budget) -> Result<()> {
        if let Some(container) = self.container(number) {
            let stream = ObjectStream::new(decoded, self.named(container), budget)?;
            container.keep(Ok(stream));
        }
        Ok(())
    }
}

impl Container {
    /// Returns what decoding the stream found, if it has been decoded.
    pub(super) fn outcome(&self) -> Option<&Result<ObjectStream>> {
        self.outcome.get()
    }

    /// Keeps `outcome` unless another read kept its own first, and returns
    /// what is kept. Decoding finds the same whichever read decodes.
    pub(super) fn keep(&self, outcome: Result<ObjectStream>) -> &Result<ObjectStream> {
        self.outcome.get_or_init(|| outcome)
    }
}

/// Decodes object stream `number`, which `source` locates, and reads its
/// member table.
///
/// The stream must be an uncompressed object of generation 0, as ISO
/// 32000-2 section 7.5.8.3 requires of the stream of a compressed entry.
/// Its `/N` and `/First` are direct integers or references to uncompressed
/// integers. When `/Length` is a reference, which section 7.5.7 keeps out of
/// object streams, the length that it names decides the data when
/// `endstream` follows that many bytes, as for a direct length; otherwise,
/// as for a wrong direct length, the data runs to the first `endstream`
/// without the white space before it, as hayro-syntax reads it.
///
/// The table holds `/N` pairs of unsigned integers, separated by white
/// space and comments: an object number, and the member's offset from
/// `/First`.
pub(super) fn decode(source: &Source<'_>, number: u32, budget: &Budget) -> Result<Decoded> {
    let reference = Reference::new(number, 0);
    let bytes = source.uncompressed(reference).ok_or_else(|| {
        Error::malformed("no entry locates it as an uncompressed object of generation 0")
    })?;
    let stream = super::stream(&bytes)
        .ok_or_else(|| Error::malformed("it is not a stream that hayro-syntax reads"))?;
    let dict = dictionary(&bytes, &stream, budget)?;
    let count = integer(source, dict.get("N"), budget)?
        .ok_or_else(|| Error::malformed("its /N is not a number of members"))?;
    let first = integer(source, dict.get("First"), budget)?
        .ok_or_else(|| Error::malformed("its /First is not an offset"))?;
    let length = match dict.get("Length") {
        Some(&Value::Reference(length)) => resolve(source, length, budget)?,
        _ => None,
    };
    let data = data(&bytes, &stream, length, budget)?;
    let mut members = BudgetVec::new(budget);
    let mut ticker = Ticker::default();
    let mut pos = 0;
    for _ in 0..count {
        ticker.tick(budget)?;
        let (listed, end) = lex::unsigned(&data, lex::skip_space(&data, pos))
            .ok_or_else(|| Error::malformed("its member table ends early"))?;
        let (offset, end) = lex::unsigned(&data, lex::skip_space(&data, end))
            .ok_or_else(|| Error::malformed("its member table ends early"))?;
        let listed = u32::try_from(listed)
            .map_err(|_| Error::malformed("its member table lists an object number beyond u32"))?;
        // A member that begins past the data reads as no object.
        let start = first
            .checked_add(offset)
            .and_then(|start| usize::try_from(start).ok())
            .unwrap_or(usize::MAX);
        members.try_push((listed, start))?;
        pos = end;
    }
    Ok(Decoded {
        data,
        members: members.into_vec(),
    })
}

/// Returns the dictionary of `stream`, which hayro-syntax parsed from
/// `bytes`, as a value.
fn dictionary(bytes: &Bytes, stream: &syntax::Stream<'_>, budget: &Budget) -> Result<Dict> {
    let span = bytes
        .share(stream.dict().data())
        .ok_or_else(|| Error::malformed("its dictionary is not in its bytes"))?;
    match Value::parse(&span, budget)? {
        Value::Dict(dict) => Ok(dict),
        _ => Err(Error::malformed("its dictionary does not read as one")),
    }
}

/// Returns the decoded data of `stream`, which hayro-syntax parsed from
/// `bytes` and whose `/Length` names `length`, and charges it to `budget`.
///
/// When the length decides the data and hayro-syntax, which cannot resolve
/// it, read other data, the stream decodes from a copy whose dictionary
/// ends with the length as a direct value, which hayro-syntax reads in
/// place of the reference: of a key that a dictionary repeats, it keeps the
/// last value.
fn data(
    bytes: &Bytes,
    stream: &syntax::Stream<'_>,
    length: Option<u64>,
    budget: &Budget,
) -> Result<Bytes> {
    let raw = stream.raw_data();
    let start = bytes
        .offset_of(&raw)
        .ok_or_else(|| Error::malformed("its data is not in its bytes"))?;
    let declared = length
        .and_then(|length| usize::try_from(length).ok())
        .and_then(|length| start.checked_add(length))
        .filter(|&end| {
            end <= bytes.len()
                && lex::keyword(bytes, lex::skip_white(bytes, end), b"endstream").is_some()
        });
    let decoded = match declared {
        Some(end) if end != start + raw.len() => {
            let dict = stream.dict().data();
            let entries = dict
                .strip_prefix(b"<<")
                .and_then(|dict| dict.strip_suffix(b">>"))
                .ok_or_else(|| Error::malformed("its dictionary is not in its bytes"))?;
            let length = format!(" /Length {} >>\nstream\n", end - start);
            let mut copy = BudgetVec::new(budget);
            copy.try_reserve_exact(2 + entries.len() + length.len() + end - start + 10)?;
            copy.try_extend_from_slice(b"<<")?;
            copy.try_extend_from_slice(entries)?;
            copy.try_extend_from_slice(length.as_bytes())?;
            copy.try_extend_from_slice(&bytes[start..end])?;
            copy.try_extend_from_slice(b"\nendstream")?;
            let stream = super::stream(&copy)
                .ok_or_else(|| Error::malformed("it does not read with its length"))?;
            decoded(&stream, budget)?.into_owned()
        }
        _ => match decoded(stream, budget)? {
            Cow::Borrowed(data) => {
                return bytes
                    .share(data)
                    .ok_or_else(|| Error::malformed("its data is not in its bytes"));
            }
            Cow::Owned(data) => data,
        },
    };
    // The index keeps the decoded data, so it counts as memory of the
    // operation that decoded it.
    budget.charge(LimitCode::MemorySize, super::count(decoded.len()))?;
    Ok(Bytes::from(decoded))
}

/// Returns the non-negative integer that `value` is or names through an
/// uncompressed object, or `None` if it is neither.
fn integer(source: &Source<'_>, value: Option<&Value>, budget: &Budget) -> Result<Option<u64>> {
    match value {
        Some(&Value::Integer(integer)) => Ok(u64::try_from(integer).ok()),
        Some(&Value::Reference(reference)) => resolve(source, reference, budget),
        _ => Ok(None),
    }
}

/// Returns the non-negative integer that the uncompressed object named by
/// `reference` holds, or `None` if it holds none.
fn resolve(source: &Source<'_>, reference: Reference, budget: &Budget) -> Result<Option<u64>> {
    let Some(bytes) = source.uncompressed(reference) else {
        return Ok(None);
    };
    match Value::parse(&bytes, budget) {
        Ok(Value::Integer(integer)) => Ok(u64::try_from(integer).ok()),
        Err(error) if error.limit().is_some() => Err(error),
        _ => Ok(None),
    }
}

/// Returns whether `entries` locate object `number` as an uncompressed
/// object of generation 0, which may be the stream of a compressed entry.
pub(super) fn is_container(entries: &[(u32, Entry)], number: u32) -> bool {
    matches!(
        find(entries, number),
        Some(Entry {
            generation: 0,
            location: Location::Offset(_),
        })
    )
}
