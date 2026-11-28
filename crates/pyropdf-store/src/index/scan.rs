//! Rebuilding the index by scanning the bytes for object headers (DESIGN.md
//! section 4.3).
//!
//! The rules that decide the rebuilt index:
//!
//! - Every object header that begins a token is an object, except one in
//!   the data of a stream whose direct `/Length` `endstream` follows: the
//!   scan skips such data, so that a file embedded in a stream lends it no
//!   objects. Other data is scanned, as hayro-syntax scans it, so that a
//!   stream whose length is wrong hides no object after it.
//! - hayro-syntax parses each object within the bytes before the next
//!   header, so the scan takes time linear in the size of the input. A
//!   dictionary followed by `stream` is valid only when its stream parses
//!   within those bytes, so a truncated stream at the end of a cut file is
//!   not treated as a live object.
//! - Of the headers of one object number, the last whose value parses
//!   wins, since an update appends its objects; an object number whose
//!   headers never parse is omitted from the rebuilt entries (while its
//!   header position still bounds the preceding object).
//! - Each object stream that wins its number decodes leniently, unless a
//!   trailer candidate names a security handler and no handler is supplied,
//!   and adds its members. The object later in the bytes wins a number, a
//!   member counting where its stream begins, and within one stream the
//!   later index wins. hayro-syntax 0.8 prefers any uncompressed object to
//!   a member, which keeps the old copy of an object that an update moved
//!   into an object stream.
//! - The trailer is the last candidate in the order of the bytes whose
//!   `/Root` names a dictionary with a live `/Pages` target: a dictionary
//!   after a `trailer` keyword, or the dictionary of a cross-reference
//!   stream. Without one, the scan picks the last `/Type /Catalog` (or an
//!   untyped dictionary with `/Pages` pointing to a live `/Type /Pages`),
//!   preserving `/Encrypt`, `/ID`, and `/Info` from trailer candidates.
//!   When no catalog reaches a live page tree and intact `/Type /Pages` or
//!   `/Type /Page` dictionaries exist (and the file is not locked), the
//!   scan synthesizes the missing Catalog (and `/Pages` root when needed)
//!   into appended bytes charged to the budget. Otherwise it falls back to
//!   any catalog or trailer candidate, or an empty trailer.

use std::fmt::Write as _;
use std::iter;
use std::ops::Range;

use hayro_syntax::object::{self as syntax, FromBytes, MaybeRef};

use super::lex::{self, Header, StringState, Tokens};
use super::objstm::{self, Decoded};
use super::{Entry, Location, POLL_INTERVAL, Source, Ticker, count, find, guard, sort_unique};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::security::Handler;
use crate::value::{Bytes, Dict, Name, Reference, Value};
use crate::{Error, Result};

/// The index that a scan rebuilt.
pub(super) struct Rebuilt {
    /// Replacement backing bytes when synthetic Catalog/Pages objects were
    /// appended; `None` when the original bytes are reused unchanged.
    pub(super) bytes: Option<Bytes>,
    /// The in-use entries, ascending by object number.
    pub(super) entries: Vec<(u32, Entry)>,
    /// Where the objects that the scan found begin, ascending.
    pub(super) bounds: Vec<usize>,
    pub(super) trailer: Dict,
    /// Where the trailer's dictionary begins, or `None` when the scan made
    /// or modified the trailer.
    pub(super) trailer_at: Option<usize>,
    /// The object streams that the scan decoded, with their object numbers.
    pub(super) streams: Vec<(u32, Decoded)>,
}

/// An object that the scan found.
#[derive(Clone, Copy, Debug)]
struct Found {
    number: u32,
    generation: u16,
    /// Where its header begins.
    pos: usize,
    /// Whether hayro-syntax parses a complete value after the header, within
    /// the bytes before the next header.
    valid: bool,
    /// Whether the object is a stream rather than a bare dictionary.
    is_stream: bool,
    kind: Kind,
    /// Whether its dictionary has a `/Type` key.
    has_type: bool,
    /// Where its dictionary begins and ends, if its value is a dictionary
    /// or a stream.
    dict: Option<(usize, usize)>,
    /// Whether its dictionary has `/Pages`, as a catalog's must.
    pages: bool,
    /// Whether its dictionary has `/MediaBox`.
    has_media_box: bool,
}

/// What an object's dictionary says that it is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Other,
    Catalog,
    Pages,
    Page,
    ObjectStream,
    XRefStream,
}

/// Rebuilds the index of `bytes` by scanning them for object headers.
/// An encrypted document's object streams decode only with `security`.
///
/// Refuses with a malformed input error bytes that hold no parseable object,
/// and with the budget's limits.
pub(super) fn rebuild(
    bytes: &Bytes,
    security: Option<&Handler>,
    budget: &Budget,
) -> Result<Rebuilt> {
    let Marks {
        headers,
        keywords,
        endstreams,
        barriers,
    } = marks(bytes, budget)?;
    if headers.is_empty() {
        return Err(Error::malformed(
            "no cross-reference data reads, and the bytes hold no object header",
        ));
    }
    let (found, skipped) = objects(bytes, &headers, &endstreams, &barriers, budget)?;
    drop((headers, endstreams, barriers));
    let candidates = candidates(bytes, &found, &keywords, &skipped, budget)?;
    drop((keywords, skipped));
    let mut bounds = BudgetVec::new(budget);
    bounds.try_reserve_exact(found.len())?;
    bounds.try_extend(found.iter().map(|object| object.pos))?;
    sort_unique(&mut bounds);
    let winners = winners(found, budget)?;
    if winners.is_empty() {
        return Err(Error::malformed(
            "no cross-reference data reads, and the bytes hold no valid object",
        ));
    }
    let mut uncompressed = BudgetVec::new(budget);
    uncompressed.try_reserve_exact(winners.len())?;
    uncompressed.try_extend(winners.iter().map(|object| {
        let location = Location::Offset(object.pos);
        let entry = Entry {
            generation: object.generation,
            location,
        };
        (object.number, entry)
    }))?;
    let source = Source {
        bytes,
        entries: &uncompressed,
        bounds: &bounds,
    };
    let encrypted = candidates
        .iter()
        .any(|(_, trailer)| trailer.get("Encrypt").is_some());
    // Without a key, an encrypted document's object streams are ciphertext.
    let locked = encrypted && security.is_none();
    let mut streams = if locked {
        BudgetVec::new(budget)
    } else {
        decode_streams(&source, &winners, security, budget)?
    };
    let mut entries = entries(&uncompressed, &streams, budget)?;
    let mut by_number = BudgetVec::new(budget);
    by_number.try_reserve_exact(streams.len())?;
    by_number.try_extend(
        streams
            .iter()
            .enumerate()
            .map(|(index, &(_, number, _))| (number, index)),
    )?;
    by_number.sort_unstable();
    let choice = Choice {
        bytes,
        entries: &entries,
        winners: &winners,
        streams: &streams,
        by_number: &by_number,
        locked,
        budget,
    };
    let (trailer, trailer_at, synthetic) = choice.trailer(candidates)?;
    drop(by_number);
    let mut out_bytes = None;
    if let Some(synth) = synthetic {
        let appended = append_synthetic(bytes, &synth, budget)?;
        // Merge the new (strictly larger) object numbers and offsets in place
        // so `ObjectCount` charges only the appended objects and no replaced
        // `BudgetVec` buffer leaks its `MemorySize` charge.
        entries.try_extend_from_slice(&appended.entries)?;
        bounds.try_extend_from_slice(&appended.bounds)?;
        sort_unique(&mut bounds);
        out_bytes = Some(appended.bytes);
    }
    let mut decoded = BudgetVec::new(budget);
    decoded.try_reserve_exact(streams.len())?;
    while let Some((_, number, stream)) = streams.pop() {
        decoded.try_push((number, stream))?;
    }
    decoded.reverse();
    drop(streams);
    Ok(Rebuilt {
        bytes: out_bytes,
        entries: entries.into_vec(),
        bounds: bounds.into_vec(),
        trailer,
        trailer_at,
        streams: decoded.into_vec(),
    })
}

