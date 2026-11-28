//! Recoding: the stream data that a save compresses, decompresses, re-encodes
//! with an explicit filter, or wraps for ASCII-safe output on request
//! (DESIGN.md section 4.4).

use std::borrow::Cow;
use std::mem::size_of;

use flate2::{Compress, Compression, Decompress, FlushCompress, FlushDecompress, Status};

use super::{Format, Result};
use crate::budget::{Budget, BudgetVec, DecodeMeter, LimitCode, StreamKind};
use crate::security::{self, Handler};
use crate::value::{
    Array, Bytes, Dict, Name, Reference, Stream, Value, copy_dict_for_edit, copy_for_edit, encode,
};
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
    /// so does one whose data is corrupt and silently decodes to nothing.
    Decompress,
    /// Re-encodes eligible non-image streams with an explicit filter.
    Filter(StreamFilter),
}

/// Releases a temporary `LimitCode::MemorySize` charge when dropped.
pub(super) struct MemoryGuard<'b> {
    budget: &'b Budget,
    charged: u64,
}

impl<'b> MemoryGuard<'b> {
    pub(super) const fn new(budget: &'b Budget, charged: u64) -> Self {
        Self { budget, charged }
    }

    pub(super) fn add(&mut self, bytes: u64) {
        self.charged = self.charged.saturating_add(bytes);
    }

    /// Disarms the guard so its charge stays with the budget.
    fn disarm(mut self) {
        self.charged = 0;
    }
}

impl Drop for MemoryGuard<'_> {
    fn drop(&mut self) {
        if self.charged != 0 {
            self.budget.release(LimitCode::MemorySize, self.charged);
        }
    }
}

/// A temporary recoded stream whose `LimitCode::MemorySize` charges release on drop.
pub(super) struct RecodedStream<'b> {
    stream: Stream,
    guard: MemoryGuard<'b>,
}

impl<'b> RecodedStream<'b> {
    pub(super) const fn new_with_guard(stream: Stream, guard: MemoryGuard<'b>) -> Self {
        Self { stream, guard }
    }

    pub(super) const fn stream(&self) -> &Stream {
        &self.stream
    }

    pub(super) fn into_value_guard(self) -> RecodedValue<'b> {
        RecodedValue {
            value: Value::Stream(Box::new(self.stream)),
            guard: self.guard,
        }
    }
}

/// A temporary recoded value whose `LimitCode::MemorySize` charges release on drop.
pub(super) struct RecodedValue<'b> {
    value: Value,
    guard: MemoryGuard<'b>,
}

impl<'b> RecodedValue<'b> {
    pub(super) const fn value(&self) -> &Value {
        &self.value
    }

    fn into_value(self) -> Value {
        self.guard.disarm();
        self.value
    }
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
    Ok(apply_temp(value, number, policy, ascii, lookup, budget)?.map(RecodedValue::into_value))
}

/// Returns `value` with its stream recoded according to `policy` and `ascii`
/// inside a temporary guard that releases its `MemorySize` charges on drop.
pub(super) fn apply_temp<'b>(
    value: &Value,
    number: u32,
    policy: &StreamPolicy<'_>,
    ascii: bool,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &'b Budget,
) -> Result<Option<RecodedValue<'b>>> {
    let Value::Stream(stream) = value else {
        return Ok(None);
    };
    // A stream with `/F` keeps its data in another file (ISO 32000-2 §7.3.8.2),
    // so any inline bytes between `stream` and `endstream` are ignored.
    if stream.dict().get("F").is_some() {
        if ascii && !is_printable_ascii(stream.data()) {
            let mut guard = MemoryGuard::new(budget, 0);
            let dict = copy_dict_tracked(stream.dict(), &mut guard, budget)?;
            return Ok(Some(
                RecodedStream::new_with_guard(Stream::new(dict, Bytes::default()), guard)
                    .into_value_guard(),
            ));
        }
        return Ok(None);
    }
    let stage1 = if let Some(filter) = policy.override_for(number) {
        apply_filter(stream, filter, true, lookup, budget)?
    } else {
        match policy.default {
            Streams::Keep => None,
            Streams::Compress(level) => compressed_temp(stream, level, budget)?,
            Streams::Decompress => decompress_resolved(stream, lookup, budget)?,
            Streams::Filter(filter) => apply_filter(stream, filter, false, lookup, budget)?,
        }
    };
    let current = stage1
        .as_ref()
        .map_or(stream.as_ref(), RecodedStream::stream);
    if ascii && !is_printable_ascii(current.data()) {
        let wrapped = wrap_ascii85_temp(current, lookup, budget)?;
        drop(stage1);
        return Ok(Some(wrapped.into_value_guard()));
    }
    Ok(stage1.map(RecodedStream::into_value_guard))
}

