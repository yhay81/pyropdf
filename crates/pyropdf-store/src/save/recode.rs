//! Recoding: the stream data that a save compresses, decompresses, re-encodes
//! with an explicit filter, or wraps for ASCII-safe output on request
//! (DESIGN.md section 4.4).

use std::borrow::Cow;

use flate2::{Compress, Compression, FlushCompress, Status};

use super::Result;
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Array, Bytes, Dict, Name, Stream, Value, encode};
use crate::{ErrorKind, LimitExceeded};

/// Column width used to wrap ASCII85 and ASCIIHex streams produced by a save.
pub(super) const ASCII_WRAP_COLUMNS: usize = 80;

/// An explicit filter choice for eligible non-image streams on a full save.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StreamFilter {
    /// Leave the stream bytes and filters untouched (F3).
    Keep,
    /// Decode leading general-purpose filters, preserving any image filters.
    None,
    /// Re-encode with `FlateDecode` at the zlib level `0..=9`.
    Flate(u8),
    /// Re-encode with `ASCII85Decode`.
    Ascii85,
    /// Re-encode with `ASCIIHexDecode`.
    AsciiHex,
    /// Re-encode with `RunLengthDecode`.
    RunLength,
}

/// How a save writes the data of streams.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Streams {
    /// As they are.
    #[default]
    Keep,
    /// Compresses with Flate, at the level from 0 to 9, the data of each
    /// stream that has no filter, where that makes the object shorter. A
    /// level above 9 is 9. XMP metadata stays unfiltered, so that a tool
    /// that does not parse PDF finds it.
    Compress(u8),
    /// Decodes the leading filters that hayro-syntax decodes, so that a
    /// stream keeps only the filters from its first image filter on, such
    /// as `/DCTDecode`. A stream that does not decode stays as it is, and
    /// so does one whose data decodes to nothing, as hayro-syntax decodes
    /// data that is not of its filter.
    Decompress,
    /// Re-encodes eligible non-image streams with an explicit filter.
    Filter(StreamFilter),
}

/// Resolved document-wide stream policy and budgeted per-object overrides.
pub(super) struct StreamPolicy<'a> {
    default: Streams,
    /// Sorted by object number, deduplicated keeping the last entry per number.
    overrides: BudgetVec<'a, (u32, StreamFilter)>,
}

impl<'a> StreamPolicy<'a> {
    pub(super) fn new(
        default: Streams,
        raw_overrides: &[(u32, StreamFilter)],
        budget: &'a Budget,
    ) -> Result<Self> {
        let mut overrides = BudgetVec::new(budget);
        if !raw_overrides.is_empty() {
            overrides.try_reserve(raw_overrides.len())?;
            for &entry in raw_overrides {
                budget.poll()?;
                overrides.try_push(entry)?;
            }
            // Keep the last entry when duplicate object numbers are supplied.
            overrides.as_mut_slice().sort_by_key(|&(number, _)| number);
            let slice = overrides.as_mut_slice();
            let mut write = 0;
            for read in 0..slice.len() {
                if read + 1 < slice.len() && slice[read].0 == slice[read + 1].0 {
                    continue;
                }
                slice[write] = slice[read];
                write += 1;
            }
            overrides.truncate(write);
        }
        Ok(Self { default, overrides })
    }

    pub(super) fn override_for(&self, number: u32) -> Option<StreamFilter> {
        self.overrides
            .as_slice()
            .binary_search_by_key(&number, |&(object, _)| object)
            .ok()
            .map(|at| self.overrides[at].1)
    }

    /// Returns whether object `number` has no stream recoding when `ascii` is false.
    pub(super) fn is_keep(&self, number: u32) -> bool {
        match self.override_for(number) {
            Some(StreamFilter::Keep) => true,
            Some(_) => false,
            None => matches!(
                self.default,
                Streams::Keep | Streams::Filter(StreamFilter::Keep)
            ),
        }
    }

    /// Returns whether recoding object `number` keeps the original bytes unless
    /// the recoded object is strictly shorter (true only for `Streams::Compress`).
    pub(super) fn requires_shorter(&self, number: u32) -> bool {
        self.override_for(number).is_none() && matches!(self.default, Streams::Compress(_))
    }