/// The object headers, `trailer` keywords, `endstream` tokens, and
/// `stream`/`endobj` barriers of the bytes, in their order.
struct Marks<'b> {
    /// Each header, and where it begins.
    headers: BudgetVec<'b, (usize, Header)>,
    /// Where the value after each `trailer` keyword begins.
    keywords: BudgetVec<'b, usize>,
    /// Each `endstream` keyword token: `(start, end)`.
    endstreams: BudgetVec<'b, (usize, usize)>,
    /// Start positions of `stream` and `endobj` keyword tokens, ascending.
    barriers: BudgetVec<'b, usize>,
}

/// Finds the object headers, `trailer`, `endstream`, and `stream`/`endobj`
/// keywords of `bytes`.
fn marks<'b>(bytes: &[u8], budget: &'b Budget) -> Result<Marks<'b>> {
    let mut marks = Marks {
        headers: BudgetVec::new(budget),
        keywords: BudgetVec::new(budget),
        endstreams: BudgetVec::new(budget),
        barriers: BudgetVec::new(budget),
    };
    let mut ticker = Ticker::default();
    let mut tokens = Tokens::new(bytes, 0);
    while let Some(pos) = tokens.next() {
        ticker.tick(budget)?;
        if let Some(header) = lex::header(bytes, pos) {
            marks.headers.try_push((pos, header))?;
            tokens.skip_to(header.end);
        } else if let Some(end) = lex::keyword(bytes, pos, b"trailer") {
            marks.keywords.try_push(lex::skip_space(bytes, end))?;
        } else if let Some(end) = lex::keyword(bytes, pos, b"endstream") {
            marks.endstreams.try_push((pos, end))?;
        } else if lex::keyword(bytes, pos, b"stream").is_some()
            || lex::keyword(bytes, pos, b"endobj").is_some()
        {
            marks.barriers.try_push(pos)?;
        }
    }
    Ok(marks)
}

/// Reads the object after each of `headers` within the bytes before the
/// next header, dropping false-positive headers inside literal strings and
/// inside streams whose direct or indirect `/Length` `endstream` follows.
///
/// Returns the objects in the order of the bytes, and the ranges of the
/// data that the scan skipped, ascending.
fn objects<'b>(
    bytes: &[u8],
    headers: &[(usize, Header)],
    endstreams: &[(usize, usize)],
    barriers: &[usize],
    budget: &'b Budget,
) -> Result<(BudgetVec<'b, Found>, BudgetVec<'b, Range<usize>>)> {
    let ints = int_objects(bytes, headers, budget)?;
    let mut found = BudgetVec::new(budget);
    let mut skipped = BudgetVec::new(budget);
    let mut ticker = Ticker::default();
    let mut failed_scan_budget = bytes.len();
    let mut next = 0;
    while let Some(&(pos, header)) = headers.get(next) {
        ticker.tick(budget)?;
        next += 1;
        let end = headers.get(next).map_or(bytes.len(), |&(next, _)| next);
        let mut value = &bytes[header.body..end.max(header.body)];
        let mut object = Found {
            number: header.number,
            generation: header.generation,
            pos,
            valid: false,
            is_stream: false,
            kind: Kind::Other,
            has_type: false,
            dict: None,
            pages: false,
            has_media_box: false,
        };
        if value.starts_with(b"<<") {
            let mut recovered_from_string = false;
            let dict = match guard(|| syntax::Dict::from_bytes(value)) {
                Some(dict) => Some(dict),
                None if next < headers.len() && failed_scan_budget > 0 => {
                    let (dict, ext_next, ext_end) = recover_dict_past_strings(
                        bytes,
                        headers,
                        header.body,
                        next,
                        &mut failed_scan_budget,
                        &mut ticker,
                        budget,
                    )?;
                    if dict.is_some() {
                        next = ext_next;
                        value = &bytes[header.body..ext_end.max(header.body)];
                        recovered_from_string = true;
                    }
                    dict
                }
                None => None,
            };
            if let Some(dict) = dict {
                let dict_end = header.body + dict.data().len();
                let is_stream =
                    lex::keyword(bytes, lex::skip_space(bytes, dict_end), b"stream").is_some();
                object.is_stream = is_stream;
                if is_stream {
                    if let Some(data_end) = stream_end(bytes, dict_end, &dict, &ints) {
                        object.valid = true;
                        let skip_start = if recovered_from_string {
                            header.body
                        } else {
                            dict_end
                        };
                        skipped.try_push(skip_start..data_end)?;
                        while headers.get(next).is_some_and(|&(next, _)| next < data_end) {
                            next += 1;
                        }
                    } else if super::stream(value).is_some() {
                        object.valid = true;
                        if recovered_from_string {
                            skipped.try_push(header.body..dict_end)?;
                        }
                    } else if matches!(dict.get_raw::<i64>(b"Length"), Some(MaybeRef::Ref(_)))
                        && let Some(data_end) =
                            indirect_stream_fallback(bytes, dict_end, endstreams, barriers)
                    {
                        object.valid = true;
                        let skip_start = if recovered_from_string {
                            header.body
                        } else {
                            dict_end
                        };
                        skipped.try_push(skip_start..data_end)?;
                        while headers.get(next).is_some_and(|&(next, _)| next < data_end) {
                            next += 1;
                        }
                    }
                } else {
                    object.valid = true;
                    if recovered_from_string {
                        skipped.try_push(header.body..dict_end)?;
                    }
                }
                if object.valid {
                    object.dict = Some((header.body, dict_end));
                    object.kind = kind(&dict);
                    object.has_type = dict.contains_key(b"Type");
                    object.pages = dict.contains_key(b"Pages");
                    object.has_media_box = dict.contains_key(b"MediaBox");
                }
            }
        } else if guard(|| MaybeRef::<syntax::Object<'_>>::from_bytes(value)).is_some() {
            object.valid = true;
        } else if next < headers.len()
            && failed_scan_budget > 0
            && let Some((ext_next, close_pos)) = recover_value_past_strings(
                bytes,
                headers,
                header.body,
                next,
                &mut failed_scan_budget,
                &mut ticker,
                budget,
            )?
        {
            object.valid = true;
            next = ext_next;
            skipped.try_push(header.body..close_pos)?;
        }
        found.try_push(object)?;
    }
    Ok((found, skipped))
}