/// Serializes `value` formatted per `format`, applying `policy` and
/// `format.ascii` (including comparing against the printable-ASCII or
/// `ASCII85Decode` baseline when `policy.requires_shorter` is true).
///
/// When `encrypt` is `Some((handler, source, target))`, encrypts the chosen
/// `Value` directly and serializes once via `format.write_value`.
pub(super) fn write_formatted<'b>(
    value: &Value,
    number: u32,
    policy: &StreamPolicy<'_>,
    format: Format,
    encrypt: Option<(&Handler, Reference, Reference)>,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u8>> {
    if let Some((handler, source, target)) = encrypt {
        debug_assert!(!format.ascii, "ASCII output is refused for encrypted saves");
        let recoded = if !policy.is_keep(number) {
            apply_temp(value, number, policy, false, lookup, budget)?
        } else {
            None
        };
        let keep_recoded = match &recoded {
            Some(cand) if policy.requires_shorter(number) => {
                let mut before = BudgetVec::new(budget);
                format.write_value(&mut before, value)?;
                let mut after = BudgetVec::new(budget);
                format.write_value(&mut after, cand.value())?;
                after.len() < before.len()
            }
            Some(_) => true,
            None => false,
        };
        let encrypted = if keep_recoded {
            let cand = recoded.expect("keep_recoded implies Some");
            let encrypted = security::encrypt_value_with(
                handler,
                (source, target),
                cand.value(),
                lookup,
                budget,
            )?;
            drop(cand);
            encrypted
        } else {
            drop(recoded);
            security::encrypt_value_with(handler, (source, target), value, lookup, budget)?
        };
        let mut out = BudgetVec::new(budget);
        format.write_value(&mut out, &encrypted)?;
        return Ok(out);
    }

    if !policy.requires_shorter(number) {
        let recoded = if !policy.is_keep(number) || format.ascii {
            apply_temp(value, number, policy, format.ascii, lookup, budget)?
        } else {
            None
        };
        let mut out = BudgetVec::new(budget);
        format.write_value(
            &mut out,
            recoded.as_ref().map_or(value, RecodedValue::value),
        )?;
        return Ok(out);
    }

    let Value::Stream(stream) = value else {
        let mut out = BudgetVec::new(budget);
        format.write_value(&mut out, value)?;
        return Ok(out);
    };
    if stream.dict().get("F").is_some() {
        let recoded = apply_temp(value, number, policy, format.ascii, lookup, budget)?;
        let mut out = BudgetVec::new(budget);
        format.write_value(
            &mut out,
            recoded.as_ref().map_or(value, RecodedValue::value),
        )?;
        return Ok(out);
    }
    let Streams::Compress(level) = policy.default else {
        unreachable!("requires_shorter is true only for Streams::Compress");
    };

    // Baseline: original value when !ascii or already printable ASCII,
    // otherwise wrap_ascii85(original).
    let mut before = BudgetVec::new(budget);
    if format.ascii && !is_printable_ascii(stream.data()) {
        let baseline = wrap_ascii85_temp(stream, lookup, budget)?.into_value_guard();
        format.write_value(&mut before, baseline.value())?;
    } else {
        format.write_value(&mut before, value)?;
    }

    let Some(mut comp) = compressed_temp(stream, level, budget)? else {
        return Ok(before);
    };
    if format.ascii && !is_printable_ascii(comp.stream().data()) {
        comp = wrap_ascii85_temp(comp.stream(), lookup, budget)?;
    }
    let candidate = comp.into_value_guard();
    let mut after = BudgetVec::new(budget);
    format.write_value(&mut after, candidate.value())?;
    drop(candidate);
    if after.len() < before.len() {
        Ok(after)
    } else {
        Ok(before)
    }
}

fn decompress_resolved<'b>(
    stream: &Stream,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &'b Budget,
) -> Result<Option<RecodedStream<'b>>> {
    match crate::value::filters::resolved(stream.dict(), lookup, budget)? {
        Cow::Borrowed(_) => decompressed(stream, budget),
        Cow::Owned(dict) => decompressed(&Stream::new(dict, stream.data().clone()), budget),
    }
}

fn apply_filter<'b>(
    stream: &Stream,
    filter: StreamFilter,
    explicit_target: bool,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &'b Budget,
) -> Result<Option<RecodedStream<'b>>> {
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

