//! Reading the cross-reference data that the file's last `startxref` names
//! (ISO 32000-2 sections 7.5.4 to 7.5.8).
//!
//! The rules that decide what the data gives:
//!
//! - The file's last `startxref` keyword names the newest section. When an
//!   object header or an `xref` or `trailer` keyword follows it, the file
//!   holds an update whose own `startxref` is damaged or missing, and the
//!   data counts as unreadable.
//! - A section is a classic table and its trailer, or a cross-reference
//!   stream, whose dictionary is its trailer. A table's trailer may name a
//!   stream of the same section in `/XRefStm`, which makes a hybrid
//!   section. A trailer's `/Prev` names the previous section. The chain may
//!   hold at most 256 sections and `/XRefStm` streams together, as
//!   hayro-syntax 0.8 reads it, and may not return to a section.
//! - The newest section that lists an object number decides it: in use at
//!   a location, or free.
//!
//! Any section that cannot be read makes the whole data unreadable, so the
//! index never mixes what it read with what a scan finds.

use std::cmp::Reverse;

use hayro_syntax::object as syntax;

use super::lex::{self, Tokens};
use super::{
    Damage, Entry, Listing, Location, Revision, Section, SectionKind, Ticker, decoded, find, objstm,
};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Bytes, Dict, Name, Value};
use crate::{Error, Result};

/// The most sections, `/XRefStm` streams included, that the chain may hold:
/// hayro-syntax 0.8's bound, beyond which it does not read the chain
/// either.
const MAX_SECTIONS: usize = 256;

/// The cross-reference data of a file: the in-use entries that it gives,
/// and the revisions, whose newest section holds the newest trailer.
pub(super) struct Chain {
    /// The in-use entries, ascending by object number.
    pub(super) entries: Vec<(u32, Entry)>,
    /// The revisions, oldest first, which hold at least one section.
    pub(super) revisions: Vec<Revision>,
}

/// One section as it reads.
struct Read<'b> {
    kind: SectionKind,
    trailer: Dict,
    /// What the section lists, in the order of its bytes.
    listings: BudgetVec<'b, (u32, Listing)>,
    /// Where the section ends: after its trailer's dictionary, or after its
    /// stream object.
    end: usize,
}

/// Reads the cross-reference data that the last `startxref` of `bytes`
/// names, whose offsets count from `origin`.
///
/// Refuses with a malformed input error data that cannot be read, and with
/// the budget's limits.
pub(super) fn read(bytes: &Bytes, origin: usize, budget: &Budget) -> Result<Chain> {
    let at = lex::last_startxref(bytes)
        .ok_or_else(|| Error::malformed("the file has no `startxref`"))?;
    let last = lex::startxref(bytes, at)
        .ok_or_else(|| Error::malformed("no offset follows the last `startxref`"))?;
    if let Some(pos) = structure_after(bytes, last.value_end, budget)? {
        return Err(Error::malformed(format!(
            "an object or a cross-reference section at byte {pos} follows the last `startxref`"
        )));
    }
    // The `startxref` lines that name sections: the position of the section
    // that each names, and where its `%%EOF` marker ends.
    let mut lines = BudgetVec::new(budget);
    lines.try_push((locate(origin, last.offset)?, last.eof_end))?;
    let mut sections = BudgetVec::new(budget);
    let mut visited = BudgetVec::new(budget);
    let mut next = Some(lines[0].0);
    while let Some(at) = next {
        let context = || format!("the cross-reference section at byte {at}");
        let read = read_at(bytes, origin, at, &mut visited, budget)
            .map_err(|error| error.context(context()))?;
        let mut kind = read.kind;
        let mut stream = None;
        if kind == SectionKind::Table
            && let Some(pos) =
                link(&read.trailer, "XRefStm", origin).map_err(|error| error.context(context()))?
        {
            let hybrid = read_at(bytes, origin, pos, &mut visited, budget)
                .and_then(|hybrid| match hybrid.kind {
                    SectionKind::Stream => Ok(hybrid),
                    _ => Err(Error::malformed("it is not a cross-reference stream")),
                })
                .map_err(|error| {
                    error
                        .context(format!("its /XRefStm at byte {pos}"))
                        .context(context())
                })?;
            kind = SectionKind::Hybrid { stream: pos };
            stream = Some(hybrid.listings);
        }
        next = link(&read.trailer, "Prev", origin).map_err(|error| error.context(context()))?;
        if let Some(line) = lex::startxref(bytes, lex::skip_space(bytes, read.end))
            && let Ok(pos) = locate(origin, line.offset)
        {
            lines.try_push((pos, line.eof_end))?;
        }
        sections.try_push(Section {
            offset: at,
            kind,
            listings: combine(read.listings, stream, budget)?,
            trailer: read.trailer,
        })?;
    }
    let entries = merge(&sections, budget)?;
    let revisions = revisions(sections, &lines, budget)?;
    Ok(Chain { entries, revisions })
}