/// Builds a lookup table of the last valid non-negative integer object per
/// `(number, generation)` among `headers`, so indirect stream `/Length`
/// references resolve in `O(log H)` time.
fn int_objects<'b>(
    bytes: &[u8],
    headers: &[(usize, Header)],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, (u32, u16, usize, u64)>> {
    let mut ints = BudgetVec::new(budget);
    let mut ticker = Ticker::default();
    for (i, &(pos, header)) in headers.iter().enumerate() {
        ticker.tick(budget)?;
        let end = headers.get(i + 1).map_or(bytes.len(), |&(next, _)| next);
        if header.body >= end {
            continue;
        }
        if let Some((val, val_end)) = lex::unsigned(bytes, header.body)
            && val_end <= end
        {
            let after = lex::skip_space(bytes, val_end);
            if after >= end || lex::keyword(bytes, after, b"endobj").is_some() {
                ints.try_push((header.number, header.generation, pos, val))?;
            }
        }
    }
    ints.sort_unstable_by_key(|&(number, generation, pos, _)| (number, generation, pos));
    let mut kept = 0;
    for index in 0..ints.len() {
        if ints
            .get(index + 1)
            .is_none_or(|&(n, g, ..)| n != ints[index].0 || g != ints[index].1)
        {
            ints[kept] = ints[index];
            kept += 1;
        }
    }
    ints.truncate(kept);
    Ok(ints)
}

/// Extends past false-positive `N G obj` matches inside balanced literal
/// strings `(...)` when parsing a dictionary object starting at `body` failed
/// before `headers[next]`. Keeps total failed lookahead strictly `O(bytes.len())`
/// and refuses to swallow following objects across an unclosed `(` followed by
/// `endobj`.
fn recover_dict_past_strings<'a>(
    bytes: &'a [u8],
    headers: &[(usize, Header)],
    body: usize,
    next: usize,
    failed_scan_budget: &mut usize,
    ticker: &mut Ticker,
    budget: &Budget,
) -> Result<(Option<syntax::Dict<'a>>, usize, usize)> {
    let mut state = StringState::default();
    let mut cur_next = next;
    let mut scan_pos = body;
    let mut scan_end = headers.get(cur_next).map_or(bytes.len(), |&(p, _)| p);
    let mut extra_scanned = 0usize;
    loop {
        state.feed(bytes, scan_pos, scan_end.max(scan_pos), false);
        if state.depth == 0 || state.saw_endobj_in_string || cur_next >= headers.len() {
            *failed_scan_budget = failed_scan_budget.saturating_sub(extra_scanned);
            return Ok((None, next, scan_end));
        }
        let Some((close_pos, probe_next)) = advance_to_string_close(
            bytes,
            headers,
            scan_end,
            cur_next,
            &mut state,
            *failed_scan_budget,
            &mut extra_scanned,
            ticker,
            budget,
        )?
        else {
            *failed_scan_budget = failed_scan_budget.saturating_sub(extra_scanned);
            return Ok((None, next, scan_end));
        };
        cur_next = probe_next;
        scan_pos = close_pos;
        scan_end = headers.get(cur_next).map_or(bytes.len(), |&(p, _)| p);
        let extended = &bytes[body..scan_end.max(body)];
        if let Some(dict) = guard(|| syntax::Dict::from_bytes(extended)) {
            return Ok((Some(dict), cur_next, scan_end));
        }
    }
}

/// Extends past false-positive `N G obj` matches inside balanced literal
/// strings `(...)` when parsing a non-dictionary object starting at `body`
/// failed before `headers[next]`.
fn recover_value_past_strings(
    bytes: &[u8],
    headers: &[(usize, Header)],
    body: usize,
    next: usize,
    failed_scan_budget: &mut usize,
    ticker: &mut Ticker,
    budget: &Budget,
) -> Result<Option<(usize, usize)>> {
    let mut state = StringState::default();
    let mut cur_next = next;
    let mut scan_pos = body;
    let mut scan_end = headers.get(cur_next).map_or(bytes.len(), |&(p, _)| p);
    let mut extra_scanned = 0usize;
    loop {
        state.feed(bytes, scan_pos, scan_end.max(scan_pos), false);
        if state.depth == 0 || state.saw_endobj_in_string || cur_next >= headers.len() {
            *failed_scan_budget = failed_scan_budget.saturating_sub(extra_scanned);
            return Ok(None);
        }
        let Some((close_pos, probe_next)) = advance_to_string_close(
            bytes,
            headers,
            scan_end,
            cur_next,
            &mut state,
            *failed_scan_budget,
            &mut extra_scanned,
            ticker,
            budget,
        )?
        else {
            *failed_scan_budget = failed_scan_budget.saturating_sub(extra_scanned);
            return Ok(None);
        };
        cur_next = probe_next;
        scan_pos = close_pos;
        scan_end = headers.get(cur_next).map_or(bytes.len(), |&(p, _)| p);
        let extended = &bytes[body..scan_end.max(body)];
        if guard(|| MaybeRef::<syntax::Object<'_>>::from_bytes(extended)).is_some() {
            return Ok(Some((cur_next, close_pos)));
        }
    }
}

/// Scans forward from `start` (at `headers[cur_next]`) while `state.depth > 0`
/// until the open literal string closes, polling `budget`, stopping if an
/// `endobj` precedes another `N G obj`, and respecting `max_extra`.
#[expect(
    clippy::too_many_arguments,
    reason = "internal linear-time scanner threading bounded recovery state"
)]
fn advance_to_string_close(
    bytes: &[u8],
    headers: &[(usize, Header)],
    start: usize,
    cur_next: usize,
    state: &mut StringState,
    max_extra: usize,
    extra_scanned: &mut usize,
    ticker: &mut Ticker,
    budget: &Budget,
) -> Result<Option<(usize, usize)>> {
    let mut cursor = start;
    let mut probe_next = cur_next + 1;
    while cursor < bytes.len() {
        ticker.tick(budget)?;
        let next_hdr = headers.get(probe_next).map_or(bytes.len(), |&(p, _)| p);
        let remaining = max_extra.saturating_sub(*extra_scanned);
        if remaining == 0 {
            return Ok(None);
        }
        let step_end = next_hdr
            .min(cursor.saturating_add(POLL_INTERVAL as usize))
            .min(cursor.saturating_add(remaining));
        let reached = state.feed(bytes, cursor, step_end, true);
        *extra_scanned = extra_scanned.saturating_add(reached - cursor);
        cursor = reached;
        if state.depth == 0 {
            return Ok(Some((cursor, probe_next)));
        }
        if cursor == next_hdr {
            if state.saw_endobj_in_string {
                return Ok(None);
            }
            probe_next += 1;
        }
    }
    Ok(None)
}

/// Returns what `dict` says that its object is, by its `/Type`.
fn kind(dict: &syntax::Dict<'_>) -> Kind {
    match dict.get::<syntax::Name<'_>>(b"Type").as_deref() {
        Some(b"Catalog") => Kind::Catalog,
        Some(b"Pages") => Kind::Pages,
        Some(b"Page") => Kind::Page,
        Some(b"ObjStm") => Kind::ObjectStream,
        Some(b"XRef") => Kind::XRefStream,
        _ => Kind::Other,
    }
}

