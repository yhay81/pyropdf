//! Lossless encoders for PDF stream data, applied in reverse filter order.

use super::{Bytes, Dict, FilterLookup, Stream, Value};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::{Error, Result};

const HEX: &[u8; 16] = b"0123456789ABCDEF";

/// Replace the data and filter metadata together. The filter array is ordered
/// as a reader decodes it; encoding starts with the innermost filter.
pub fn write(stream: &mut Stream, data: &[u8], options: Dict, budget: &Budget) -> Result<()> {
    write_with_lookup(stream, data, options, &|_, _| Ok(None), budget)
}

/// Encode and replace a stream whose previous security declaration can be indirect.
pub fn write_with_lookup(
    stream: &mut Stream,
    data: &[u8],
    options: Dict,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
) -> Result<()> {
    let filter = options.get("Filter");
    let parameters = options.get("DecodeParms");
    let filters = match filter {
        None => &[][..],
        Some(name @ Value::Name(_)) => std::slice::from_ref(name),
        Some(Value::Array(array)) => array.as_slice(),
        _ => return Err(Error::malformed("invalid stream Filter")),
    };
    budget.check(LimitCode::StreamFilterCount, filters.len() as u64)?;
    let parms = match parameters {
        None => &[][..],
        Some(dict @ Value::Dict(_)) if matches!(filter, Some(Value::Name(_))) => {
            std::slice::from_ref(dict)
        }
        Some(Value::Array(array))
            if matches!(filter, Some(Value::Array(_)))
                && array.as_slice().len() == filters.len() =>
        {
            array.as_slice()
        }
        _ => {
            return Err(Error::malformed(
                "DecodeParms must match the filter or filter array",
            ));
        }
    };
    let mut encoded: Option<Vec<u8>> = None;
    for (i, name) in filters.iter().enumerate().rev() {
        let next = encode_filter(
            name,
            parms.get(i),
            encoded.as_deref().unwrap_or(data),
            budget,
        );
        if let Some(previous) = encoded.take() {
            // Each encoder transfers its output charge to us. Release the
            // previous stage even if the following stage refused its input.
            budget.release(LimitCode::MemorySize, previous.capacity() as u64);
        }
        encoded = Some(next?);
    }
    let bytes = if let Some(encoded) = encoded {
        encoded
    } else {
        let mut copy = BudgetVec::new(budget);
        copy.try_extend_from_slice(data)?;
        copy.into_vec()
    };
    let charge = bytes.capacity() as u64;
    let result = stream.replace_encoded_options_with(Bytes::from(bytes), options, lookup, budget);
    if result.is_err() {
        budget.release(LimitCode::MemorySize, charge);
    }
    result
}

/// Encode the input with the named general-purpose filter (ISO 32000-2, 7.4).
/// Image filters use the image engine's sample layout and encoders.
fn encode_filter(
    filter: &Value,
    parameters: Option<&Value>,
    data: &[u8],
    budget: &Budget,
) -> Result<Vec<u8>> {
    budget.poll()?;
    let Value::Name(name) = filter else {
        return Err(Error::malformed("expected a filter name"));
    };
    let filter = name.as_bytes();
    let parameters = match parameters {
        None | Some(Value::Null) => None,
        Some(Value::Dict(dict)) => Some(dict),
        _ => return Err(Error::malformed("expected a parameter dictionary or null")),
    };
    let predicted = if matches!(filter, b"FlateDecode" | b"LZWDecode") {
        predict(data, parameters, budget)?
    } else {
        None
    };
    let data = predicted.as_ref().map_or(data, BudgetVec::as_slice);
    match filter {
        b"ASCIIHexDecode" => ascii_hex(data, None, budget),
        b"ASCII85Decode" => ascii85(data, None, budget),
        b"LZWDecode" => {
            let early = parameter(parameters, "EarlyChange", 1)?;
            if !matches!(early, 0 | 1) {
                return Err(Error::malformed("LZW EarlyChange must be 0 or 1"));
            }
            let mut out = BudgetVec::new(budget);
            lzw(data, early == 1, &mut out, budget)?;
            Ok(out.into_vec())
        }
        b"RunLengthDecode" => run_length_bytes(data, budget),
        b"FlateDecode" => crate::save::deflate(data, 6, usize::MAX, budget)?
            .ok_or_else(|| Error::malformed("could not encode the Flate stream")),
        _ => Err(Error::malformed("unsupported stream encoder")),
    }
}