    pub(super) const fn default_streams(&self) -> Streams {
        self.default
    }
}

/// The filters that hayro-syntax decodes without its image decoders.
const DECODED: [&[u8]; 5] = [
    b"ASCIIHexDecode",
    b"ASCII85Decode",
    b"LZWDecode",
    b"FlateDecode",
    b"RunLengthDecode",
];

/// Returns whether every byte of `bytes` is printable ASCII or ASCII line/tab whitespace.
pub(super) fn is_printable_ascii(bytes: &[u8]) -> bool {
    bytes
        .iter()
        .all(|&byte| matches!(byte, b'\t' | b'\n' | b'\r' | 0x20..=0x7E))
}

/// Returns `value` with its stream recoded as `streams` asks, or `None`
/// when it leaves `value` as it is.
#[cfg_attr(not(test), expect(dead_code, reason = "used by save unit tests"))]
pub(super) fn recoded(
    value: &Value,
    streams: Streams,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &Budget,
) -> Result<Option<Value>> {
    let policy = StreamPolicy::new(streams, &[], budget)?;
    apply(value, 0, &policy, false, lookup, budget)
}

/// Returns `value` with its stream recoded according to `policy` and `ascii`,
/// or `None` when it leaves `value` as it is.
pub(super) fn apply(
    value: &Value,
    number: u32,
    policy: &StreamPolicy<'_>,
    ascii: bool,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &Budget,
) -> Result<Option<Value>> {
    let Value::Stream(stream) = value else {
        return Ok(None);
    };
    // A stream with `/F` keeps its data in another file.
    if stream.dict().get("F").is_some() {
        return Ok(None);
    }
    let stage1 = if let Some(filter) = policy.override_for(number) {
        apply_filter(stream, filter, true, lookup, budget)?
    } else {
        match policy.default {
            Streams::Keep => None,
            Streams::Compress(level) => compressed(stream, level, budget)?,
            Streams::Decompress => decompress_resolved(stream, lookup, budget)?,
            Streams::Filter(filter) => apply_filter(stream, filter, false, lookup, budget)?,
        }
    };
    let current = stage1.as_ref().unwrap_or(stream);
    if ascii && !is_printable_ascii(current.data()) {
        let wrapped = wrap_ascii85(current, lookup, budget)?;
        return Ok(Some(Value::Stream(Box::new(wrapped))));
    }
    Ok(stage1.map(|stream| Value::Stream(Box::new(stream))))
}

fn decompress_resolved(
    stream: &Stream,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &Budget,
) -> Result<Option<Stream>> {
    match crate::value::filters::resolved(stream.dict(), lookup, budget)? {
        Cow::Borrowed(_) => decompressed(stream, budget),
        Cow::Owned(dict) => decompressed(&Stream::new(dict, stream.data().clone()), budget),
    }
}

fn apply_filter(
    stream: &Stream,
    filter: StreamFilter,
    explicit_target: bool,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &Budget,
) -> Result<Option<Stream>> {
    match filter {
        StreamFilter::Keep => Ok(None),
        StreamFilter::None => decompress_resolved(stream, lookup, budget),
        StreamFilter::Flate(_)
        | StreamFilter::Ascii85
        | StreamFilter::AsciiHex
        | StreamFilter::RunLength => {
            reencode_resolved(stream, filter, explicit_target, lookup, budget)
        }
    }
}

fn is_decoded_name(value: &Value) -> bool {
    matches!(value, Value::Name(name) if DECODED.contains(&name.as_bytes()))
}