/// Returns where the stream whose dictionary `dict` ends at `dict_end`
/// ends, after `endstream`, when the dictionary's direct or indirect `/Length`
/// is followed by `endstream`.
fn stream_end(
    bytes: &[u8],
    dict_end: usize,
    dict: &syntax::Dict<'_>,
    ints: &[(u32, u16, usize, u64)],
) -> Option<usize> {
    let length = match dict.get_raw::<i64>(b"Length")? {
        MaybeRef::NotRef(length) => u64::try_from(length).ok()?,
        MaybeRef::Ref(reference) => {
            let generation = u16::try_from(reference.gen_number).ok()?;
            let slot = ints
                .binary_search_by_key(&(reference.obj_number, generation), |&(n, g, ..)| (n, g))
                .ok()?;
            ints[slot].3
        }
    };
    stream_end_with_length(bytes, dict_end, length)
}

fn stream_end_with_length(bytes: &[u8], dict_end: usize, length: u64) -> Option<usize> {
    let keyword = lex::keyword(bytes, lex::skip_space(bytes, dict_end), b"stream")?;
    let start = lex::skip_eol(bytes, keyword);
    if start == keyword {
        return None;
    }
    let end = start.checked_add(usize::try_from(length).ok()?)?;
    if end > bytes.len() {
        return None;
    }
    lex::keyword(bytes, lex::skip_white(bytes, end), b"endstream")
}

/// Fallback for an indirect-`/Length` stream whose length object could not be
/// resolved (for example because it is missing or compressed in an `ObjStm`):
/// accepts the first `endstream` after `stream` only when `endobj` immediately
/// follows it and no intervening `stream` or `endobj` lies in between.
fn indirect_stream_fallback(
    bytes: &[u8],
    dict_end: usize,
    endstreams: &[(usize, usize)],
    barriers: &[usize],
) -> Option<usize> {
    let keyword = lex::keyword(bytes, lex::skip_space(bytes, dict_end), b"stream")?;
    let start = lex::skip_eol(bytes, keyword);
    if start == keyword {
        return None;
    }
    let slot = endstreams.partition_point(|&(es_start, _)| es_start < start);
    let &(es_start, es_end) = endstreams.get(slot)?;
    lex::keyword(bytes, lex::skip_space(bytes, es_end), b"endobj")?;
    let first_barrier = barriers.partition_point(|&pos| pos < start);
    if barriers
        .get(first_barrier)
        .is_some_and(|&pos| pos < es_start)
    {
        return None;
    }
    Some(es_end)
}

/// Returns the candidates for the trailer, in the order of the bytes: where
/// each dictionary begins, and the dictionary.
///
/// A candidate is the dictionary after a `trailer` keyword that is not in
/// skipped data, read within the bytes before the next object, or the
/// dictionary of a cross-reference stream.
fn candidates<'b>(
    bytes: &Bytes,
    found: &[Found],
    keywords: &[usize],
    skipped: &[Range<usize>],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, (usize, Dict)>> {
    let mut candidates = BudgetVec::new(budget);
    for &start in keywords {
        let next = skipped.partition_point(|range| range.start <= start);
        if next > 0 && skipped[next - 1].contains(&start) {
            continue;
        }
        let next = found.partition_point(|object| object.pos <= start);
        let end = found.get(next).map_or(bytes.len(), |object| object.pos);
        if let Some(dict) = dictionary(bytes, start..end, budget)? {
            candidates.try_push((start, dict))?;
        }
    }
    for object in found
        .iter()
        .filter(|object| object.valid && object.kind == Kind::XRefStream)
    {
        if let Some((start, end)) = object.dict
            && let Some(dict) = dictionary(bytes, start..end, budget)?
        {
            candidates.try_push((start, dict))?;
        }
    }
    candidates.sort_unstable_by_key(|&(start, _)| start);
    Ok(candidates)
}

/// Returns the dictionary that the bytes of `range` begin with, if they
/// begin with one.
fn dictionary(bytes: &Bytes, range: Range<usize>, budget: &Budget) -> Result<Option<Dict>> {
    let Some(source) = bytes.slice(range) else {
        return Ok(None);
    };
    match Value::parse(&source, budget) {
        Ok(Value::Dict(dict)) => Ok(Some(dict)),
        Err(error) if error.limit().is_some() => Err(error),
        _ => Ok(None),
    }
}

/// Returns, ascending by object number, the last parseable object for each
/// number among `found`. Object numbers whose headers never parse are
/// omitted.
fn winners<'b>(
    mut found: BudgetVec<'b, Found>,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, Found>> {
    found.sort_unstable_by_key(|object| (object.number, object.valid, object.pos));
    let mut winners = BudgetVec::new(budget);
    for (index, object) in found.iter().enumerate() {
        if object.valid
            && found
                .get(index + 1)
                .is_none_or(|next| next.number != object.number)
        {
            winners.try_push(*object)?;
        }
    }
    Ok(winners)
}

/// Decodes the object streams of generation 0 among `winners`, in the order
/// of the bytes: where each begins, its object number, and what it decodes
/// to. A stream that does not decode adds no members.
fn decode_streams<'b>(
    source: &Source<'_>,
    winners: &[Found],
    security: Option<&Handler>,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, (usize, u32, Decoded)>> {
    let mut order = BudgetVec::new(budget);
    for object in winners {
        if object.kind == Kind::ObjectStream && object.generation == 0 {
            order.try_push((object.pos, object.number))?;
        }
    }
    order.sort_unstable();
    let mut streams = BudgetVec::new(budget);
    for &(pos, number) in order.iter() {
        let lookup = |reference, b: &Budget| {
            source
                .uncompressed(reference)
                .map(|bytes| {
                    b.check(LimitCode::ObjectParseSize, bytes.len() as u64)?;
                    crate::value::Value::parse(&bytes, b)
                })
                .transpose()
        };
        match objstm::decode_repair(source, number, security, &lookup, budget) {
            Ok(decoded) => streams.try_push((pos, number, decoded))?,
            Err(error) if error.limit().is_some() => return Err(error),
            Err(_) => {}
        }
    }
    Ok(streams)
}

/// Returns the entries of the rebuilt index, ascending by object number:
/// for each number, the uncompressed object of `uncompressed`, or the member
/// of `streams` when its stream begins later in the bytes.
fn entries<'b>(
    uncompressed: &[(u32, Entry)],
    streams: &[(usize, u32, Decoded)],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, (u32, Entry)>> {
    let mut members = BudgetVec::new(budget);
    for (pos, stream, decoded) in streams {
        for (index, &(number, _)) in decoded.members().iter().enumerate() {
            let Ok(index) = u32::try_from(index) else {
                break;
            };
            let later = match find(uncompressed, number).map(|entry| entry.location) {
                Some(Location::Offset(header)) => *pos > header,
                _ => true,
            };
            if later {
                members.try_push((number, *pos, index, *stream))?;
            }
        }
    }
    members.sort_unstable();
    let mut compressed = members
        .iter()
        .enumerate()
        .filter(|&(index, &(number, ..))| {
            members
                .get(index + 1)
                .is_none_or(|&(next, ..)| next != number)
        })
        .map(|(_, &(number, _, index, stream))| {
            let location = Location::Member { stream, index };
            let entry = Entry {
                generation: 0,
                location,
            };
            (number, entry)
        })
        .peekable();
    let mut entries = BudgetVec::counting(budget, LimitCode::ObjectCount);
    for &(number, entry) in uncompressed {
        while let Some(&(member, _)) = compressed.peek()
            && member < number
        {
            entries.try_extend(compressed.next())?;
        }
        match compressed.next_if(|&(member, _)| member == number) {
            Some(member) => entries.try_push(member)?,
            None => entries.try_push((number, entry))?,
        }
    }
    entries.try_extend(compressed)?;
    Ok(entries)
}