/// Encodes `data` with `ASCIIHexDecode` (ISO 32000-2 section 7.4.2), optionally
/// wrapping lines at `wrap_columns` bytes.
pub(crate) fn ascii_hex(
    data: &[u8],
    wrap_columns: Option<usize>,
    budget: &Budget,
) -> Result<Vec<u8>> {
    let mut out = BudgetVec::new(budget);
    let raw_len = data.len().saturating_mul(2).saturating_add(1);
    let newlines = wrap_columns.map_or(0, |cols| {
        if cols >= 2 {
            raw_len / cols + 1
        } else {
            raw_len
        }
    });
    out.try_reserve_exact(raw_len.saturating_add(newlines))?;
    let mut col = 0usize;
    for chunk in data.chunks(4096) {
        budget.poll()?;
        for &byte in chunk {
            if let Some(cols) = wrap_columns
                && col > 0
                && col + 2 > cols
            {
                out.try_push(b'\n')?;
                col = 0;
            }
            out.try_extend_from_slice(&[HEX[usize::from(byte >> 4)], HEX[usize::from(byte & 15)]])?;
            col += 2;
        }
    }
    if let Some(cols) = wrap_columns
        && col > 0
        && col + 1 > cols
    {
        out.try_push(b'\n')?;
    }
    out.try_push(b'>')?;
    Ok(out.into_vec())
}

/// Encodes `data` with `ASCII85Decode` (ISO 32000-2 section 7.4.3), optionally
/// wrapping lines at `wrap_columns` bytes without splitting the `~>` marker.
pub(crate) fn ascii85(
    data: &[u8],
    wrap_columns: Option<usize>,
    budget: &Budget,
) -> Result<Vec<u8>> {
    let mut out = BudgetVec::new(budget);
    let mut col = 0usize;
    for chunk in data.chunks(4096) {
        budget.poll()?;
        for group in chunk.chunks(4) {
            let mut word = [0; 4];
            word[..group.len()].copy_from_slice(group);
            let mut value = u32::from_be_bytes(word);
            let mut digits = [0u8; 5];
            let token: &[u8] = if value == 0 && group.len() == 4 {
                b"z"
            } else {
                for digit in digits.iter_mut().rev() {
                    *digit = u8::try_from(value % 85).expect("ASCII85 digit") + b'!';
                    value /= 85;
                }
                &digits[..group.len() + 1]
            };
            if let Some(cols) = wrap_columns
                && col > 0
                && col + token.len() > cols
            {
                out.try_push(b'\n')?;
                col = 0;
            }
            out.try_extend_from_slice(token)?;
            col += token.len();
        }
    }
    if let Some(cols) = wrap_columns
        && col > 0
        && col + 2 > cols
    {
        out.try_push(b'\n')?;
    }
    out.try_extend_from_slice(b"~>")?;
    Ok(out.into_vec())
}

/// Encodes `data` with `RunLengthDecode` (ISO 32000-2 section 7.4.5).
pub(crate) fn run_length_bytes(data: &[u8], budget: &Budget) -> Result<Vec<u8>> {
    let mut out = BudgetVec::new(budget);
    run_length(data, &mut out, budget)?;
    Ok(out.into_vec())
}

fn repeated(data: &[u8]) -> bool {
    matches!(data, [a, b, c, ..] if a == b && b == c)
}

fn run_length(data: &[u8], out: &mut BudgetVec<'_, u8>, budget: &Budget) -> Result<()> {
    let mut at = 0;
    while at < data.len() {
        budget.poll()?;
        let start = at;
        let window = &data[start..][..(data.len() - start).min(128)];
        if repeated(window) {
            let count = window.iter().take_while(|&&b| b == window[0]).count();
            out.try_extend_from_slice(&[
                u8::try_from(257 - count).expect("3 to 128 repeated bytes"),
                window[0],
            ])?;
            at += count;
        } else {
            at += 1;
            while at - start < window.len() && !repeated(&data[at..]) {
                at += 1;
            }
            out.try_push(u8::try_from(at - start - 1).expect("1 to 128 literal bytes"))?;
            out.try_extend_from_slice(&data[start..at])?;
        }
    }
    out.try_push(128)?;
    Ok(())
}

fn parameter(parameters: Option<&Dict>, key: &str, default: i64) -> Result<i64> {
    match parameters.and_then(|dict| dict.get(key)) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Integer(value)) => Ok(*value),
        _ => Err(Error::malformed(format!("{key} must be an integer"))),
    }
}