fn reencode_resolved(
    stream: &Stream,
    filter: StreamFilter,
    explicit_target: bool,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &Budget,
) -> Result<Option<Stream>> {
    let resolved = crate::value::filters::resolved(stream.dict(), lookup, budget)?;
    let dict = resolved.as_ref();
    if !explicit_target {
        let is_image =
            matches!(dict.get("Subtype"), Some(Value::Name(name)) if name.as_bytes() == b"Image");
        let is_metadata =
            matches!(dict.get("Type"), Some(Value::Name(name)) if name.as_bytes() == b"Metadata");
        if is_image || is_metadata {
            return Ok(None);
        }
    }
    let filters: &[Value] = match dict.get("Filter") {
        None => &[],
        Some(Value::Array(array)) => array.as_slice(),
        Some(single) => std::slice::from_ref(single),
    };
    if !filters.iter().all(is_decoded_name) {
        // Leave image codecs (DCT/JPX/JBIG2/CCITT), Crypt, or unknown filters untouched.
        return Ok(None);
    }
    if filters.is_empty() && dict.get("DecodeParms").is_some() && !explicit_target {
        return Ok(None);
    }
    let raw: Cow<'_, [u8]> = if filters.is_empty() {
        Cow::Borrowed(stream.data().as_slice())
    } else {
        let source = match &resolved {
            Cow::Borrowed(_) => stream.clone(),
            Cow::Owned(resolved_dict) => Stream::new(resolved_dict.clone(), stream.data().clone()),
        };
        let data = match source.decoded(budget) {
            Ok(data) => data,
            Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if data.is_empty() && !stream.data().is_empty() {
            return Ok(None);
        }
        Cow::Owned(data)
    };
    let (encoded, filter_name) = match filter {
        StreamFilter::Flate(level) => {
            let out = deflate(&raw, level, usize::MAX, budget)?
                .ok_or_else(|| crate::Error::malformed("could not encode Flate stream"))?;
            (out, "FlateDecode")
        }
        StreamFilter::Ascii85 => (
            encode::ascii85(&raw, Some(ASCII_WRAP_COLUMNS), budget)?,
            "ASCII85Decode",
        ),
        StreamFilter::AsciiHex => (
            encode::ascii_hex(&raw, Some(ASCII_WRAP_COLUMNS), budget)?,
            "ASCIIHexDecode",
        ),
        StreamFilter::RunLength => (encode::run_length_bytes(&raw, budget)?, "RunLengthDecode"),
        StreamFilter::Keep | StreamFilter::None => unreachable!(),
    };
    let mut new_dict: Dict = dict.clone();
    new_dict.remove("DecodeParms");
    #[expect(
        clippy::disallowed_methods,
        reason = "the stream's dictionary replaces its single `/Filter` entry"
    )]
    new_dict.insert(Name::from("Filter"), Value::Name(Name::from(filter_name)));
    Ok(Some(Stream::new(new_dict, Bytes::new(encoded))))
}

/// Wraps `stream`'s encoded bytes with an outer `ASCII85Decode` filter so every
/// byte in the serialized stream is printable ASCII while preserving any
/// underlying filters (including lossy image filters) losslessly.
pub(super) fn wrap_ascii85(
    stream: &Stream,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &Budget,
) -> Result<Stream> {
    let resolved = crate::value::filters::resolved(stream.dict(), lookup, budget)?;
    let dict = resolved.as_ref();
    let encoded = encode::ascii85(stream.data().as_slice(), Some(ASCII_WRAP_COLUMNS), budget)?;
    let mut new_dict = dict.clone();
    match dict.get("Filter") {
        None => {
            new_dict.remove("DecodeParms");
            #[expect(
                clippy::disallowed_methods,
                reason = "the stream's dictionary gains one `/Filter` entry"
            )]
            new_dict.insert(
                Name::from("Filter"),
                Value::Name(Name::from("ASCII85Decode")),
            );
        }
        Some(Value::Name(existing)) => {
            budget.check(LimitCode::StreamFilterCount, 2)?;
            let mut filters = BudgetVec::new(budget);
            filters.try_push(Value::Name(Name::from("ASCII85Decode")))?;
            filters.try_push(Value::Name(existing.clone()))?;
            #[expect(
                clippy::disallowed_methods,
                reason = "updates the stream's `/Filter` and optional `/DecodeParms` entries"
            )]
            {
                new_dict.insert(
                    Name::from("Filter"),
                    Value::Array(Array::from(filters.into_vec())),
                );
                if let Some(parms) = dict.get("DecodeParms") {
                    if let Some(wrapped_parms) = prepend_null_parms(parms, budget)? {
                        new_dict.insert(Name::from("DecodeParms"), wrapped_parms);
                    } else {
                        new_dict.remove("DecodeParms");
                    }
                }
            }
        }
        Some(Value::Array(existing)) => {
            let new_len = existing.len().saturating_add(1);
            budget.check(LimitCode::StreamFilterCount, new_len as u64)?;
            let mut filters = BudgetVec::new(budget);
            filters.try_reserve(new_len)?;
            filters.try_push(Value::Name(Name::from("ASCII85Decode")))?;
            filters.try_extend(existing.iter().cloned())?;
            #[expect(
                clippy::disallowed_methods,
                reason = "updates the stream's `/Filter` and optional `/DecodeParms` entries"
            )]
            {
                new_dict.insert(
                    Name::from("Filter"),
                    Value::Array(Array::from(filters.into_vec())),
                );
                if let Some(parms) = dict.get("DecodeParms") {
                    if let Some(wrapped_parms) = prepend_null_parms(parms, budget)? {
                        new_dict.insert(Name::from("DecodeParms"), wrapped_parms);
                    } else {
                        new_dict.remove("DecodeParms");
                    }
                }
            }
        }
        Some(_) => {
            return Err(crate::Error::malformed(
                "cannot wrap a stream with an invalid /Filter for ASCII output",
            )
            .into());
        }
    }
    Ok(Stream::new(new_dict, Bytes::new(encoded)))
}