/// Synthetic catalog / page-tree objects to append when no intact catalog
/// reaches a live page tree.
struct SyntheticPlan<'b> {
    /// Synthetic `/Type /Pages` object (if needed): `(number, kids, total_count, add_mediabox)`.
    pages: Option<(u32, BudgetVec<'b, Reference>, usize, bool)>,
    /// Synthetic `/Type /Catalog` object: `(number, pages_ref)`.
    catalog: (u32, Reference),
}

/// Extended bytes and new entries produced by [`append_synthetic`].
struct Appended<'b> {
    bytes: Bytes,
    entries: BudgetVec<'b, (u32, Entry)>,
    bounds: BudgetVec<'b, usize>,
}

/// Appends the objects described by `plan` to a budgeted copy of `bytes`.
fn append_synthetic<'b>(
    bytes: &Bytes,
    plan: &SyntheticPlan<'b>,
    budget: &'b Budget,
) -> Result<Appended<'b>> {
    let mut tail = BudgetVec::new(budget);
    tail.try_push(b'\n')?;
    let mut added_entries = BudgetVec::new(budget);
    let mut added_bounds = BudgetVec::new(budget);
    let base_len = bytes.len();

    if let Some((pages_num, kids, total_count, add_mediabox)) = &plan.pages {
        let offset = base_len
            .checked_add(tail.len())
            .ok_or_else(|| Error::malformed("synthetic repair object offset overflow"))?;
        let mut buf = BudgetVec::new(budget);
        write_header(&mut buf, *pages_num)?;
        buf.try_extend_from_slice(b"<< /Type /Pages /Kids [")?;
        let mut ticker = Ticker::default();
        for (i, kid) in kids.iter().enumerate() {
            ticker.tick(budget)?;
            if i > 0 {
                buf.try_push(b' ')?;
            }
            write_ref(&mut buf, *kid)?;
        }
        buf.try_extend_from_slice(b"] /Count ")?;
        write_usize(&mut buf, *total_count)?;
        if *add_mediabox {
            buf.try_extend_from_slice(b" /MediaBox [0 0 612 792]")?;
        }
        buf.try_extend_from_slice(b" >>\nendobj\n")?;
        tail.try_extend_from_slice(&buf)?;
        added_entries.try_push((
            *pages_num,
            Entry {
                generation: 0,
                location: Location::Offset(offset),
            },
        ))?;
        added_bounds.try_push(offset)?;
    }

    let (cat_num, pages_ref) = plan.catalog;
    let cat_offset = base_len
        .checked_add(tail.len())
        .ok_or_else(|| Error::malformed("synthetic repair object offset overflow"))?;
    let mut cat_buf = BudgetVec::new(budget);
    write_header(&mut cat_buf, cat_num)?;
    cat_buf.try_extend_from_slice(b"<< /Type /Catalog /Pages ")?;
    write_ref(&mut cat_buf, pages_ref)?;
    cat_buf.try_extend_from_slice(b" >>\nendobj\n")?;
    tail.try_extend_from_slice(&cat_buf)?;
    added_entries.try_push((
        cat_num,
        Entry {
            generation: 0,
            location: Location::Offset(cat_offset),
        },
    ))?;
    added_bounds.try_push(cat_offset)?;

    let total_len = base_len
        .checked_add(tail.len())
        .ok_or_else(|| Error::malformed("repaired file size overflow"))?;
    budget.check(LimitCode::FileSize, count(total_len))?;
    budget.check(
        LimitCode::ObjectCount,
        budget
            .used(LimitCode::ObjectCount)
            .saturating_add(count(added_entries.len())),
    )?;
    let mut combined = BudgetVec::new(budget);
    combined.try_reserve_exact(total_len)?;
    combined.try_extend_from_slice(bytes)?;
    combined.try_extend_from_slice(&tail)?;
    Ok(Appended {
        bytes: Bytes::from(combined.into_vec()),
        entries: added_entries,
        bounds: added_bounds,
    })
}

fn write_header(buf: &mut BudgetVec<'_, u8>, number: u32) -> Result<()> {
    let mut tmp = FixedBuf::new();
    let _ = writeln!(tmp, "{number} 0 obj");
    buf.try_extend_from_slice(tmp.as_bytes())?;
    Ok(())
}

fn write_ref(buf: &mut BudgetVec<'_, u8>, reference: Reference) -> Result<()> {
    let mut tmp = FixedBuf::new();
    let _ = write!(tmp, "{} {} R", reference.number(), reference.generation());
    buf.try_extend_from_slice(tmp.as_bytes())?;
    Ok(())
}

fn write_usize(buf: &mut BudgetVec<'_, u8>, value: usize) -> Result<()> {
    let mut tmp = FixedBuf::new();
    let _ = write!(tmp, "{value}");
    buf.try_extend_from_slice(tmp.as_bytes())?;
    Ok(())
}

/// Tiny stack-allocated formatting buffer for fixed-size numbers and keywords.
struct FixedBuf {
    buf: [u8; 64],
    len: usize,
}

impl FixedBuf {
    const fn new() -> Self {
        Self {
            buf: [0; 64],
            len: 0,
        }
    }

    fn as_bytes(&self) -> &[u8] {
        &self.buf[..self.len]
    }
}

impl std::fmt::Write for FixedBuf {
    fn write_str(&mut self, s: &str) -> std::fmt::Result {
        let bytes = s.as_bytes();
        let end = self.len + bytes.len();
        if end > self.buf.len() {
            return Err(std::fmt::Error);
        }
        self.buf[self.len..end].copy_from_slice(bytes);
        self.len = end;
        Ok(())
    }
}

/// What choosing the trailer of a rebuilt index reads.
struct Choice<'a, 'b> {
    bytes: &'a Bytes,
    entries: &'a [(u32, Entry)],
    /// The objects that won their numbers, ascending by object number.
    winners: &'a [Found],
    /// The decoded object streams, in the order of the bytes.
    streams: &'a [(usize, u32, Decoded)],
    /// The object number of each of `streams` and its index there,
    /// ascending.
    by_number: &'a [(u32, usize)],
    /// Whether the document appears encrypted and has no security handler yet.
    locked: bool,
    budget: &'b Budget,
}