impl Chain {
    /// Returns the trailer of the newest section.
    fn trailer(&self) -> Option<&Dict> {
        let revision = self.revisions.last()?;
        Some(&revision.sections.first()?.trailer)
    }

    /// Checks the entries against `bytes`, and returns the damage that the
    /// check finds, if any.
    ///
    /// Every uncompressed entry must point to an object header that gives
    /// its object number and generation, every compressed entry must name a
    /// stream that an uncompressed entry of generation 0 locates, and the
    /// trailer's `/Root` must name an in-use entry of its generation.
    pub(super) fn check(&self, bytes: &[u8], budget: &Budget) -> Result<Option<Damage>> {
        let mut first = None;
        let mut count = 0;
        let mut ticker = Ticker::default();
        for &(number, entry) in &self.entries {
            ticker.tick(budget)?;
            let located = match entry.location {
                Location::Offset(pos) => lex::header(bytes, pos).is_some_and(|header| {
                    header.number == number && header.generation == entry.generation
                }),
                Location::Member { stream, .. } => objstm::is_container(&self.entries, stream),
            };
            if !located {
                first.get_or_insert(number);
                count += 1;
            }
        }
        if let Some(first) = first {
            return Ok(Some(Damage::Entries { first, count }));
        }
        let root = match self.trailer().and_then(|trailer| trailer.get("Root")) {
            Some(Value::Reference(root)) => find(&self.entries, root.number())
                .is_some_and(|entry| entry.generation == root.generation()),
            _ => false,
        };
        Ok((!root).then_some(Damage::Root))
    }
}

/// Reads the section at `at`, unless the chain has read it already or holds
/// as many sections as it may.
fn read_at<'b>(
    bytes: &Bytes,
    origin: usize,
    at: usize,
    visited: &mut BudgetVec<'_, usize>,
    budget: &'b Budget,
) -> Result<Read<'b>> {
    if visited.contains(&at) {
        return Err(Error::malformed("the chain of sections returns to it"));
    }
    if visited.len() == MAX_SECTIONS {
        return Err(Error::malformed(format!(
            "the chain holds more than {MAX_SECTIONS} sections"
        )));
    }
    visited.try_push(at)?;
    let pos = lex::skip_space(bytes, at);
    if let Some(end) = lex::keyword(bytes, pos, b"xref") {
        table(bytes, origin, end, budget)
    } else if let Some(header) = lex::header(bytes, pos) {
        stream(bytes, origin, header, budget)
    } else {
        Err(Error::malformed(
            "neither `xref` nor an object header begins there",
        ))
    }
}

/// Reads the classic table that begins at `pos`, after its keyword `xref`,
/// and the trailer after it (ISO 32000-2 sections 7.5.4 and 7.5.5).
///
/// Each subsection is a header of two integers, the first object number
/// and the number of entries, followed by the entries. hayro-syntax 0.8
/// reads an entry as 20 bytes; the index reads its three tokens, an offset,
/// a generation, and `n` or `f`, separated by any white space, as pdf.js
/// does, so that an entry of 19 or 21 bytes reads as well. The check against
/// the object headers refuses an entry that reads but does not locate its
/// object.
fn table<'b>(bytes: &Bytes, origin: usize, mut pos: usize, budget: &'b Budget) -> Result<Read<'b>> {
    let mut listings = BudgetVec::new(budget);
    let mut ticker = Ticker::default();
    while let Some((first, end)) = lex::unsigned(bytes, lex::skip_space(bytes, pos)) {
        let (count, end) = lex::unsigned(bytes, lex::skip_space(bytes, end)).ok_or_else(|| {
            Error::malformed("a subsection of the table has no number of entries")
        })?;
        pos = end;
        for index in 0..count {
            ticker.tick(budget)?;
            let number = number(first, index)?;
            let unreadable = || {
                Error::malformed(format!(
                    "the table's entry of object {number} cannot be read"
                ))
            };
            let (field, end) =
                lex::unsigned(bytes, lex::skip_space(bytes, pos)).ok_or_else(unreadable)?;
            let (generation, end) =
                lex::unsigned(bytes, lex::skip_space(bytes, end)).ok_or_else(unreadable)?;
            let at = lex::skip_space(bytes, end);
            let (used, end) = match (lex::keyword(bytes, at, b"n"), lex::keyword(bytes, at, b"f")) {
                (Some(end), _) => (true, end),
                (None, Some(end)) => (false, end),
                (None, None) => return Err(unreadable()),
            };
            let generation = u16::try_from(generation).map_err(|_| unreadable())?;
            let listing = if used {
                Listing::InUse(Entry {
                    generation,
                    location: Location::Offset(locate(origin, field)?),
                })
            } else {
                Listing::Free { generation }
            };
            listings.try_push((number, listing))?;
            pos = end;
        }
    }
    let at = lex::skip_space(bytes, pos);
    let start = lex::keyword(bytes, at, b"trailer")
        .map(|end| lex::skip_space(bytes, end))
        .ok_or_else(|| Error::malformed("no `trailer` follows the table"))?;
    let source = bytes
        .slice(start..bytes.len())
        .ok_or_else(|| Error::malformed("the trailer is missing"))?;
    let trailer = match Value::parse(&source, budget) {
        Ok(Value::Dict(trailer)) => trailer,
        Err(error) if error.limit().is_some() => return Err(error),
        _ => return Err(Error::malformed("the trailer is not a dictionary")),
    };
    let end = start + trailer.lexeme().map_or(0, Bytes::len);
    Ok(Read {
        kind: SectionKind::Table,
        trailer,
        listings,
        end,
    })
}