fn lzw(data: &[u8], early: bool, out: &mut BudgetVec<'_, u8>, budget: &Budget) -> Result<()> {
    use weezl::{BitOrder, LzwStatus, encode::Encoder};

    // The 12-bit dictionary contains at most one new entry per input byte.
    // In weezl 0.1 the sparse nodes cost at most 50 bytes per code; a 512-byte
    // dense node needs at least 17 successor codes. Include Vec capacity
    // doubling, the initial 258 code keys, and the dense root in the charge.
    let bytes = 4096 + 192 * u64::try_from(data.len().min(4096)).unwrap_or(4096);
    budget.charge(LimitCode::MemorySize, bytes)?;
    let result = (|| {
        let mut encoder = if early {
            Encoder::with_tiff_size_switch(BitOrder::Msb, 8)
        } else {
            Encoder::new(BitOrder::Msb, 8)
        };
        let mut at = 0usize;
        let mut buffer = [0; 4096];
        loop {
            budget.poll()?;
            let end = data.len().min(at.saturating_add(buffer.len()));
            if at == end {
                encoder.finish();
            }
            let step = encoder.encode_bytes(&data[at..end], &mut buffer);
            at += step.consumed_in;
            out.try_extend_from_slice(&buffer[..step.consumed_out])?;
            match step.status {
                Ok(LzwStatus::Done) => return Ok(()),
                Ok(LzwStatus::Ok) => {}
                _ => return Err(Error::malformed("LZW encoder made no progress")),
            }
        }
    })();
    budget.release(LimitCode::MemorySize, bytes);
    result
}

/// Encode the TIFF or PNG predictor rows before LZW/Flate compression
/// (ISO 32000-2, 7.4.4.3 and Table 8). Only the last byte may have padding.
fn predict<'b>(
    data: &[u8],
    parameters: Option<&Dict>,
    budget: &'b Budget,
) -> Result<Option<BudgetVec<'b, u8>>> {
    let predictor = parameter(parameters, "Predictor", 1)?;
    if predictor == 1 {
        return Ok(None);
    }
    if predictor != 2 && !(10..=15).contains(&predictor) {
        return Err(Error::malformed("Predictor must be 1, 2, or 10 through 15"));
    }
    let colors = usize::try_from(parameter(parameters, "Colors", 1)?)
        .ok()
        .filter(|&value| value > 0);
    let columns = usize::try_from(parameter(parameters, "Columns", 1)?)
        .ok()
        .filter(|&value| value > 0);
    let bits = parameter(parameters, "BitsPerComponent", 8)?;
    let (Some(colors), Some(columns), 1 | 2 | 4 | 8 | 16) = (colors, columns, bits) else {
        return Err(Error::malformed(
            "invalid predictor Colors, Columns or BitsPerComponent",
        ));
    };
    let bits = usize::try_from(bits).expect("validated sample depth");
    let samples = colors
        .checked_mul(columns)
        .ok_or_else(|| Error::malformed("predictor row too large"))?;
    let row_bits = samples
        .checked_mul(bits)
        .ok_or_else(|| Error::malformed("predictor row too large"))?;
    let pixel_bits = colors * bits; // columns is positive and row_bits fits
    // The project's current hayro-syntax pin uses a u8 for pixel width and
    // loses the next TIFF row's boundary after partially filled bytes (#982).
    if pixel_bits > usize::from(u8::MAX) || (predictor == 2 && !row_bits.is_multiple_of(8)) {
        return Err(Error::malformed(
            "the current decoder cannot round-trip this predictor row layout",
        ));
    }
    let width = row_bits.div_ceil(8);
    if data.is_empty() {
        budget.check(LimitCode::StreamDataSize, width as u64)?;
        budget.check(LimitCode::MemorySize, width as u64)?;
    }
    if !data.len().is_multiple_of(width) {
        return Err(Error::malformed(
            "decoded data does not contain complete predictor rows",
        ));
    }
    let mut out = BudgetVec::new(budget);
    if predictor == 2 {
        out.try_extend_from_slice(data)?;
        for (source, dest) in data
            .chunks_exact(width)
            .zip(out.as_mut_slice().chunks_exact_mut(width))
        {
            for sample in colors..samples {
                if sample.is_multiple_of(4096) {
                    budget.poll()?;
                }
                let left = read_sample(source, sample - colors, bits);
                let current = read_sample(source, sample, bits);
                write_sample(dest, sample, bits, current.wrapping_sub(left));
            }
            budget.poll()?;
        }
    } else {
        // Each PNG row carries the actual filter tag (7.4.4.4). The pin
        // mishandles a filtered final partial pixel, so use None for those
        // rows. This retains every input byte, including padding.
        let stride = pixel_bits.div_ceil(8);
        let tag = if predictor == 15 || !width.is_multiple_of(stride) {
            0
        } else {
            u8::try_from(predictor - 10).expect("PNG filter")
        };
        let mut previous: &[u8] = &[];
        for row in data.chunks_exact(width) {
            budget.poll()?;
            out.try_push(tag)?;
            for (x, &value) in row.iter().enumerate() {
                if x.is_multiple_of(4096) {
                    budget.poll()?;
                }
                let left = x.checked_sub(stride).map_or(0, |i| row[i]);
                let up = previous.get(x).copied().unwrap_or(0);
                let last = x
                    .checked_sub(stride)
                    .and_then(|i| previous.get(i))
                    .copied()
                    .unwrap_or(0);
                let prediction = match tag {
                    1 => left,
                    2 => up,
                    3 => u8::midpoint(left, up),
                    4 => paeth(left, up, last),
                    _ => 0,
                };
                out.try_push(value.wrapping_sub(prediction))?;
            }
            previous = row;
        }
    }
    Ok(Some(out))
}