fn prepend_null_parms(parms: &Value, budget: &Budget) -> Result<Option<Value>> {
    match parms {
        Value::Null => Ok(None),
        Value::Array(items) => {
            let mut out = BudgetVec::new(budget);
            out.try_reserve(items.len().saturating_add(1))?;
            out.try_push(Value::Null)?;
            out.try_extend(items.iter().cloned())?;
            Ok(Some(Value::Array(Array::from(out.into_vec()))))
        }
        other => {
            let mut out = BudgetVec::new(budget);
            out.try_reserve(2)?;
            out.try_push(Value::Null)?;
            out.try_push(other.clone())?;
            Ok(Some(Value::Array(Array::from(out.into_vec()))))
        }
    }
}

pub(super) fn compressed(stream: &Stream, level: u8, budget: &Budget) -> Result<Option<Stream>> {
    let dict = stream.dict();
    let metadata =
        matches!(dict.get("Type"), Some(Value::Name(name)) if name.as_bytes() == b"Metadata");
    // `/DecodeParms` without a filter would apply to the new one.
    if metadata
        || dict.get("Filter").is_some()
        || dict.get("DecodeParms").is_some()
        || stream.data().is_empty()
    {
        return Ok(None);
    }
    let data = stream.data().as_slice();
    // Output that would not be shorter than the data is not kept.
    let Some(out) = deflate(data, level, data.len().saturating_sub(1), budget)? else {
        return Ok(None);
    };
    let mut dict = dict.clone();
    #[expect(
        clippy::disallowed_methods,
        reason = "the stream's dictionary gains one entry, its `/Filter`"
    )]
    dict.insert(Name::from("Filter"), Value::Name(Name::from("FlateDecode")));
    Ok(Some(Stream::new(dict, Bytes::new(out))))
}

fn decompressed(stream: &Stream, budget: &Budget) -> Result<Option<Stream>> {
    let dict = stream.dict();
    let decoded =
        |value: &Value| matches!(value, Value::Name(name) if DECODED.contains(&name.as_bytes()));
    // The filters and their parameters, which a lone filter gives directly.
    let (filters, parameters): (&[Value], Option<&[Value]>) = match dict.get("Filter") {
        Some(Value::Array(filters)) => (
            filters.as_slice(),
            match dict.get("DecodeParms") {
                Some(Value::Array(parameters)) => Some(parameters.as_slice()),
                _ => None,
            },
        ),
        Some(filter) => (std::slice::from_ref(filter), None),
        None => return Ok(None),
    };
    let count = filters.iter().take_while(|filter| decoded(filter)).count();
    if count == 0 {
        return Ok(None);
    }
    // The leading filters decode a copy of the stream; the rest stay.
    let mut leading = dict.clone();
    let mut rest = dict.clone();
    rest.remove("Filter");
    rest.remove("DecodeParms");
    if let Some(Value::Array(_)) = dict.get("Filter") {
        let split = parameters.map(|parameters| count.min(parameters.len()));
        #[expect(
            clippy::disallowed_methods,
            reason = "the dictionaries take parts of the stream's own arrays"
        )]
        {
            leading.insert(Name::from("Filter"), array(&filters[..count], budget)?);
            if let (Some(parameters), Some(split)) = (parameters, split) {
                leading.insert(
                    Name::from("DecodeParms"),
                    array(&parameters[..split], budget)?,
                );
            }
            if count < filters.len() {
                rest.insert(Name::from("Filter"), array(&filters[count..], budget)?);
                if let (Some(parameters), Some(split)) = (parameters, split) {
                    rest.insert(
                        Name::from("DecodeParms"),
                        array(&parameters[split..], budget)?,
                    );
                }
            }
        }
    }
    let data = match Stream::new(leading, stream.data().clone()).decoded(budget) {
        Ok(data) => data,
        Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    if data.is_empty() && !stream.data().is_empty() {
        return Ok(None);
    }
    Ok(Some(Stream::new(rest, Bytes::new(data))))
}