fn reencode_resolved<'b>(
    stream: &Stream,
    filter: StreamFilter,
    explicit_target: bool,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &'b Budget,
) -> Result<Option<RecodedStream<'b>>> {
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
        None | Some(Value::Null) => &[],
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
        let decoded = match &resolved {
            Cow::Borrowed(_) => stream.decoded(budget),
            Cow::Owned(resolved_dict) => {
                let mut temp_guard = MemoryGuard::new(budget, 0);
                let temp_dict = copy_dict_tracked(resolved_dict, &mut temp_guard, budget)?;
                Stream::new(temp_dict, stream.data().clone()).decoded(budget)
            }
        };
        let data = match decoded {
            Ok(data) => data,
            Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if data.is_empty()
            && !stream.data().is_empty()
            && !is_valid_empty_encoded(filters, dict.get("DecodeParms"), stream.data(), budget)?
        {
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
    let mut guard = MemoryGuard::new(
        budget,
        u64::try_from(encoded.capacity()).unwrap_or(u64::MAX),
    );
    let mut new_dict = match resolved {
        Cow::Borrowed(dict) => copy_dict_tracked(dict, &mut guard, budget)?,
        Cow::Owned(dict) => dict,
    };
    new_dict.remove("DecodeParms");
    #[expect(
        clippy::disallowed_methods,
        reason = "the stream's dictionary replaces its single `/Filter` entry"
    )]
    new_dict.insert(Name::from("Filter"), Value::Name(Name::from(filter_name)));
    Ok(Some(RecodedStream::new_with_guard(
        Stream::new(new_dict, Bytes::new(encoded)),
        guard,
    )))
}

/// Wraps `stream`'s encoded bytes with an outer `ASCII85Decode` filter (or right
/// after a leading `/Crypt` `/Identity` filter per ISO 32000-2 §7.4.10) so
/// every byte in the serialized stream is printable ASCII while preserving any
/// underlying filters (including lossy image filters) losslessly.
pub(super) fn wrap_ascii85_temp<'b>(
    stream: &Stream,
    lookup: &crate::value::FilterLookup<'_>,
    budget: &'b Budget,
) -> Result<RecodedStream<'b>> {
    let resolved = crate::value::filters::resolved(stream.dict(), lookup, budget)?;
    let has_crypt = match crate::security::filters::explicit(resolved.as_ref(), budget)? {
        Some(b"Identity") => true,
        Some(_) => {
            return Err(crate::Error::malformed(
                "cannot wrap a stream with a non-Identity /Crypt filter for ASCII output",
            )
            .into());
        }
        None => false,
    };
    let encoded = encode::ascii85(stream.data().as_slice(), Some(ASCII_WRAP_COLUMNS), budget)?;
    let mut guard = MemoryGuard::new(
        budget,
        u64::try_from(encoded.capacity()).unwrap_or(u64::MAX),
    );
    let mut new_dict = match resolved {
        Cow::Borrowed(dict) => copy_dict_tracked(dict, &mut guard, budget)?,
        Cow::Owned(dict) => dict,
    };
    if has_crypt {
        wrap_ascii85_after_crypt(&mut new_dict, &mut guard, budget)?;
    } else {
        wrap_ascii85_leading(&mut new_dict, &mut guard, budget)?;
    }
    Ok(RecodedStream::new_with_guard(
        Stream::new(new_dict, Bytes::new(encoded)),
        guard,
    ))
}

fn set_single_ascii85(dict: &mut Dict) {
    dict.remove("DecodeParms");
    #[expect(
        clippy::disallowed_methods,
        reason = "the stream's dictionary gains one `/Filter` entry"
    )]
    dict.insert(
        Name::from("Filter"),
        Value::Name(Name::from("ASCII85Decode")),
    );
}

