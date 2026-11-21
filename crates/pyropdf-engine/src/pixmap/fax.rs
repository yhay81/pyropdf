//! Lossless 1-bit image output for PDF. Group 3 and Group 4 fax use the
//! default BlackIs1=false. Embedded JBIG2 uses an MMR generic region.

use super::MAX_PIXELS;

/// The terminating codes of white runs of 0 through 63 pixels (T.4 table 2).
const WHITE_TERMINATING: [&str; 64] = [
    "00110101", "000111", "0111", "1000", "1011", "1100", "1110", "1111", "10011", "10100",
    "00111", "01000", "001000", "000011", "110100", "110101", "101010", "101011", "0100111",
    "0001100", "0001000", "0010111", "0000011", "0000100", "0101000", "0101011", "0010011",
    "0100100", "0011000", "00000010", "00000011", "00011010", "00011011", "00010010", "00010011",
    "00010100", "00010101", "00010110", "00010111", "00101000", "00101001", "00101010", "00101011",
    "00101100", "00101101", "00000100", "00000101", "00001010", "00001011", "01010010", "01010011",
    "01010100", "01010101", "00100100", "00100101", "01011000", "01011001", "01011010", "01011011",
    "01001010", "01001011", "00110010", "00110011", "00110100",
];

/// The terminating codes of black runs of 0 through 63 pixels.
const BLACK_TERMINATING: [&str; 64] = [
    "0000110111",
    "010",
    "11",
    "10",
    "011",
    "0011",
    "0010",
    "00011",
    "000101",
    "000100",
    "0000100",
    "0000101",
    "0000111",
    "00000100",
    "00000111",
    "000011000",
    "0000010111",
    "0000011000",
    "0000001000",
    "00001100111",
    "00001101000",
    "00001101100",
    "00000110111",
    "00000101000",
    "00000010111",
    "00000011000",
    "000011001010",
    "000011001011",
    "000011001100",
    "000011001101",
    "000001101000",
    "000001101001",
    "000001101010",
    "000001101011",
    "000011010010",
    "000011010011",
    "000011010100",
    "000011010101",
    "000011010110",
    "000011010111",
    "000001101100",
    "000001101101",
    "000011011010",
    "000011011011",
    "000001010100",
    "000001010101",
    "000001010110",
    "000001010111",
    "000001100100",
    "000001100101",
    "000001010010",
    "000001010011",
    "000000100100",
    "000000110111",
    "000000111000",
    "000000100111",
    "000000101000",
    "000001011000",
    "000001011001",
    "000000101011",
    "000000101100",
    "000001011010",
    "000001100110",
    "000001100111",
];

/// The make-up codes of white runs of 64 through 1728 pixels, by 64 (T.4 table 3).
const WHITE_MAKEUP: [&str; 27] = [
    "11011",
    "10010",
    "010111",
    "0110111",
    "00110110",
    "00110111",
    "01100100",
    "01100101",
    "01101000",
    "01100111",
    "011001100",
    "011001101",
    "011010010",
    "011010011",
    "011010100",
    "011010101",
    "011010110",
    "011010111",
    "011011000",
    "011011001",
    "011011010",
    "011011011",
    "010011000",
    "010011001",
    "010011010",
    "011000",
    "010011011",
];

/// The make-up codes of black runs of 64 through 1728 pixels, by 64.
const BLACK_MAKEUP: [&str; 27] = [
    "0000001111",
    "000011001000",
    "000011001001",
    "000001011011",
    "000000110011",
    "000000110100",
    "000000110101",
    "0000001101100",
    "0000001101101",
    "0000001001010",
    "0000001001011",
    "0000001001100",
    "0000001001101",
    "0000001110010",
    "0000001110011",
    "0000001110100",
    "0000001110101",
    "0000001110110",
    "0000001110111",
    "0000001010010",
    "0000001010011",
    "0000001010100",
    "0000001010101",
    "0000001011010",
    "0000001011011",
    "0000001100100",
    "0000001100101",
];