/// Returns the array of `values`, grown through `budget`.
fn array(values: &[Value], budget: &Budget) -> Result<Value> {
    let mut items = BudgetVec::new(budget);
    items.try_extend(values.iter().cloned())?;
    Ok(Value::Array(Array::from(items.into_vec())))
}

/// The input that [`deflate`] compresses between two polls of the budget.
const DEFLATE_STEP: usize = 64 * 1024;

/// Compresses `data` with Flate, in the zlib format, at the level from 0 to
/// 9, and returns `None` when the output would be longer than `limit`
/// bytes. The output buffer is charged to `budget` before it grows, and the
/// budget is polled between steps of the input, so cancellation and the
/// deadline stop a long compression.
///
/// # Errors
///
/// Returns the budget's refusal when the output buffer passes its memory
/// limit, or when the operation is cancelled or runs past its deadline.
pub fn deflate(
    data: &[u8],
    level: u8,
    limit: usize,
    budget: &Budget,
) -> crate::Result<Option<Vec<u8>>> {
    deflate_with(data, level, limit, budget, || budget.poll())
}

/// Compresses as [`deflate`] does, calling `poll` where it polls the budget.
pub(super) fn deflate_with(
    data: &[u8],
    level: u8,
    limit: usize,
    budget: &Budget,
    mut poll: impl FnMut() -> std::result::Result<(), LimitExceeded>,
) -> crate::Result<Option<Vec<u8>>> {
    poll()?;
    // zlib's `compressBound`: stored blocks, plus the header and trailer.
    let bound = data
        .len()
        .saturating_add(data.len() >> 12)
        .saturating_add(data.len() >> 14)
        .saturating_add(data.len() >> 25)
        .saturating_add(19);
    let mut out = BudgetVec::new(budget);
    out.try_extend(std::iter::repeat_n(0u8, bound.min(limit)))?;
    let mut encoder = Compress::new(Compression::new(u32::from(level.min(9))), true);
    loop {
        let read = usize::try_from(encoder.total_in()).unwrap_or(usize::MAX);
        let written = usize::try_from(encoder.total_out()).unwrap_or(usize::MAX);
        if written >= out.len() {
            // Some levels can pass the bound on data that does not compress.
            if out.len() >= limit {
                return Ok(None);
            }
            let more = (out.len() / 2).max(DEFLATE_STEP).min(limit - out.len());
            out.try_extend(std::iter::repeat_n(0u8, more))?;
        }
        let (Some(rest), Some(output)) = (data.get(read..), out.as_mut_slice().get_mut(written..))
        else {
            return Ok(None);
        };
        let (input, flush) = match rest.get(..DEFLATE_STEP) {
            Some(step) if rest.len() > DEFLATE_STEP => (step, FlushCompress::None),
            _ => (rest, FlushCompress::Finish),
        };
        match encoder.compress(input, output, flush) {
            Ok(Status::StreamEnd) => break,
            Ok(Status::Ok) => poll()?,
            Ok(Status::BufError) if encoder.total_out() > written as u64 => poll()?,
            Ok(Status::BufError) | Err(_) => return Ok(None),
        }
    }
    out.truncate(usize::try_from(encoder.total_out()).unwrap_or(usize::MAX));
    Ok(Some(out.into_vec()))
}