fn wrap_ascii85_leading(
    new_dict: &mut Dict,
    guard: &mut MemoryGuard<'_>,
    budget: &Budget,
) -> Result<()> {
    match new_dict.get("Filter") {
        None | Some(Value::Null) => set_single_ascii85(new_dict),
        Some(Value::Array(existing)) if existing.is_empty() => set_single_ascii85(new_dict),
        Some(Value::Name(_)) => {
            budget.check(LimitCode::StreamFilterCount, 2)?;
            let existing = new_dict.remove("Filter").expect("checked Name");
            let mut filters = BudgetVec::new(budget);
            filters.try_reserve_exact(2)?;
            filters.try_push(Value::Name(Name::from("ASCII85Decode")))?;
            filters.try_push(existing)?;
            let parms = new_dict.remove("DecodeParms");
            #[expect(
                clippy::disallowed_methods,
                reason = "updates the stream's `/Filter` and optional `/DecodeParms` entries"
            )]
            {
                new_dict.insert(
                    Name::from("Filter"),
                    Value::Array(Array::from(take_vec(filters, guard))),
                );
                if let Some(parms) = parms
                    && let Some(wrapped) = prepend_null_parms(parms, guard, budget)?
                {
                    new_dict.insert(Name::from("DecodeParms"), wrapped);
                }
            }
        }
        Some(Value::Array(existing)) => {
            let new_len = existing.len().saturating_add(1);
            budget.check(LimitCode::StreamFilterCount, new_len as u64)?;
            let mut filters = BudgetVec::new(budget);
            filters.try_reserve_exact(new_len)?;
            filters.try_push(Value::Name(Name::from("ASCII85Decode")))?;
            for item in existing.iter() {
                filters.try_push(copy_value_tracked(item, guard, budget)?)?;
            }
            let parms = new_dict.remove("DecodeParms");
            #[expect(
                clippy::disallowed_methods,
                reason = "updates the stream's `/Filter` and optional `/DecodeParms` entries"
            )]
            {
                new_dict.insert(
                    Name::from("Filter"),
                    Value::Array(Array::from(take_vec(filters, guard))),
                );
                if let Some(parms) = parms
                    && let Some(wrapped) = prepend_null_parms(parms, guard, budget)?
                {
                    new_dict.insert(Name::from("DecodeParms"), wrapped);
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
    Ok(())
}

/// Inserts `/ASCII85Decode` immediately after a validated leading `/Crypt`
/// filter so `/Crypt` remains first as required by ISO 32000-2 §7.4.10.
fn wrap_ascii85_after_crypt(
    new_dict: &mut Dict,
    guard: &mut MemoryGuard<'_>,
    budget: &Budget,
) -> Result<()> {
    let existing = new_dict.remove("Filter");
    let mut filters = BudgetVec::new(budget);
    match existing {
        Some(crypt @ Value::Name(_)) => {
            budget.check(LimitCode::StreamFilterCount, 2)?;
            filters.try_reserve_exact(2)?;
            filters.try_push(crypt)?;
            filters.try_push(Value::Name(Name::from("ASCII85Decode")))?;
        }
        Some(Value::Array(items)) if !items.is_empty() => {
            let new_len = items.len().saturating_add(1);
            budget.check(LimitCode::StreamFilterCount, new_len as u64)?;
            filters.try_reserve_exact(new_len)?;
            filters.try_push(copy_value_tracked(&items.as_slice()[0], guard, budget)?)?;
            filters.try_push(Value::Name(Name::from("ASCII85Decode")))?;
            for item in &items.as_slice()[1..] {
                filters.try_push(copy_value_tracked(item, guard, budget)?)?;
            }
        }
        _ => {
            return Err(crate::Error::malformed(
                "cannot wrap a stream with an invalid /Filter for ASCII output",
            )
            .into());
        }
    }
    let parms = new_dict.remove("DecodeParms");
    #[expect(
        clippy::disallowed_methods,
        reason = "updates the stream's `/Filter` and optional `/DecodeParms` entries"
    )]
    {
        new_dict.insert(
            Name::from("Filter"),
            Value::Array(Array::from(take_vec(filters, guard))),
        );
        if let Some(parms) = parms
            && let Some(wrapped) = insert_after_crypt_parms(parms, guard, budget)?
        {
            new_dict.insert(Name::from("DecodeParms"), wrapped);
        }
    }
    Ok(())
}

fn prepend_null_parms(
    parms: Value,
    guard: &mut MemoryGuard<'_>,
    budget: &Budget,
) -> Result<Option<Value>> {
    match parms {
        Value::Null => Ok(None),
        Value::Array(items) if items.is_empty() => Ok(None),
        Value::Array(items) => {
            let mut out = BudgetVec::new(budget);
            out.try_reserve_exact(items.len().saturating_add(1))?;
            out.try_push(Value::Null)?;
            for item in items.iter() {
                out.try_push(copy_value_tracked(item, guard, budget)?)?;
            }
            Ok(Some(Value::Array(Array::from(take_vec(out, guard)))))
        }
        other => {
            let mut out = BudgetVec::new(budget);
            out.try_reserve_exact(2)?;
            out.try_push(Value::Null)?;
            out.try_push(other)?;
            Ok(Some(Value::Array(Array::from(take_vec(out, guard)))))
        }
    }
}

fn insert_after_crypt_parms(
    parms: Value,
    guard: &mut MemoryGuard<'_>,
    budget: &Budget,
) -> Result<Option<Value>> {
    match parms {
        Value::Null => Ok(None),
        Value::Array(items) if items.is_empty() => Ok(None),
        Value::Array(items) => {
            let mut out = BudgetVec::new(budget);
            out.try_reserve_exact(items.len().saturating_add(1))?;
            out.try_push(copy_value_tracked(&items.as_slice()[0], guard, budget)?)?;
            out.try_push(Value::Null)?;
            for item in &items.as_slice()[1..] {
                out.try_push(copy_value_tracked(item, guard, budget)?)?;
            }
            Ok(Some(Value::Array(Array::from(take_vec(out, guard)))))
        }
        other => {
            let mut out = BudgetVec::new(budget);
            out.try_reserve_exact(2)?;
            out.try_push(other)?;
            out.try_push(Value::Null)?;
            Ok(Some(Value::Array(Array::from(take_vec(out, guard)))))
        }
    }
}