/// Reads the cross-reference stream whose object header is `header` (ISO
/// 32000-2 section 7.5.8).
///
/// The stream's dictionary is its trailer, and must have `/Type /XRef`,
/// `/Size`, and `/W` with three widths of at most 8 bytes, as direct
/// values. hayro-syntax reads the stream from its bytes alone, so it cannot
/// resolve a `/Length` that is a reference: the data then runs to the first
/// `endstream`, without the white space before it, as for a direct length
/// that `endstream` does not follow.
fn stream<'b>(
    bytes: &Bytes,
    origin: usize,
    header: lex::Header,
    budget: &'b Budget,
) -> Result<Read<'b>> {
    let stream = super::stream(&bytes[header.body..])
        .ok_or_else(|| Error::malformed("the object is not a stream that hayro-syntax reads"))?;
    let span = bytes
        .share(stream.dict().data())
        .ok_or_else(|| Error::malformed("the stream's dictionary is not in its bytes"))?;
    let trailer = match Value::parse(&span, budget)? {
        Value::Dict(trailer) => trailer,
        _ => {
            return Err(Error::malformed(
                "the stream's dictionary does not read as one",
            ));
        }
    };
    if trailer.get("Type") != Some(&Value::Name(Name::from("XRef"))) {
        return Err(Error::malformed("the stream's /Type is not /XRef"));
    }
    let widths = widths(&trailer)?;
    let size = match trailer.get("Size") {
        Some(&Value::Integer(size)) => u64::try_from(size).ok(),
        _ => None,
    }
    .ok_or_else(|| Error::malformed("the stream's /Size is not a number of objects"))?;
    let subsections = subsections(&trailer, size, budget)?;
    let data = decoded(&stream, budget)?;
    let row = widths.iter().sum::<usize>();
    let mut listings = BudgetVec::new(budget);
    let mut ticker = Ticker::default();
    let mut rows = data.chunks_exact(row);
    for &(first, count) in subsections.iter() {
        for index in 0..count {
            ticker.tick(budget)?;
            let number = number(first, index)?;
            let unreadable = || {
                Error::malformed(format!(
                    "the stream's entry of object {number} cannot be read"
                ))
            };
            let fields = rows
                .next()
                .ok_or_else(|| Error::malformed("the stream's data ends before its entries do"))?;
            let (kind, rest) = fields.split_at(widths[0]);
            let (second, third) = rest.split_at(widths[1]);
            let kind = if kind.is_empty() { 1 } else { big_endian(kind) };
            let (second, third) = (big_endian(second), big_endian(third));
            let listing = match kind {
                0 => Listing::Free {
                    generation: u16::try_from(third).unwrap_or(u16::MAX),
                },
                1 => Listing::InUse(Entry {
                    generation: u16::try_from(third).map_err(|_| unreadable())?,
                    location: Location::Offset(locate(origin, second)?),
                }),
                2 => Listing::InUse(Entry {
                    generation: 0,
                    location: Location::Member {
                        stream: u32::try_from(second).map_err(|_| unreadable())?,
                        index: u32::try_from(third).map_err(|_| unreadable())?,
                    },
                }),
                _ => Listing::Free { generation: 0 },
            };
            listings.try_push((number, listing))?;
        }
    }
    Ok(Read {
        kind: SectionKind::Stream,
        trailer,
        listings,
        end: stream_end(bytes, &stream),
    })
}