/// The make-up codes of runs of 1792 through 2560 pixels of either color,
/// by 64 (T.4 table 3, extended).
const EXTENDED_MAKEUP: [&str; 13] = [
    "00000001000",
    "00000001100",
    "00000001101",
    "000000010010",
    "000000010011",
    "000000010100",
    "000000010101",
    "000000010110",
    "000000010111",
    "000000011100",
    "000000011101",
    "000000011110",
    "000000011111",
];

/// The vertical mode codes for `a1 - b1` of -3 through 3 (T.6 table 1).
const VERTICAL: [&str; 7] = ["0000010", "000010", "010", "1", "011", "000011", "0000011"];

const PASS: &str = "0001";
const HORIZONTAL: &str = "001";
const EOL: &str = "000000000001";

/// Bits written most significant first, which stop growing past `limit` bytes.
struct Bits {
    out: Vec<u8>,
    byte: u8,
    used: u8,
    limit: usize,
}

impl Bits {
    #[expect(
        clippy::disallowed_methods,
        reason = "encode_line checks the output limit after each bounded code/run; a row has at most MAX_PIXELS pixels"
    )]
    fn put(&mut self, code: &str) {
        for bit in code.bytes() {
            self.byte = (self.byte << 1) | u8::from(bit == b'1');
            self.used += 1;
            if self.used == 8 {
                self.out.push(self.byte);
                self.byte = 0;
                self.used = 0;
            }
        }
    }

    fn over(&self) -> bool {
        self.out.len() > self.limit
    }

    #[expect(
        clippy::disallowed_methods,
        reason = "at most one partial byte is appended after the encoder checks its output limit"
    )]
    fn finish(mut self) -> Vec<u8> {
        if self.used > 0 {
            self.out.push(self.byte << (8 - self.used));
        }
        self.out
    }

    /// Writes a run of `length` pixels of one color: make-up codes, then a
    /// terminating code.
    fn run(&mut self, mut length: usize, white: bool) {
        while length >= 2560 {
            if self.over() {
                return;
            }
            self.put(EXTENDED_MAKEUP[12]);
            length -= 2560;
        }
        if length >= 64 {
            let index = length / 64 - 1;
            let code = if index >= 27 {
                EXTENDED_MAKEUP[index - 27]
            } else if white {
                WHITE_MAKEUP[index]
            } else {
                BLACK_MAKEUP[index]
            };
            self.put(code);
            length %= 64;
        }
        self.put(if white {
            WHITE_TERMINATING[length]
        } else {
            BLACK_TERMINATING[length]
        });
    }
}

/// Returns the first position after `from` (from 0 when `from` is `None`)
/// whose pixel is `white`, or the line's length. Positions before the line
/// are white.
fn next_color(line: &[bool], from: Option<usize>, white: bool) -> usize {
    let start = from.map_or(0, |at| at + 1);
    line.iter()
        .skip(start)
        .position(|&pixel| pixel == white)
        .map_or(line.len(), |offset| start + offset)
}

/// Returns the next changing element after `at`: the first position past
/// it whose pixel is `white`, or the line's length when `at` is its end.
fn after(line: &[bool], at: usize, white: bool) -> usize {
    if at >= line.len() {
        line.len()
    } else {
        next_color(line, Some(at), white)
    }
}

/// Returns `b1` of T.4 4.2.1.3.1: the first changing element of the
/// reference line after `a0` whose color is the opposite of `white`.
fn changing_element(reference: &[bool], a0: Option<usize>, white: bool) -> usize {
    let mut at = a0.map_or(0, |at| at + 1);
    while at < reference.len() {
        let previous = if at == 0 { true } else { reference[at - 1] };
        if reference[at] != white && previous == white {
            return at;
        }
        at += 1;
    }
    reference.len()
}