impl<'b> Choice<'_, 'b> {
    /// Returns the trailer among `candidates`, or one that the scan makes,
    /// where its dictionary begins (if taken verbatim), and any synthetic
    /// catalog/page-tree plan needed to recover unreferenced pages.
    fn trailer(
        &self,
        mut candidates: BudgetVec<'_, (usize, Dict)>,
    ) -> Result<(Dict, Option<usize>, Option<SyntheticPlan<'b>>)> {
        // Whether the object of each entry is a catalog with a live `/Pages`
        // dictionary, once checked.
        let mut checked_live = BudgetVec::new(self.budget);
        checked_live.try_reserve_exact(self.entries.len())?;
        checked_live.try_extend(iter::repeat_n(None, self.entries.len()))?;

        // Step A: latest trailer candidate whose `/Root` names a dictionary
        // with a live `/Pages` dictionary.
        for index in (0..candidates.len()).rev() {
            if let Some(&Value::Reference(root)) = candidates[index].1.get("Root")
                && self.has_live_page_tree(root, &mut checked_live)?
            {
                let start = candidates[index].0;
                let mut trailer = std::mem::take(&mut candidates[index].1);
                let modified = preserve_security(&mut candidates, &mut trailer);
                return Ok((trailer, (!modified).then_some(start), None));
            }
        }

        // Step B: latest `/Type /Catalog` (or untyped dict with `/Pages`
        // pointing to a live `/Type /Pages`) whose page tree is live.
        if let Some(root) = self.live_catalog(&mut checked_live)? {
            let trailer = merge_trailer(candidates, root);
            return Ok((trailer, None, None));
        }

        // Step C: synthesize a Catalog (and `/Pages` root when needed) from
        // unreferenced `/Type /Pages` roots or orphan `/Type /Page` objects.
        if !self.locked
            && let Some(plan) = self.synthesize_page_tree()?
        {
            let root = Reference::new(plan.catalog.0, 0);
            let trailer = merge_trailer(candidates, root);
            return Ok((trailer, None, Some(plan)));
        }

        // Step D: fallback when no live pages exist — latest candidate whose
        // `/Root` has `/Pages`, else latest `/Type /Catalog`, else last
        // candidate, else empty trailer.
        let mut checked_any = BudgetVec::new(self.budget);
        checked_any.try_reserve_exact(self.entries.len())?;
        checked_any.try_extend(iter::repeat_n(None, self.entries.len()))?;
        let mut chosen = None;
        for index in (0..candidates.len()).rev() {
            if let Some(&Value::Reference(root)) = candidates[index].1.get("Root")
                && self.is_catalog(root, &mut checked_any)?
            {
                chosen = Some(index);
                break;
            }
        }
        if chosen.is_none()
            && let Some(root) = self.any_catalog()?
        {
            let trailer = merge_trailer(candidates, root);
            return Ok((trailer, None, None));
        }
        let Some(index) = chosen.or_else(|| candidates.len().checked_sub(1)) else {
            return Ok((Dict::new(), None, None));
        };
        let start = candidates[index].0;
        let mut trailer = std::mem::take(&mut candidates[index].1);
        let modified = preserve_security(&mut candidates, &mut trailer);
        Ok((trailer, (!modified).then_some(start), None))
    }

    /// Returns whether `root` names an object whose dictionary has `/Pages`
    /// resolving to a live dictionary in `self.entries`.
    fn has_live_page_tree(&self, root: Reference, checked: &mut [Option<bool>]) -> Result<bool> {
        let Ok(slot) = self
            .entries
            .binary_search_by_key(&root.number(), |&(number, _)| number)
        else {
            return Ok(false);
        };
        let entry = self.entries[slot].1;
        if entry.generation != root.generation() {
            return Ok(false);
        }
        if let Some(known) = checked[slot] {
            return Ok(known);
        }
        let live = match self.dict_at(slot)? {
            Some(dict) => match dict.get("Pages") {
                Some(Value::Dict(_)) => true,
                Some(&Value::Reference(pages_ref)) => self.is_live_dict(pages_ref)?,
                _ => false,
            },
            None => false,
        };
        checked[slot] = Some(live);
        Ok(live)
    }

    /// Returns whether `reference` resolves to a live dictionary in
    /// `self.entries`.
    fn is_live_dict(&self, reference: Reference) -> Result<bool> {
        Ok(self.resolve_dict(reference)?.is_some())
    }

    /// Returns whether `root` names an object whose value is a dictionary
    /// with `/Pages`, keeping what it finds for each entry in `checked`.
    fn is_catalog(&self, root: Reference, checked: &mut [Option<bool>]) -> Result<bool> {
        let Ok(slot) = self
            .entries
            .binary_search_by_key(&root.number(), |&(number, _)| number)
        else {
            return Ok(false);
        };
        let entry = self.entries[slot].1;
        if entry.generation != root.generation() {
            return Ok(false);
        }
        if let Some(known) = checked[slot] {
            return Ok(known);
        }
        let catalog = match entry.location {
            Location::Offset(_) => self
                .winners
                .binary_search_by_key(&root.number(), |object| object.number)
                .is_ok_and(|index| !self.winners[index].is_stream && self.winners[index].pages),
            Location::Member { stream, index } => self
                .member_dict(stream, index, root.number())?
                .is_some_and(|dict| dict.get("Pages").is_some()),
        };
        checked[slot] = Some(catalog);
        Ok(catalog)
    }

    /// Returns the last catalog object with a live `/Pages` target: first
    /// any `/Type /Catalog`, then any untyped dictionary whose `/Pages`
    /// points to a live `/Type /Pages` dictionary.
    fn live_catalog(&self, checked: &mut [Option<bool>]) -> Result<Option<Reference>> {
        // 1. Explicit `/Type /Catalog` with live `/Pages`.
        let mut best: Option<(usize, Reference)> = None;
        for object in self.winners {
            if object.kind == Kind::Catalog
                && !object.is_stream
                && find(self.entries, object.number)
                    .is_some_and(|entry| entry.location == Location::Offset(object.pos))
            {
                let reference = Reference::new(object.number, object.generation);
                if self.has_live_page_tree(reference, checked)?
                    && best.is_none_or(|(pos, _)| object.pos > pos)
                {
                    best = Some((object.pos, reference));
                }
            }
        }
        if let Some((_, reference)) = best {
            return Ok(Some(reference));
        }
        let catalog = Value::Name(Name::from("Catalog"));
        for (_, stream, decoded) in self.streams.iter().rev() {
            for (index, &(number, _)) in decoded.members().iter().enumerate().rev() {
                let Ok(index) = u32::try_from(index) else {
                    continue;
                };
                let location = Location::Member {
                    stream: *stream,
                    index,
                };
                if find(self.entries, number).is_some_and(|entry| entry.location == location)
                    && self
                        .member(decoded, index, number)?
                        .is_some_and(|dict| dict.get("Type") == Some(&catalog))
                {
                    let reference = Reference::new(number, 0);
                    if self.has_live_page_tree(reference, checked)? {
                        return Ok(Some(reference));
                    }
                }
            }
        }

        // 2. Untyped dictionary (no `/Type` key) with `/Pages` pointing to a
        //    live `/Type /Pages` dictionary.
        let pages_type = Value::Name(Name::from("Pages"));
        let mut untyped_best: Option<(usize, Reference)> = None;
        for object in self.winners {
            if !object.has_type
                && object.pages
                && !object.is_stream
                && find(self.entries, object.number)
                    .is_some_and(|entry| entry.location == Location::Offset(object.pos))
            {
                let reference = Reference::new(object.number, object.generation);
                if let Some(dict) = self.resolve_dict(reference)?
                    && let Some(&Value::Reference(pages_ref)) = dict.get("Pages")
                    && self
                        .resolve_dict(pages_ref)?
                        .is_some_and(|pages_dict| pages_dict.get("Type") == Some(&pages_type))
                    && untyped_best.is_none_or(|(pos, _)| object.pos > pos)
                {
                    untyped_best = Some((object.pos, reference));
                }
            }
        }
        if let Some((_, reference)) = untyped_best {
            return Ok(Some(reference));
        }
        for (_, stream, decoded) in self.streams.iter().rev() {
            for (index, &(number, _)) in decoded.members().iter().enumerate().rev() {
                let Ok(index) = u32::try_from(index) else {
                    continue;
                };
                let location = Location::Member {
                    stream: *stream,
                    index,
                };
                if find(self.entries, number).is_some_and(|entry| entry.location == location)
                    && let Some(dict) = self.member(decoded, index, number)?
                    && dict.get("Type").is_none()
                    && let Some(&Value::Reference(pages_ref)) = dict.get("Pages")
                    && self
                        .resolve_dict(pages_ref)?
                        .is_some_and(|pages_dict| pages_dict.get("Type") == Some(&pages_type))
                {
                    return Ok(Some(Reference::new(number, 0)));
                }
            }
        }
        Ok(None)
    }

    /// Returns the last object in the order of the bytes whose dictionary
    /// has `/Type /Catalog`: an uncompressed one, or else a member of the
    /// latest object stream that holds one.
    fn any_catalog(&self) -> Result<Option<Reference>> {
        if let Some(object) = self
            .winners
            .iter()
            .filter(|object| {
                object.kind == Kind::Catalog
                    && !object.is_stream
                    && find(self.entries, object.number)
                        .is_some_and(|entry| entry.location == Location::Offset(object.pos))
            })
            .max_by_key(|object| object.pos)
        {
            return Ok(Some(Reference::new(object.number, object.generation)));
        }
        let catalog = Value::Name(Name::from("Catalog"));
        for (_, stream, decoded) in self.streams.iter().rev() {
            for (index, &(number, _)) in decoded.members().iter().enumerate().rev() {
                let Ok(index) = u32::try_from(index) else {
                    continue;
                };
                let location = Location::Member {
                    stream: *stream,
                    index,
                };
                if find(self.entries, number).is_some_and(|entry| entry.location == location)
                    && self
                        .member(decoded, index, number)?
                        .is_some_and(|dict| dict.get("Type") == Some(&catalog))
                {
                    return Ok(Some(Reference::new(number, 0)));
                }
            }
        }
        Ok(None)
    }

    /// Scans `self.entries` for intact `/Type /Pages` roots and orphan
    /// `/Type /Page` dictionaries, and builds a [`SyntheticPlan`] when at
    /// least one page can be recovered.
    fn synthesize_page_tree(&self) -> Result<Option<SyntheticPlan<'b>>> {
        let mut ticker = Ticker::default();
        let pages_name = Value::Name(Name::from("Pages"));
        let page_name = Value::Name(Name::from("Page"));

        // Collect all `/Type /Pages` and `/Type /Page` non-stream dicts in
        // deterministic file order `(major_offset, minor_index)`.
        let mut pages_nodes: BudgetVec<'_, (usize, u32, usize, Reference)> =
            BudgetVec::new(self.budget);
        let mut page_objs: BudgetVec<'_, (usize, u32, usize, Reference, bool)> =
            BudgetVec::new(self.budget);

        for (slot, &(number, entry)) in self.entries.iter().enumerate() {
            ticker.tick(self.budget)?;
            match entry.location {
                Location::Offset(pos) => {
                    let Ok(wi) = self
                        .winners
                        .binary_search_by_key(&number, |object| object.number)
                    else {
                        continue;
                    };
                    let object = self.winners[wi];
                    if object.is_stream {
                        continue;
                    }
                    let reference = Reference::new(number, entry.generation);
                    match object.kind {
                        Kind::Pages => pages_nodes.try_push((pos, 0, slot, reference))?,
                        Kind::Page => {
                            page_objs.try_push((pos, 0, slot, reference, object.has_media_box))?
                        }
                        _ => {}
                    }
                }
                Location::Member { stream, index } => {
                    let Ok(bi) = self.by_number.binary_search_by_key(&stream, |&(s, _)| s) else {
                        continue;
                    };
                    let (stream_pos, _, ref decoded) = self.streams[self.by_number[bi].1];
                    let Some(dict) = self.member(decoded, index, number)? else {
                        continue;
                    };
                    let reference = Reference::new(number, entry.generation);
                    if dict.get("Type") == Some(&pages_name) {
                        pages_nodes.try_push((stream_pos, index, slot, reference))?;
                    } else if dict.get("Type") == Some(&page_name) {
                        page_objs.try_push((
                            stream_pos,
                            index,
                            slot,
                            reference,
                            dict.get("MediaBox").is_some(),
                        ))?;
                    }
                }
            }
        }

        if pages_nodes.is_empty() && page_objs.is_empty() {
            return Ok(None);
        }

        pages_nodes.sort_unstable_by_key(|&(pos, idx, _, r)| (pos, idx, r.number()));
        page_objs.sort_unstable_by_key(|&(pos, idx, _, r, _)| (pos, idx, r.number()));

        // Find root `/Type /Pages` nodes (no live `/Parent` in `entries`) that
        // form acyclic, vertex-disjoint trees reaching at least one valid
        // `/Type /Page` object.
        // `state[slot]`:
        //   0 = unvisited
        //   1 = in current DFS walk
        //   2 = claimed by an accepted root
        //   3 = explored and rejected
        // Each slot transitions 0 -> 1 -> {2, 3} at most once (F6 `O(V + E)`).
        const UNVISITED: u8 = 0;
        const IN_WALK: u8 = 1;
        const CLAIMED: u8 = 2;
        const REJECTED: u8 = 3;

        let mut state: BudgetVec<'_, u8> = BudgetVec::new(self.budget);
        state.try_reserve_exact(self.entries.len())?;
        state.try_extend(iter::repeat_n(UNVISITED, self.entries.len()))?;

        let mut root_pages: BudgetVec<'_, (Reference, usize)> = BudgetVec::new(self.budget);
        let mut stack: BudgetVec<'_, usize> = BudgetVec::new(self.budget);
        let mut visited_now: BudgetVec<'_, usize> = BudgetVec::new(self.budget);

        for &(_, _, root_slot, root_ref) in pages_nodes.iter() {
            ticker.tick(self.budget)?;
            if state[root_slot] != UNVISITED {
                continue;
            }
            let Some(root_dict) = self.dict_at(root_slot)? else {
                state[root_slot] = REJECTED;
                continue;
            };
            let has_live_parent = matches!(
                root_dict.get("Parent"),
                Some(&Value::Reference(parent)) if self.is_live_dict(parent)?
            );
            if has_live_parent {
                continue;
            }
            // Iterative bounded walk from `root_slot` to validate acyclicity,
            // disjointness, and reached page count.
            stack.clear();
            visited_now.clear();
            stack.try_push(root_slot)?;
            state[root_slot] = IN_WALK;
            visited_now.try_push(root_slot)?;
            let mut count_pages = 0usize;
            let mut valid_tree = true;
            while let Some(cur_slot) = stack.pop() {
                ticker.tick(self.budget)?;
                let Some(dict) = self.dict_at(cur_slot)? else {
                    continue;
                };
                if dict.get("Type") == Some(&pages_name) {
                    if let Some(Value::Array(kids)) = dict.get("Kids") {
                        for kid in kids.iter() {
                            ticker.tick(self.budget)?;
                            if let &Value::Reference(kid_ref) = kid
                                && let Some(kid_slot) = self.slot_of(kid_ref)
                            {
                                if state[kid_slot] == UNVISITED {
                                    state[kid_slot] = IN_WALK;
                                    visited_now.try_push(kid_slot)?;
                                    stack.try_push(kid_slot)?;
                                } else {
                                    // Cycle or duplicate kid in this walk (`IN_WALK`),
                                    // overlap with an accepted root (`CLAIMED`), or
                                    // shared child from a rejected walk (`REJECTED`).
                                    valid_tree = false;
                                }
                            }
                        }
                    }
                } else if dict.get("Type") == Some(&page_name) {
                    count_pages += 1;
                }
            }
            if count_pages > 0 && valid_tree {
                for &slot in visited_now.iter() {
                    state[slot] = CLAIMED;
                }
                root_pages.try_push((root_ref, count_pages))?;
            } else {
                for &slot in visited_now.iter() {
                    state[slot] = REJECTED;
                }
            }
        }

        // Gather orphan `/Type /Page` objects not reached by any root `/Pages`.
        let mut orphan_pages: BudgetVec<'_, (Reference, bool)> = BudgetVec::new(self.budget);
        for &(_, _, slot, page_ref, has_mb) in page_objs.iter() {
            if state[slot] != CLAIMED {
                orphan_pages.try_push((page_ref, has_mb))?;
            }
        }

        if root_pages.is_empty() && orphan_pages.is_empty() {
            return Ok(None);
        }

        // Allocate fresh object numbers above every existing entry.
        let max_num = self.entries.last().map_or(0, |&(n, _)| n);
        let Some(first_new) = max_num.checked_add(1) else {
            return Ok(None);
        };

        // Case 1: a single intact root `/Pages` already reaches every page.
        if root_pages.len() == 1 && orphan_pages.is_empty() {
            return Ok(Some(SyntheticPlan {
                pages: None,
                catalog: (first_new, root_pages[0].0),
            }));
        }

        // Case 2: synthesize a top-level `/Type /Pages` combining intact roots
        // and/or orphan `/Type /Page` objects, plus a `/Type /Catalog`.
        let Some(second_new) = first_new.checked_add(1) else {
            return Ok(None);
        };
        let mut kids = BudgetVec::new(self.budget);
        let mut total_count = 0usize;
        for &(root_ref, cnt) in root_pages.iter() {
            kids.try_push(root_ref)?;
            total_count = total_count.saturating_add(cnt);
        }
        let mut needs_mediabox = false;
        for &(page_ref, has_mb) in orphan_pages.iter() {
            kids.try_push(page_ref)?;
            total_count = total_count.saturating_add(1);
            if !has_mb {
                needs_mediabox = true;
            }
        }
        Ok(Some(SyntheticPlan {
            pages: Some((first_new, kids, total_count, needs_mediabox)),
            catalog: (second_new, Reference::new(first_new, 0)),
        }))
    }

    fn slot_of(&self, reference: Reference) -> Option<usize> {
        let slot = self
            .entries
            .binary_search_by_key(&reference.number(), |&(number, _)| number)
            .ok()?;
        (self.entries[slot].1.generation == reference.generation()).then_some(slot)
    }

    fn resolve_dict(&self, reference: Reference) -> Result<Option<Dict>> {
        let Some(slot) = self.slot_of(reference) else {
            return Ok(None);
        };
        self.dict_at(slot)
    }

    fn dict_at(&self, slot: usize) -> Result<Option<Dict>> {
        let (number, entry) = self.entries[slot];
        match entry.location {
            Location::Offset(_) => {
                let Ok(wi) = self
                    .winners
                    .binary_search_by_key(&number, |object| object.number)
                else {
                    return Ok(None);
                };
                if self.winners[wi].is_stream {
                    return Ok(None);
                }
                let Some((start, end)) = self.winners[wi].dict else {
                    return Ok(None);
                };
                dictionary(self.bytes, start..end, self.budget)
            }
            Location::Member { stream, index } => self.member_dict(stream, index, number),
        }
    }

    fn member_dict(&self, stream: u32, index: u32, number: u32) -> Result<Option<Dict>> {
        let decoded = self
            .by_number
            .binary_search_by_key(&stream, |&(s, _)| s)
            .ok()
            .map(|slot| &self.streams[self.by_number[slot].1].2);
        match decoded {
            Some(decoded) => self.member(decoded, index, number),
            None => Ok(None),
        }
    }

    /// Returns the dictionary of member `index` of `decoded`, if its table
    /// lists object `number` there and the member's value is a dictionary.
    fn member(&self, decoded: &Decoded, index: u32, number: u32) -> Result<Option<Dict>> {
        let Some(bytes) = decoded.member(index, number) else {
            return Ok(None);
        };
        match Value::parse(&bytes, self.budget) {
            Ok(Value::Dict(dict)) => Ok(Some(dict)),
            Err(error) if error.limit().is_some() => Err(error),
            _ => Ok(None),
        }
    }
}