pub(super) fn compressed_temp<'b>(
    stream: &Stream,
    level: u8,
    budget: &'b Budget,
) -> Result<Option<RecodedStream<'b>>> {
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
    let mut guard = MemoryGuard::new(budget, u64::try_from(out.capacity()).unwrap_or(u64::MAX));
    let mut dict = copy_dict_tracked(dict, &mut guard, budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "the stream's dictionary gains one entry, its `/Filter`"
    )]
    dict.insert(Name::from("Filter"), Value::Name(Name::from("FlateDecode")));
    Ok(Some(RecodedStream::new_with_guard(
        Stream::new(dict, Bytes::new(out)),
        guard,
    )))
}

fn decompressed<'b>(stream: &Stream, budget: &'b Budget) -> Result<Option<RecodedStream<'b>>> {
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
        Some(Value::Null) | None => return Ok(None),
        Some(filter) => (std::slice::from_ref(filter), None),
    };
    let count = filters.iter().take_while(|filter| decoded(filter)).count();
    if count == 0 {
        return Ok(None);
    }
    // The leading filters decode a copy of the stream; the rest stay.
    let mut leading_guard = MemoryGuard::new(budget, 0);
    let mut leading = copy_dict_tracked(dict, &mut leading_guard, budget)?;
    let mut rest_guard = MemoryGuard::new(budget, 0);
    let mut rest = copy_dict_tracked(dict, &mut rest_guard, budget)?;
    rest.remove("Filter");
    rest.remove("DecodeParms");
    if let Some(Value::Array(_)) = dict.get("Filter") {
        let split = parameters.map(|parameters| count.min(parameters.len()));
        #[expect(
            clippy::disallowed_methods,
            reason = "the dictionaries take parts of the stream's own arrays"
        )]
        {
            leading.insert(
                Name::from("Filter"),
                array(&filters[..count], &mut leading_guard, budget)?,
            );
            if let (Some(parameters), Some(split)) = (parameters, split) {
                leading.insert(
                    Name::from("DecodeParms"),
                    array(&parameters[..split], &mut leading_guard, budget)?,
                );
            }
            if count < filters.len() {
                rest.insert(
                    Name::from("Filter"),
                    array(&filters[count..], &mut rest_guard, budget)?,
                );
                if let (Some(parameters), Some(split)) = (parameters, split) {
                    rest.insert(
                        Name::from("DecodeParms"),
                        array(&parameters[split..], &mut rest_guard, budget)?,
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
    drop(leading_guard);
    if data.is_empty()
        && !stream.data().is_empty()
        && !is_valid_empty_encoded(
            &filters[..count],
            dict.get("DecodeParms"),
            stream.data(),
            budget,
        )?
    {
        return Ok(None);
    }
    Ok(Some(RecodedStream::new_with_guard(
        Stream::new(rest, Bytes::new(data)),
        rest_guard,
    )))
}

/// Returns the array of `values`, grown through `budget` with copies tracked in `guard`.
fn array(values: &[Value], guard: &mut MemoryGuard<'_>, budget: &Budget) -> Result<Value> {
    let mut items = BudgetVec::new(budget);
    items.try_reserve_exact(values.len())?;
    for value in values {
        items.try_push(copy_value_tracked(value, guard, budget)?)?;
    }
    Ok(Value::Array(Array::from(take_vec(items, guard))))
}

/// Verifies that `encoded` genuinely decodes to empty bytes through `filters`
/// (distinguishing valid empty Flate/ASCII85/ASCIIHex/RunLength/LZW streams
/// from corrupt streams that hayro-syntax's lenient fallback decodes to `[]`).
fn is_valid_empty_encoded(
    filters: &[Value],
    decode_parms: Option<&Value>,
    encoded: &[u8],
    budget: &Budget,
) -> Result<bool> {
    let parms: &[Value] = match decode_parms {
        Some(Value::Array(items)) => items.as_slice(),
        Some(Value::Null) | None => &[],
        Some(single) => std::slice::from_ref(single),
    };
    let mut buf: Option<BudgetVec<'_, u8>> = None;
    let mut meter = budget.decode(StreamKind::Any);
    for (idx, filter) in filters.iter().enumerate() {
        budget.poll()?;
        let Value::Name(name) = filter else {
            return Ok(false);
        };
        let expect_empty = idx + 1 == filters.len();
        let input = buf.as_deref().unwrap_or(encoded);
        let Some(next) = strict_decode_step(
            name.as_bytes(),
            parms.get(idx),
            input,
            expect_empty,
            &mut meter,
            budget,
        )?
        else {
            return Ok(false);
        };
        buf = Some(next);
    }
    Ok(buf.is_none_or(|b| b.is_empty()))
}

fn strict_decode_step<'b>(
    filter: &[u8],
    parms: Option<&Value>,
    input: &[u8],
    expect_empty: bool,
    meter: &mut DecodeMeter<'b>,
    budget: &'b Budget,
) -> Result<Option<BudgetVec<'b, u8>>> {
    match filter {
        b"FlateDecode" => {
            if let Some(out) = flate_strict(input, true, expect_empty, meter, budget)? {
                return Ok(Some(out));
            }
            flate_strict(input, false, expect_empty, meter, budget)
        }
        b"ASCIIHexDecode" => ascii_hex_strict(input, expect_empty, meter, budget),
        b"ASCII85Decode" => ascii85_strict(input, expect_empty, meter, budget),
        b"RunLengthDecode" => run_length_strict(input, expect_empty, meter, budget),
        b"LZWDecode" => lzw_strict(input, parms, expect_empty, meter, budget),
        _ => Ok(None),
    }
}

fn flate_strict<'b>(
    input: &[u8],
    zlib: bool,
    expect_empty: bool,
    meter: &mut DecodeMeter<'b>,
    budget: &'b Budget,
) -> Result<Option<BudgetVec<'b, u8>>> {
    if input.is_empty() {
        return Ok(None);
    }
    let mut dec = Decompress::new(zlib);
    let mut out = BudgetVec::new(budget);
    let mut chunk = [0u8; 4096];
    loop {
        budget.poll()?;
        let in_pos = usize::try_from(dec.total_in()).unwrap_or(input.len());
        let before_in = dec.total_in();
        let before_out = dec.total_out();
        let rest = input.get(in_pos..).unwrap_or(&[]);
        let status = match dec.decompress(rest, &mut chunk, FlushDecompress::Finish) {
            Ok(status) => status,
            Err(_) => return Ok(None),
        };
        let produced = usize::try_from(dec.total_out().saturating_sub(before_out)).unwrap_or(0);
        if produced > 0 {
            if expect_empty {
                return Ok(None);
            }
            meter.charge(produced as u64)?;
            out.try_extend_from_slice(&chunk[..produced])?;
        }
        match status {
            Status::StreamEnd => return Ok(Some(out)),
            Status::Ok => {}
            Status::BufError => {
                if dec.total_in() == before_in && produced == 0 {
                    return Ok(None);
                }
            }
        }
    }
}