/// Returns the widths of the three fields of each entry that `/W` of
/// `trailer` gives.
fn widths(trailer: &Dict) -> Result<[usize; 3]> {
    let invalid = || Error::malformed("the stream's /W is not three widths of at most 8 bytes");
    let Some(Value::Array(widths)) = trailer.get("W") else {
        return Err(invalid());
    };
    let mut fields = [0; 3];
    if widths.len() != fields.len() {
        return Err(invalid());
    }
    for (field, width) in fields.iter_mut().zip(widths) {
        *field = match width {
            Value::Integer(width @ 0..=8) => usize::try_from(*width).map_err(|_| invalid())?,
            _ => return Err(invalid()),
        };
    }
    if fields == [0; 3] {
        return Err(Error::malformed(
            "the stream's /W gives its entries no bytes",
        ));
    }
    Ok(fields)
}

/// Returns the subsections of a cross-reference stream: the first object
/// number and the number of entries of each, from `/Index` of `trailer`,
/// or the one subsection of `size` entries from object 0.
fn subsections<'b>(
    trailer: &Dict,
    size: u64,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, (u64, u64)>> {
    let mut subsections = BudgetVec::new(budget);
    let Some(index) = trailer.get("Index") else {
        subsections.try_push((0, size))?;
        return Ok(subsections);
    };
    let invalid =
        || Error::malformed("the stream's /Index is not pairs of object numbers and counts");
    let Value::Array(index) = index else {
        return Err(invalid());
    };
    let (pairs, []) = index.as_slice().as_chunks::<2>() else {
        return Err(invalid());
    };
    for pair in pairs {
        let [Value::Integer(first), Value::Integer(count)] = pair else {
            return Err(invalid());
        };
        let (Ok(first), Ok(count)) = (u64::try_from(*first), u64::try_from(*count)) else {
            return Err(invalid());
        };
        subsections.try_push((first, count))?;
    }
    Ok(subsections)
}

/// Returns where the stream object of `stream`, which hayro-syntax parsed
/// from `bytes`, ends: after `endstream`, and after `endobj` if it follows.
fn stream_end(bytes: &Bytes, stream: &syntax::Stream<'_>) -> usize {
    let raw = stream.raw_data();
    let Some(start) = bytes.offset_of(&raw) else {
        return bytes.len();
    };
    let mut end = lex::skip_white(bytes, start + raw.len());
    if let Some(after) = lex::keyword(bytes, end, b"endstream") {
        end = after;
        if let Some(after) = lex::keyword(bytes, lex::skip_space(bytes, end), b"endobj") {
            end = after;
        }
    }
    end
}

/// Returns the position that the offset of `key` in `trailer` gives, if the
/// trailer has the key: a non-negative integer, as ISO 32000-2 Tables 15
/// and 19 require of `/Prev` and `/XRefStm`.
fn link(trailer: &Dict, key: &str, origin: usize) -> Result<Option<usize>> {
    match trailer.get(key) {
        None => Ok(None),
        Some(&Value::Integer(offset)) => u64::try_from(offset)
            .map_err(|_| Error::malformed(format!("its /{key} is negative")))
            .and_then(|offset| locate(origin, offset))
            .map(Some),
        Some(_) => Err(Error::malformed(format!("its /{key} is not an offset"))),
    }
}

/// Returns the position of the bytes that `offset`, counted from `origin`,
/// gives.
fn locate(origin: usize, offset: u64) -> Result<usize> {
    usize::try_from(offset)
        .ok()
        .and_then(|offset| origin.checked_add(offset))
        .ok_or_else(|| Error::malformed(format!("offset {offset} is beyond the address space")))
}

/// Returns object number `first + index` of a subsection.
fn number(first: u64, index: u64) -> Result<u32> {
    first
        .checked_add(index)
        .and_then(|number| u32::try_from(number).ok())
        .ok_or_else(|| Error::malformed("a subsection lists an object number beyond u32"))
}

/// Returns the unsigned integer that `bytes` hold, most significant byte
/// first.
fn big_endian(bytes: &[u8]) -> u64 {
    bytes
        .iter()
        .fold(0, |value, &byte| (value << 8) | u64::from(byte))
}