fn read_sample(row: &[u8], i: usize, bits: usize) -> u16 {
    if bits == 16 {
        return u16::from_be_bytes([row[i * 2], row[i * 2 + 1]]);
    }
    let bit = i * bits;
    u16::from(row[bit / 8] >> (8 - bits - bit % 8)) & ((1 << bits) - 1)
}

fn write_sample(row: &mut [u8], i: usize, bits: usize, value: u16) {
    if bits == 16 {
        row[i * 2..i * 2 + 2].copy_from_slice(&value.to_be_bytes());
    } else {
        let bit = i * bits;
        let shift = 8 - bits - bit % 8;
        let mask = u8::MAX >> (8 - bits);
        row[bit / 8] =
            (row[bit / 8] & !(mask << shift)) | ((value.to_le_bytes()[0] & mask) << shift);
    }
}

fn paeth(left: u8, up: u8, last: u8) -> u8 {
    let prediction = i16::from(left) + i16::from(up) - i16::from(last);
    let a = prediction.abs_diff(i16::from(left));
    let b = prediction.abs_diff(i16::from(up));
    let c = prediction.abs_diff(i16::from(last));
    if a <= b && a <= c {
        left
    } else if b <= c {
        up
    } else {
        last
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::value::Name;

    #[test]
    fn a_refused_chain_releases_its_intermediate_output_charge() {
        let budget = Budget::default();
        let filters = ["UnknownFilter", "ASCII85Decode"]
            .into_iter()
            .map(|name| Value::Name(Name::from(name)))
            .collect();
        let options = [(Name::from("Filter"), Value::Array(filters))]
            .into_iter()
            .collect();
        let mut stream = Stream::new(Dict::default(), Bytes::default());
        assert!(write(&mut stream, &[97u8; 1024], options, &budget).is_err());
        assert_eq!(budget.used(LimitCode::MemorySize), 0);
        assert!(stream.data().is_empty());
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    /// `write_sample` followed by `read_sample` recovers the low `bits` of
    /// `value` for every valid TIFF predictor depth `{1, 2, 4, 8, 16}` without
    /// panicking or corrupting neighboring samples in the row.
    #[kani::proof]
    fn kani_predictor_sample_bit_packing_roundtrip() {
        let mut row: [u8; 4] = kani::any();
        let depth_idx: u8 = kani::any();
        kani::assume(depth_idx < 5);
        let bits: usize = match depth_idx {
            0 => 1,
            1 => 2,
            2 => 4,
            3 => 8,
            _ => 16,
        };
        let samples = (row.len() * 8) / bits;
        let i: usize = kani::any();
        let other: usize = kani::any();
        kani::assume(i < samples && other < samples && other != i);
        let value: u16 = kani::any();

        let other_before = read_sample(&row, other, bits);
        write_sample(&mut row, i, bits, value);
        let mask: u16 = if bits == 16 {
            u16::MAX
        } else {
            (1_u16 << bits) - 1
        };
        assert_eq!(read_sample(&row, i, bits), value & mask);
        assert_eq!(read_sample(&row, other, bits), other_before);
    }

    /// `paeth` never overflows `i16`, always returns one of `{left, up, last}`,
    /// and selects the neighbor closest to `left + up - last` with RFC 2083
    /// tie-breaking.
    #[kani::proof]
    fn kani_predictor_paeth_exact() {
        let left: u8 = kani::any();
        let up: u8 = kani::any();
        let last: u8 = kani::any();
        let chosen = paeth(left, up, last);
        let p = i32::from(left) + i32::from(up) - i32::from(last);
        let da = (p - i32::from(left)).unsigned_abs();
        let db = (p - i32::from(up)).unsigned_abs();
        let dc = (p - i32::from(last)).unsigned_abs();
        let min_d = da.min(db).min(dc);
        let chosen_d = (p - i32::from(chosen)).unsigned_abs();
        assert_eq!(chosen_d, min_d);
        assert!(chosen == left || chosen == up || chosen == last);
    }
}