fn is_pdf_white(byte: u8) -> bool {
    matches!(byte, 0x00 | b'\t' | b'\n' | 0x0C | b'\r' | b' ')
}

fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn ascii_hex_strict<'b>(
    input: &[u8],
    expect_empty: bool,
    meter: &mut DecodeMeter<'b>,
    budget: &'b Budget,
) -> Result<Option<BudgetVec<'b, u8>>> {
    let mut out = BudgetVec::new(budget);
    let mut high: Option<u8> = None;
    for chunk in input.chunks(4096) {
        budget.poll()?;
        for &byte in chunk {
            if is_pdf_white(byte) {
                continue;
            }
            if byte == b'>' {
                if let Some(hi) = high {
                    if expect_empty {
                        return Ok(None);
                    }
                    meter.charge(1)?;
                    out.try_push(hi << 4)?;
                }
                return Ok(Some(out));
            }
            let Some(nibble) = hex_nibble(byte) else {
                return Ok(None);
            };
            if let Some(hi) = high.take() {
                if expect_empty {
                    return Ok(None);
                }
                meter.charge(1)?;
                out.try_push((hi << 4) | nibble)?;
            } else {
                high = Some(nibble);
            }
        }
    }
    if let Some(hi) = high {
        if expect_empty {
            return Ok(None);
        }
        meter.charge(1)?;
        out.try_push(hi << 4)?;
    }
    Ok(Some(out))
}