/// Returns the position of the first object header, or `xref` or `trailer`
/// keyword, of `bytes` from `pos` on.
fn structure_after(bytes: &[u8], pos: usize, budget: &Budget) -> Result<Option<usize>> {
    let mut ticker = Ticker::default();
    for token in Tokens::new(bytes, pos) {
        ticker.tick(budget)?;
        if lex::header(bytes, token).is_some()
            || lex::keyword(bytes, token, b"xref").is_some()
            || lex::keyword(bytes, token, b"trailer").is_some()
        {
            return Ok(Some(token));
        }
    }
    Ok(None)
}

/// Returns what a section lists for each object number, ascending, from
/// what its table or stream lists, and what the stream of a hybrid section
/// lists.
///
/// See [`Section::listings`] for which listing of a number the section
/// keeps.
fn combine(
    listings: BudgetVec<'_, (u32, Listing)>,
    stream: Option<BudgetVec<'_, (u32, Listing)>>,
    budget: &Budget,
) -> Result<Vec<(u32, Listing)>> {
    let hybrid = stream.is_some();
    let mut ranked = BudgetVec::new(budget);
    ranked.try_reserve_exact(listings.len() + stream.as_ref().map_or(0, |stream| stream.len()))?;
    for (position, &(number, listing)) in listings.iter().enumerate() {
        let class = match listing {
            Listing::Free { .. } if hybrid => 0,
            _ => 2,
        };
        ranked.try_push((number, class, position, listing))?;
    }
    for (position, &(number, listing)) in stream.iter().flat_map(|stream| stream.iter()).enumerate()
    {
        ranked.try_push((number, 1, position, listing))?;
    }
    drop((listings, stream));
    ranked.sort_unstable_by_key(|&(number, class, position, _)| {
        (number, Reverse(class), Reverse(position))
    });
    let mut kept = BudgetVec::new(budget);
    for &(number, _, _, listing) in ranked.iter() {
        if kept.last().is_none_or(|&(last, _)| last != number) {
            kept.try_push((number, listing))?;
        }
    }
    Ok(kept.into_vec())
}

/// Returns the in-use entries that `sections`, newest first, give together,
/// ascending by object number: for each number, what the newest section
/// that lists it lists.
fn merge(sections: &[Section], budget: &Budget) -> Result<Vec<(u32, Entry)>> {
    let mut all = BudgetVec::new(budget);
    all.try_reserve_exact(sections.iter().map(|section| section.listings.len()).sum())?;
    for (age, section) in sections.iter().enumerate() {
        for &(number, listing) in &section.listings {
            all.try_push((number, age, listing))?;
        }
    }
    all.sort_unstable_by_key(|&(number, age, _)| (number, age));
    let mut entries = BudgetVec::counting(budget, LimitCode::ObjectCount);
    let mut last = None;
    for &(number, _, listing) in all.iter() {
        if last == Some(number) {
            continue;
        }
        last = Some(number);
        if let Listing::InUse(entry) = listing {
            entries.try_push((number, entry))?;
        }
    }
    Ok(entries.into_vec())
}

/// Groups `sections`, newest first, into revisions, oldest first.
///
/// A revision ends with a `startxref` line of `lines`: the file's last, or
/// one after a section. The line names the revision's first section, and
/// its `%%EOF` marker ends the revision's bytes. A section that no line
/// names belongs to the revision of the section before it in the chain, as
/// a linearized file's main section belongs with its first-page section,
/// whose trailer's `startxref` gives 0 (ISO 32000-2 Annex F).
fn revisions(
    sections: BudgetVec<'_, Section>,
    lines: &[(usize, Option<usize>)],
    budget: &Budget,
) -> Result<Vec<Revision>> {
    let mut revisions = BudgetVec::new(budget);
    let mut current: Option<(BudgetVec<'_, Section>, Option<usize>)> = None;
    for (age, section) in sections.into_vec().into_iter().enumerate() {
        let mut named = lines
            .iter()
            .filter(|&&(pos, _)| pos == section.offset)
            .peekable();
        if age == 0 || named.peek().is_some() {
            if let Some((sections, end)) = current.take() {
                revisions.try_push(Revision {
                    sections: sections.into_vec(),
                    end,
                })?;
            }
            let end = named.filter_map(|&(_, end)| end).max();
            current = Some((BudgetVec::new(budget), end));
        }
        if let Some((sections, _)) = &mut current {
            sections.try_push(section)?;
        }
    }
    if let Some((sections, end)) = current {
        revisions.try_push(Revision {
            sections: sections.into_vec(),
            end,
        })?;
    }
    let mut revisions = revisions.into_vec();
    revisions.reverse();
    Ok(revisions)
}