/// Encodes one coding line against its reference line (T.4 4.2.1.3.4).
fn encode_line(bits: &mut Bits, line: &[bool], reference: &[bool]) {
    let width = line.len();
    let mut a0: Option<usize> = None;
    let mut white = true;
    loop {
        let a1 = next_color(line, a0, !white);
        let b1 = changing_element(reference, a0, white);
        let b2 = after(reference, b1, white);
        if b2 < a1 {
            bits.put(PASS);
            a0 = Some(b2);
        } else if a1.abs_diff(b1) <= 3 {
            bits.put(VERTICAL[(a1 as isize - b1 as isize + 3) as usize]);
            a0 = Some(a1);
            white = !white;
        } else {
            let a2 = after(line, a1, white);
            bits.put(HORIZONTAL);
            bits.run(a1 - a0.unwrap_or(0), white);
            bits.run(a2 - a1, !white);
            a0 = Some(a2);
        }
        if a0.is_some_and(|at| at >= width) || bits.over() {
            return;
        }
    }
}

/// Returns 1-bit `data` (rows padded to whole bytes, 1 for white) as CCITT
/// Group 4 data, or `None` once the output would pass `limit` bytes.
#[must_use]
#[expect(
    clippy::disallowed_methods,
    reason = "two rows of width booleans; encode_checked checks width times height against MAX_PIXELS"
)]
pub fn encode_g4(data: &[u8], width: usize, height: usize, limit: usize) -> Option<Vec<u8>> {
    let stride = width.div_ceil(8);
    let mut bits = Bits {
        out: Vec::new(),
        byte: 0,
        used: 0,
        limit,
    };
    let mut reference = vec![true; width];
    let mut line = vec![true; width];
    for row in data.chunks(stride).take(height) {
        for (x, pixel) in line.iter_mut().enumerate() {
            *pixel = row[x / 8] & (0x80 >> (x % 8)) != 0;
        }
        encode_line(&mut bits, &line, &reference);
        if bits.over() {
            return None;
        }
        std::mem::swap(&mut line, &mut reference);
    }
    bits.put(EOL);
    bits.put(EOL);
    let out = bits.finish();
    (out.len() <= limit).then_some(out)
}

/// Writes a T.4 one-dimensional row, starting with a possibly empty white run.
fn encode_runs(bits: &mut Bits, row: &[bool]) {
    let mut start = 0;
    let mut white = true;
    for (x, &pixel) in row.iter().enumerate() {
        if pixel != white {
            bits.run(x - start, white);
            if bits.over() {
                return;
            }
            start = x;
            white = pixel;
        }
    }
    bits.run(row.len() - start, white);
}

/// Encodes Group 3 fax (T.4, 4.1 and 4.2). K=0 uses one-dimensional
/// rows. K=2 alternates one-dimensional rows and reference-coded rows.
#[expect(
    clippy::disallowed_methods,
    reason = "two rows bounded by the checked raster dimensions"
)]
pub fn encode_g3_checked(
    data: &[u8],
    width: usize,
    height: usize,
    limit: usize,
    mixed: bool,
) -> Result<Option<Vec<u8>>, String> {
    validate_rows(data, width, height)?;
    let mut bits = Bits {
        out: Vec::new(),
        byte: 0,
        used: 0,
        limit,
    };
    let mut line = vec![true; width];
    let mut reference = if mixed { vec![true; width] } else { Vec::new() };
    for (y, row) in data.chunks_exact(width.div_ceil(8)).enumerate() {
        bits.put(EOL);
        for (x, pixel) in line.iter_mut().enumerate() {
            *pixel = row[x / 8] & (0x80 >> (x % 8)) != 0;
        }
        if mixed && y % 2 == 1 {
            bits.put("0");
            encode_line(&mut bits, &line, &reference);
        } else {
            if mixed {
                bits.put("1");
            }
            encode_runs(&mut bits, &line);
        }
        if bits.over() {
            return Ok(None);
        }
        if mixed {
            std::mem::swap(&mut line, &mut reference);
        }
    }
    for _ in 0..6 {
        bits.put(EOL);
        if mixed {
            bits.put("1");
        }
    }
    let out = bits.finish();
    Ok((out.len() <= limit).then_some(out))
}