fn ascii85_strict<'b>(
    input: &[u8],
    expect_empty: bool,
    meter: &mut DecodeMeter<'b>,
    budget: &'b Budget,
) -> Result<Option<BudgetVec<'b, u8>>> {
    const POW_85: [u32; 5] = [52_200_625, 614_125, 7_225, 85, 1];
    let flush = |group: &mut [u8; 5],
                 len: &mut usize,
                 out: &mut BudgetVec<'b, u8>,
                 meter: &mut DecodeMeter<'b>|
     -> Result<bool> {
        let count = *len;
        if count == 0 {
            return Ok(true);
        }
        if count == 1 || expect_empty {
            return Ok(false);
        }
        let mut digits = [u32::from(b'u' - b'!'); 5];
        for i in 0..count {
            digits[i] = u32::from(group[i] - b'!');
        }
        let mut value = 0u32;
        for (digit, weight) in digits.into_iter().zip(POW_85) {
            let Some(term) = digit.checked_mul(weight) else {
                return Ok(false);
            };
            let Some(sum) = value.checked_add(term) else {
                return Ok(false);
            };
            value = sum;
        }
        let produced = count - 1;
        meter.charge(produced as u64)?;
        out.try_extend_from_slice(&value.to_be_bytes()[..produced])?;
        *len = 0;
        Ok(true)
    };

    let mut out = BudgetVec::new(budget);
    let mut group = [0u8; 5];
    let mut group_len = 0usize;
    let mut idx = 0usize;
    while idx < input.len() {
        if idx.is_multiple_of(4096) {
            budget.poll()?;
        }
        let byte = input[idx];
        idx += 1;
        if is_pdf_white(byte) {
            continue;
        }
        match byte {
            b'!'..=b'u' => {
                group[group_len] = byte;
                group_len += 1;
                if group_len == 5 && !flush(&mut group, &mut group_len, &mut out, meter)? {
                    return Ok(None);
                }
            }
            b'z' => {
                if group_len != 0 || expect_empty {
                    return Ok(None);
                }
                meter.charge(4)?;
                out.try_extend_from_slice(&[0; 4])?;
            }
            b'~' => {
                if input.get(idx) != Some(&b'>') {
                    return Ok(None);
                }
                if !flush(&mut group, &mut group_len, &mut out, meter)? {
                    return Ok(None);
                }
                return Ok(Some(out));
            }
            _ => return Ok(None),
        }
    }
    if !flush(&mut group, &mut group_len, &mut out, meter)? {
        return Ok(None);
    }
    Ok(Some(out))
}

fn run_length_strict<'b>(
    input: &[u8],
    expect_empty: bool,
    meter: &mut DecodeMeter<'b>,
    budget: &'b Budget,
) -> Result<Option<BudgetVec<'b, u8>>> {
    let mut out = BudgetVec::new(budget);
    let mut at = 0usize;
    while at < input.len() {
        budget.poll()?;
        match input[at] {
            128 => return Ok(Some(out)),
            len @ 0..=127 => {
                let count = usize::from(len) + 1;
                if expect_empty || at.saturating_add(1).saturating_add(count) > input.len() {
                    return Ok(None);
                }
                meter.charge(count as u64)?;
                out.try_extend_from_slice(&input[at + 1..at + 1 + count])?;
                at += 1 + count;
            }
            len => {
                let count = 257 - usize::from(len);
                if expect_empty || at + 1 >= input.len() {
                    return Ok(None);
                }
                meter.charge(count as u64)?;
                let byte = input[at + 1];
                out.try_extend(std::iter::repeat_n(byte, count))?;
                at += 2;
            }
        }
    }
    Ok(None)
}

fn lzw_strict<'b>(
    input: &[u8],
    parms: Option<&Value>,
    expect_empty: bool,
    meter: &mut DecodeMeter<'b>,
    budget: &'b Budget,
) -> Result<Option<BudgetVec<'b, u8>>> {
    if expect_empty {
        // Before any byte is emitted, LZW stays at 9-bit MSB codes where only
        // CLEAR_TABLE (256) and EOD (257) are valid.
        let mut bit_pos = 0usize;
        let total_bits = input.len().saturating_mul(8);
        while bit_pos + 9 <= total_bits {
            budget.poll()?;
            let byte_idx = bit_pos / 8;
            let bit_off = bit_pos % 8;
            let pair = u16::from_be_bytes([input[byte_idx], input[byte_idx + 1]]);
            let code = (pair >> (7 - bit_off)) & 0x1FF;
            bit_pos += 9;
            match code {
                256 => {}
                257 => return Ok(Some(BudgetVec::new(budget))),
                _ => return Ok(None),
            }
        }
        return Ok(None);
    }
    let mut dict = Dict::new();
    let mut guard = MemoryGuard::new(budget, 0);
    #[expect(
        clippy::disallowed_methods,
        reason = "temporary single-filter dictionary for LZW step validation"
    )]
    {
        dict.insert(Name::from("Filter"), Value::Name(Name::from("LZWDecode")));
        if let Some(parms) = parms
            && !matches!(parms, Value::Null)
        {
            dict.insert(
                Name::from("DecodeParms"),
                copy_value_tracked(parms, &mut guard, budget)?,
            );
        }
    }
    let mut copy_in = BudgetVec::new(budget);
    copy_in.try_extend_from_slice(input)?;
    let input_cap = u64::try_from(copy_in.capacity()).unwrap_or(u64::MAX);
    guard.add(input_cap);
    let stream = Stream::new(dict, Bytes::new(copy_in.into_vec()));
    let data = match stream.decoded(budget) {
        Ok(data) => data,
        Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    drop(stream);
    drop(guard);
    if data.is_empty() {
        return lzw_strict(input, parms, true, meter, budget);
    }
    meter.charge(data.len() as u64)?;
    let mut out = BudgetVec::new(budget);
    out.try_extend_from_slice(&data)?;
    Ok(Some(out))
}