/// Supplements `trailer` with missing `/Encrypt` and `/ID` entries moved out
/// of `candidates` when any trailer in the file carries `/Encrypt`. Returns
/// whether `trailer` was modified.
fn preserve_security(candidates: &mut [(usize, Dict)], trailer: &mut Dict) -> bool {
    let encrypted = trailer.get("Encrypt").is_some()
        || candidates.iter().any(|(_, d)| d.get("Encrypt").is_some());
    if !encrypted {
        return false;
    }
    let mut modified = false;
    if trailer.get("Encrypt").is_none()
        && let Some(enc) = candidates
            .iter_mut()
            .rev()
            .find_map(|(_, d)| d.remove("Encrypt"))
    {
        insert_const(trailer, "Encrypt", enc);
        modified = true;
    }
    if trailer.get("ID").is_none()
        && let Some(id) = candidates
            .iter_mut()
            .rev()
            .find_map(|(_, d)| d.remove("ID"))
    {
        insert_const(trailer, "ID", id);
        modified = true;
    }
    modified
}

/// Builds a trailer pointing `/Root` at `root`, moving the latest candidate
/// dictionary and preserving `/Encrypt`, `/ID`, and `/Info` from earlier
/// candidates without cloning whole dictionaries.
fn merge_trailer(mut candidates: BudgetVec<'_, (usize, Dict)>, root: Reference) -> Dict {
    let mut trailer = candidates.pop().map_or_else(Dict::new, |(_, dict)| dict);
    if trailer.get("Encrypt").is_none()
        && let Some(enc) = candidates
            .iter_mut()
            .rev()
            .find_map(|(_, d)| d.remove("Encrypt"))
    {
        insert_const(&mut trailer, "Encrypt", enc);
    }
    if trailer.get("ID").is_none()
        && let Some(id) = candidates
            .iter_mut()
            .rev()
            .find_map(|(_, d)| d.remove("ID"))
    {
        insert_const(&mut trailer, "ID", id);
    }
    if trailer.get("Info").is_none()
        && let Some(info) = candidates
            .iter_mut()
            .rev()
            .find_map(|(_, d)| d.remove("Info"))
    {
        insert_const(&mut trailer, "Info", info);
    }
    insert_const(&mut trailer, "Root", Value::Reference(root));
    trailer
}

fn insert_const(dict: &mut Dict, key: &'static str, value: Value) {
    #[expect(
        clippy::disallowed_methods,
        reason = "inserts a single constant trailer key (`Root`, `Encrypt`, `ID`, or `Info`)"
    )]
    dict.insert(Name::from(key), value);
}
