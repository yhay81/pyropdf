//! Lossless encoders for PDF stream data, applied in reverse filter order.

use super::{Bytes, Dict, Name, Stream, Value};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::{Error, Result};

const HEX: &[u8; 16] = b"0123456789ABCDEF";

/// Replace the data and filter metadata together. The filter array is ordered
/// as a reader decodes it; encoding starts with the innermost filter.
#[expect(
    clippy::disallowed_methods,
    reason = "at most two bounded metadata keys are inserted"
)]
pub fn write(stream: &mut Stream, data: &[u8], mut options: Dict, budget: &Budget) -> Result<()> {
    let filter = options.remove("Filter");
    let parameters = options.remove("DecodeParms");
    let filters = match &filter {
        None => &[][..],
        Some(name @ Value::Name(_)) => std::slice::from_ref(name),
        Some(Value::Array(array)) => array.as_slice(),
        _ => return Err(Error::malformed("invalid stream Filter")),
    };
    budget.check(LimitCode::StreamFilterCount, filters.len() as u64)?;
    let parms = match &parameters {
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
    let dict = stream.dict_mut();
    for (key, value) in [("Filter", filter), ("DecodeParms", parameters)] {
        if let Some(value) = value {
            dict.insert(Name::from(key), value);
        } else {
            dict.remove(key);
        }
    }
    stream.set_data(Bytes::from(bytes));
    Ok(())
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
    let mut out = BudgetVec::new(budget);
    match filter {
        b"ASCIIHexDecode" => {
            out.try_reserve_exact(data.len().saturating_mul(2).saturating_add(1))?;
            for chunk in data.chunks(4096) {
                budget.poll()?;
                for &byte in chunk {
                    out.try_extend_from_slice(&[
                        HEX[usize::from(byte >> 4)],
                        HEX[usize::from(byte & 15)],
                    ])?;
                }
            }
            out.try_push(b'>')?;
        }
        b"ASCII85Decode" => {
            for chunk in data.chunks(4096) {
                budget.poll()?;
                for group in chunk.chunks(4) {
                    let mut word = [0; 4];
                    word[..group.len()].copy_from_slice(group);
                    let mut value = u32::from_be_bytes(word);
                    if value == 0 && group.len() == 4 {
                        out.try_push(b'z')?;
                        continue;
                    }
                    let mut digits = [0; 5];
                    for digit in digits.iter_mut().rev() {
                        *digit = u8::try_from(value % 85).expect("ASCII85 digit") + b'!';
                        value /= 85;
                    }
                    out.try_extend_from_slice(&digits[..group.len() + 1])?;
                }
            }
            out.try_extend_from_slice(b"~>")?;
        }
        b"LZWDecode" => {
            let early = parameter(parameters, "EarlyChange", 1)?;
            if !matches!(early, 0 | 1) {
                return Err(Error::malformed("LZW EarlyChange must be 0 or 1"));
            }
            lzw(data, early == 1, &mut out, budget)?;
        }
        b"RunLengthDecode" => run_length(data, &mut out, budget)?,
        b"FlateDecode" => {
            return crate::save::deflate(data, 6, usize::MAX, budget)?
                .ok_or_else(|| Error::malformed("could not encode the Flate stream"));
        }
        _ => return Err(Error::malformed("unsupported stream encoder")),
    }
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