/// Transfers `vec`'s `LimitCode::MemorySize` charge to `guard` and returns the inner `Vec`.
fn take_vec<T>(vec: BudgetVec<'_, T>, guard: &mut MemoryGuard<'_>) -> Vec<T> {
    let bytes = u64::try_from(vec.capacity())
        .unwrap_or(u64::MAX)
        .saturating_mul(u64::try_from(size_of::<T>()).unwrap_or(u64::MAX));
    guard.add(bytes);
    vec.into_vec()
}

/// Exact `LimitCode::MemorySize` charge of `copy_dict_for_edit`'s entries table.
fn dict_entries_charge(len: usize) -> u64 {
    let capacity = len.saturating_add(8).saturating_mul(2);
    u64::try_from(capacity)
        .unwrap_or(u64::MAX)
        .saturating_mul(u64::try_from(size_of::<(Name, Value)>()).unwrap_or(u64::MAX))
}

/// Copies `dict` via `copy_dict_for_edit` and records its exact `MemorySize`
/// charge in `guard` so it releases when the temporary stream is dropped.
fn copy_dict_tracked(dict: &Dict, guard: &mut MemoryGuard<'_>, budget: &Budget) -> Result<Dict> {
    let charge = copied_dict_charge(dict, budget)?;
    let copied = copy_dict_for_edit(dict, budget)?;
    guard.add(charge);
    Ok(copied)
}

/// Copies `value` via `copy_for_edit` and records its exact `MemorySize`
/// charge in `guard` so it releases when the temporary stream is dropped.
fn copy_value_tracked(
    value: &Value,
    guard: &mut MemoryGuard<'_>,
    budget: &Budget,
) -> Result<Value> {
    let charge = copied_value_charge(value, budget)?;
    let copied = copy_for_edit(value, budget)?;
    guard.add(charge);
    Ok(copied)
}

/// Computes the exact `LimitCode::MemorySize` bytes charged by `copy_dict_for_edit`.
fn copied_dict_charge(dict: &Dict, budget: &Budget) -> Result<u64> {
    let mut total = dict_entries_charge(dict.len());
    let mut stack = BudgetVec::new(budget);
    for (key, value) in dict.iter() {
        total = total.saturating_add(u64::try_from(key.as_bytes().len()).unwrap_or(u64::MAX));
        stack.try_push(value)?;
    }
    accumulate_value_charges(&mut stack, &mut total)?;
    Ok(total)
}

/// Computes the exact `LimitCode::MemorySize` bytes charged by `copy_for_edit`.
fn copied_value_charge(value: &Value, budget: &Budget) -> Result<u64> {
    let mut total = 0u64;
    let mut stack = BudgetVec::new(budget);
    stack.try_push(value)?;
    accumulate_value_charges(&mut stack, &mut total)?;
    Ok(total)
}

fn accumulate_value_charges(stack: &mut BudgetVec<'_, &Value>, total: &mut u64) -> Result<()> {
    while let Some(value) = stack.pop() {
        stack.budget().poll()?;
        match value {
            Value::Null
            | Value::Boolean(_)
            | Value::Integer(_)
            | Value::Real(_)
            | Value::Reference(_) => {}
            Value::Name(name) => {
                *total =
                    total.saturating_add(u64::try_from(name.as_bytes().len()).unwrap_or(u64::MAX));
            }
            Value::String(string) => {
                *total = total
                    .saturating_add(u64::try_from(string.as_bytes().len()).unwrap_or(u64::MAX));
            }
            Value::Array(array) => {
                let items_bytes = u64::try_from(array.len())
                    .unwrap_or(u64::MAX)
                    .saturating_mul(u64::try_from(size_of::<Value>()).unwrap_or(u64::MAX));
                *total = total.saturating_add(items_bytes);
                stack.try_extend(array.iter())?;
            }
            Value::Dict(dict) => {
                *total = total.saturating_add(dict_entries_charge(dict.len()));
                for (key, child) in dict.iter() {
                    *total = total
                        .saturating_add(u64::try_from(key.as_bytes().len()).unwrap_or(u64::MAX));
                    stack.try_push(child)?;
                }
            }
            Value::Stream(stream) => {
                *total = total
                    .saturating_add(u64::try_from(size_of::<Stream>()).unwrap_or(u64::MAX))
                    .saturating_add(dict_entries_charge(stream.dict().len()));
                for (key, child) in stream.dict().iter() {
                    *total = total
                        .saturating_add(u64::try_from(key.as_bytes().len()).unwrap_or(u64::MAX));
                    stack.try_push(child)?;
                }
            }
        }
    }
    Ok(())
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