/// Encodes a lossless embedded JBIG2 page with one MMR generic region
/// (ITU-T T.88, 7.4.6 and 7.4.8; ISO 32000-2, 7.4.7). The image needs
/// neither a global symbol dictionary nor a file header or end-page segment.
#[expect(
    clippy::disallowed_methods,
    reason = "exactly the two fixed segment headers and at most limit bytes of MMR data"
)]
pub fn encode_jbig2_checked(
    data: &[u8],
    width: usize,
    height: usize,
    limit: usize,
) -> Result<Option<Vec<u8>>, String> {
    validate_rows(data, width, height)?;
    const OVERHEAD: usize = 11 + 19 + 11 + 18;
    let Some(payload_limit) = limit.checked_sub(OVERHEAD) else {
        return Ok(None);
    };
    let Some(mmr) = encode_g4(data, width, height, payload_limit) else {
        return Ok(None);
    };
    let width = u32::try_from(width).expect("validated pixel count");
    let height = u32::try_from(height).expect("validated pixel count");
    let length =
        u32::try_from(18 + mmr.len()).map_err(|_| "JBIG2 region length exceeds 32 bits")?;
    let mut bytes = Vec::with_capacity(OVERHEAD + mmr.len());
    // No referred-to segments; one-byte page association 1.
    bytes.extend_from_slice(&[0, 0, 0, 0, 48, 0, 1, 0, 0, 0, 19]);
    bytes.extend_from_slice(&width.to_be_bytes());
    bytes.extend_from_slice(&height.to_be_bytes());
    bytes.extend_from_slice(&[0; 8]); // unspecified resolution
    bytes.extend_from_slice(&[1, 0, 0]); // lossless, white background, non-striped
    bytes.extend_from_slice(&[0, 0, 0, 1, 39, 0, 1]); // immediate lossless generic region
    bytes.extend_from_slice(&length.to_be_bytes());
    bytes.extend_from_slice(&width.to_be_bytes());
    bytes.extend_from_slice(&height.to_be_bytes());
    bytes.extend_from_slice(&[0; 9]); // origin (0, 0), OR composition
    bytes.push(1); // MMR coding
    bytes.extend_from_slice(&mmr);
    Ok(Some(bytes))
}

fn validate_rows(data: &[u8], width: usize, height: usize) -> Result<(), String> {
    if width == 0
        || height == 0
        || (width as u64)
            .checked_mul(height as u64)
            .is_none_or(|pixels| pixels > MAX_PIXELS)
    {
        return Err(format!(
            "a {width} by {height} image is empty or has more than {MAX_PIXELS} pixels"
        ));
    }
    if data.len() != width.div_ceil(8) * height {
        return Err(format!(
            "{} bytes are not the samples of a {width} by {height} bitonal image",
            data.len()
        ));
    }
    Ok(())
}

/// Validates `width`, `height`, and `data.len()` against [`MAX_PIXELS`] and
/// packed row bytes before encoding 1-bit samples as CCITT Group 4 data.
pub fn encode_checked(
    data: &[u8],
    width: usize,
    height: usize,
    limit: usize,
) -> Result<Option<Vec<u8>>, String> {
    validate_rows(data, width, height)?;
    Ok(encode_g4(data, width, height, limit))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_white_line_is_one_vertical_code() {
        // a1 and b1 are both the end of the line: V0, then the EOFB.
        let out = encode_g4(&[0xFF], 8, 1, 100).unwrap();
        assert_eq!(out, [0b1000_0000, 0b0000_1000, 0b0000_0000, 0b1000_0000]);
    }

    #[test]
    fn codes_are_prefix_free_within_each_table() {
        for table in [&WHITE_TERMINATING[..], &WHITE_MAKEUP, &EXTENDED_MAKEUP] {
            for (i, a) in table.iter().enumerate() {
                for b in &table[i + 1..] {
                    assert!(!a.starts_with(b) && !b.starts_with(a), "{a} {b}");
                }
            }
        }
    }

    #[test]
    fn the_limit_stops_the_output() {
        // A checkerboard costs several bits per pixel.
        let data = vec![0b1010_1010; 64 * 64 / 8];
        assert!(encode_g4(&data, 64, 64, 16).is_none());
        assert!(encode_g4(&data, 64, 64, 1 << 20).is_some());
    }
}
