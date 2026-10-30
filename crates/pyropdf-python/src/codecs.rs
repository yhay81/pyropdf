//! The Pixmap codecs beyond PNG and JPEG (issue 431): decoding portable
//! anymaps (PBM, PGM, PPM, plain and raw), PAM, and uncompressed BMP, and
//! encoding PAM, PBM, PGM, PPM, PSD, and PostScript. Every decoder checks
//! the pixel count against [`pixel_ops::MAX_PIXELS`] and the input length
//! against the samples it promises before it allocates them.

use std::io::{self, Write};

use crate::pixel_ops::{self, Decoded, Layout};

/// The resolution of formats that state none.
const DEFAULT_RESOLUTION: (u32, u32) = (96, 96);

/// The longest side of a version 1 PSD file.
const MAX_PSD_SIDE: u32 = 30_000;

/// The ASCII85 characters on one line of PostScript output.
const ASCII85_LINE: usize = 75;

/// Scales `value` from `0..=max` to `0..=255`, rounding.
fn scale(value: u32, max: u32) -> u8 {
    ((u64::from(value) * 255 + u64::from(max) / 2) / u64::from(max)) as u8
}

/// The header fields of a portable anymap: decimal numbers separated by
/// whitespace, which `#` comments to the end of a line interrupt.
struct Fields<'a> {
    data: &'a [u8],
    at: usize,
}

impl Fields<'_> {
    fn skip(&mut self) {
        while let Some(&byte) = self.data.get(self.at) {
            if byte == b'#' {
                while self
                    .data
                    .get(self.at)
                    .is_some_and(|&byte| byte != b'\n' && byte != b'\r')
                {
                    self.at += 1;
                }
            } else if byte.is_ascii_whitespace() {
                self.at += 1;
            } else {
                break;
            }
        }
    }

    /// The next number, at most `max`.
    fn number(&mut self, max: u32) -> Result<u32, String> {
        self.skip();
        let start = self.at;
        let mut value = 0_u32;
        while let Some(&byte) = self.data.get(self.at).filter(|byte| byte.is_ascii_digit()) {
            value = value
                .checked_mul(10)
                .and_then(|value| value.checked_add(u32::from(byte - b'0')))
                .filter(|&value| value <= max)
                .ok_or_else(|| format!("a portable anymap number passes {max}"))?;
            self.at += 1;
        }
        if self.at == start {
            return Err("the portable anymap is missing a number".to_owned());
        }
        Ok(value)
    }

    /// The single whitespace byte that ends a raw header.
    fn end_header(&mut self) -> Result<&[u8], String> {
        if !self.data.get(self.at).is_some_and(u8::is_ascii_whitespace) {
            return Err("the portable anymap header does not end in whitespace".to_owned());
        }
        Ok(&self.data[self.at + 1..])
    }
}

/// Whether `data` starts like a portable anymap or PAM.
pub fn is_pnm(data: &[u8]) -> bool {
    data.len() > 2
        && data[0] == b'P'
        && (b'1'..=b'7').contains(&data[1])
        && data[2].is_ascii_whitespace()
}

/// Decodes a PBM, PGM, PPM, or PAM image, plain or raw, at any maximum
/// value up to 65535, into 8-bit gray or RGB samples.
pub fn decode_pnm(data: &[u8]) -> Result<Decoded, String> {
    let kind = data[1];
    if kind == b'7' {
        return decode_pam(data);
    }
    let mut fields = Fields { data, at: 2 };
    let width = fields.number(u32::MAX)?;
    let height = fields.number(u32::MAX)?;
    pixel_ops::check_size(width, height)?;
    let bitmap = matches!(kind, b'1' | b'4');
    let max = if bitmap { 1 } else { fields.number(65535)? };
    if max == 0 {
        return Err("the portable anymap's maximum value is 0".to_owned());
    }
    let colors = if matches!(kind, b'3' | b'6') { 3 } else { 1 };
    let layout = Layout {
        width,
        height,
        gray: colors == 1,
        alpha: false,
    };
    let count = width as usize * height as usize * colors;
    let truncated = || "the portable anymap ends before its samples".to_owned();
    let samples = match kind {
        b'1' => {
            // Plain PBM needs no whitespace between its digits.
            if data.len() - fields.at < count {
                return Err(truncated());
            }
            let mut samples = Vec::with_capacity(count);
            let mut bytes = data[fields.at..].iter();
            while samples.len() < count {
                match bytes.next() {
                    Some(b'0') => samples.push(255),
                    Some(b'1') => samples.push(0),
                    Some(b'#') => {
                        for &byte in bytes.by_ref() {
                            if byte == b'\n' || byte == b'\r' {
                                break;
                            }
                        }
                    }
                    Some(byte) if byte.is_ascii_whitespace() => {}
                    Some(_) => return Err("a plain PBM sample is neither 0 nor 1".to_owned()),
                    None => return Err(truncated()),
                }
            }
            samples
        }
        b'2' | b'3' => {
            // Each sample takes a digit and a separator but the last.
            if data.len() - fields.at < count * 2 - 1 {
                return Err(truncated());
            }
            let mut samples = Vec::with_capacity(count);
            for _ in 0..count {
                let value = fields.number(max).map_err(|_| truncated())?;
                samples.push(scale(value, max));
            }
            samples
        }
        b'4' => {
            let rest = fields.end_header()?;
            let row = (width as usize).div_ceil(8);
            if rest.len() < row * height as usize {
                return Err(truncated());
            }
            let mut samples = Vec::with_capacity(count);
            for line in rest.chunks_exact(row).take(height as usize) {
                samples.extend((0..width as usize).map(|x| {
                    if line[x / 8] & (0x80 >> (x % 8)) == 0 {
                        255
                    } else {
                        0
                    }
                }));
            }
            samples
        }
        _ => raw_samples(fields.end_header()?, count, max)?,
    };
    Ok(Decoded {
        layout,
        data: samples,
        resolution: DEFAULT_RESOLUTION,
    })
}

/// Reads `count` raw samples, one byte each below a maximum of 256 and two
/// big-endian bytes from it on.
fn raw_samples(rest: &[u8], count: usize, max: u32) -> Result<Vec<u8>, String> {
    let width = if max < 256 { 1 } else { 2 };
    if rest.len() < count * width {
        return Err("the image ends before its samples".to_owned());
    }
    let mut samples = Vec::with_capacity(count);
    for sample in rest.chunks_exact(width).take(count) {
        let value = sample
            .iter()
            .fold(0, |value, &byte| (value << 8) | u32::from(byte));
        if value > max {
            return Err(format!("a sample passes the maximum value {max}"));
        }
        samples.push(scale(value, max));
    }
    Ok(samples)
}

/// Decodes a PAM image of tuple type `BLACKANDWHITE`, `GRAYSCALE`, or
/// `RGB`, each with or without `_ALPHA`, or of no tuple type and a depth
/// from 1 through 4. CMYK is refused.
fn decode_pam(data: &[u8]) -> Result<Decoded, String> {
    let (mut width, mut height, mut depth, mut max) = (None, None, None, None);
    let mut tuple: &[u8] = b"";
    let mut at = 3;
    loop {
        let end = data[at..]
            .iter()
            .position(|&byte| byte == b'\n')
            .ok_or_else(|| "the PAM header has no ENDHDR line".to_owned())?;
        let line = data[at..at + end].trim_ascii();
        at += end + 1;
        if line.is_empty() || line.starts_with(b"#") {
            continue;
        }
        if line == b"ENDHDR" {
            break;
        }
        let split = line
            .iter()
            .position(u8::is_ascii_whitespace)
            .unwrap_or(line.len());
        let (token, value) = (&line[..split], line[split..].trim_ascii());
        let number = || {
            let mut fields = Fields { data: value, at: 0 };
            fields.number(u32::MAX)
        };
        match token {
            b"WIDTH" => width = Some(number()?),
            b"HEIGHT" => height = Some(number()?),
            b"DEPTH" => depth = Some(number()?),
            b"MAXVAL" => max = Some(number()?),
            b"TUPLTYPE" => tuple = value,
            _ => return Err("the PAM header has an unknown line".to_owned()),
        }
    }
    let missing = || "the PAM header lacks WIDTH, HEIGHT, DEPTH, or MAXVAL".to_owned();
    let (width, height) = (width.ok_or_else(missing)?, height.ok_or_else(missing)?);
    let (depth, max) = (depth.ok_or_else(missing)?, max.ok_or_else(missing)?);
    pixel_ops::check_size(width, height)?;
    if !(1..=65535).contains(&max) {
        return Err("the PAM maximum value is outside 1 through 65535".to_owned());
    }
    let (gray, alpha) = match (tuple, depth) {
        (b"BLACKANDWHITE" | b"GRAYSCALE", 1) | (b"", 1) => (true, false),
        (b"BLACKANDWHITE_ALPHA" | b"GRAYSCALE_ALPHA", 2) | (b"", 2) => (true, true),
        (b"RGB", 3) | (b"", 3) => (false, false),
        (b"RGB_ALPHA", 4) | (b"", 4) => (false, true),
        (b"CMYK" | b"CMYK_ALPHA", _) => {
            return Err("CMYK images are not supported; a pixmap holds gray or RGB".to_owned());
        }
        _ => return Err("the PAM tuple type and depth describe no gray or RGB image".to_owned()),
    };
    let layout = Layout {
        width,
        height,
        gray,
        alpha,
    };
    let count = width as usize * height as usize * layout.channels();
    Ok(Decoded {
        layout,
        data: raw_samples(&data[at..], count, max)?,
        resolution: DEFAULT_RESOLUTION,
    })
}

fn u16_at(data: &[u8], at: usize) -> Result<u16, String> {
    data.get(at..at + 2)
        .map(|bytes| u16::from_le_bytes([bytes[0], bytes[1]]))
        .ok_or_else(|| "the BMP header is truncated".to_owned())
}

fn u32_at(data: &[u8], at: usize) -> Result<u32, String> {
    data.get(at..at + 4)
        .map(|bytes| u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
        .ok_or_else(|| "the BMP header is truncated".to_owned())
}

/// A BMP bit field: where a channel lies in a pixel, and its largest value.
#[derive(Clone, Copy)]
struct Field {
    shift: u32,
    max: u32,
}

impl Field {
    fn new(mask: u32) -> Result<Option<Self>, String> {
        if mask == 0 {
            return Ok(None);
        }
        let shift = mask.trailing_zeros();
        let max = mask >> shift;
        if max & max.wrapping_add(1) != 0 {
            return Err("a BMP bit field is not contiguous".to_owned());
        }
        Ok(Some(Self { shift, max }))
    }

    fn read(self, pixel: u32) -> u8 {
        scale((pixel >> self.shift) & self.max, self.max)
    }
}

/// Decodes an uncompressed BMP: 1, 4, or 8 bits through a palette, which
/// decodes to gray when every entry is gray, 24-bit BGR, and 16 or 32 bits
/// in the default or stated bit fields, with alpha when a field holds it.
/// Run-length and embedded JPEG or PNG data is refused.
pub fn decode_bmp(data: &[u8]) -> Result<Decoded, String> {
    let offset = u32_at(data, 10)? as usize;
    let header = u32_at(data, 14)? as usize;
    let core = header == 12;
    let (width, height, bits, compression, resolution) = if core {
        let width = i64::from(u16_at(data, 18)?);
        let height = i64::from(u16_at(data, 20)?);
        (width, height, u16_at(data, 24)?, 0, DEFAULT_RESOLUTION)
    } else if header >= 40 {
        let width = i64::from(u32_at(data, 18)? as i32);
        let height = i64::from(u32_at(data, 22)? as i32);
        let dpi = |per_metre: u32| match per_metre as i32 {
            ..=0 => 96,
            per_metre => ((f64::from(per_metre) * 0.0254).round() as u32).clamp(1, 65535),
        };
        let resolution = (dpi(u32_at(data, 38)?), dpi(u32_at(data, 42)?));
        (
            width,
            height,
            u16_at(data, 28)?,
            u32_at(data, 30)?,
            resolution,
        )
    } else {
        return Err(format!("the BMP header of {header} bytes is not supported"));
    };
    let top_down = height < 0;
    let (width, height) = (
        u32::try_from(width).map_err(|_| "the BMP width is negative".to_owned())?,
        u32::try_from(height.unsigned_abs())
            .map_err(|_| "the BMP height is too large".to_owned())?,
    );
    pixel_ops::check_size(width, height)?;
    let fields = match (compression, bits) {
        (0, 1 | 4 | 8 | 24) => None,
        (0, 16) => Some([0x7c00, 0x03e0, 0x001f, 0]),
        (0, 32) => Some([0x00ff_0000, 0x0000_ff00, 0x0000_00ff, 0]),
        (3 | 6, 16 | 32) => {
            // A 40-byte header puts the masks after itself; a longer one holds them.
            let alpha = compression == 6 || header >= 56;
            Some([
                u32_at(data, 54)?,
                u32_at(data, 58)?,
                u32_at(data, 62)?,
                if alpha { u32_at(data, 66)? } else { 0 },
            ])
        }
        (1 | 2, _) => return Err("run-length encoded BMP is not supported".to_owned()),
        (4 | 5, _) => return Err("BMP with embedded JPEG or PNG data is not supported".to_owned()),
        _ => {
            return Err(format!(
                "BMP of {bits} bits and compression {compression} is not supported"
            ));
        }
    };
    let stride = (u64::from(bits) * u64::from(width)).div_ceil(32) * 4;
    let rows = data
        .get(offset..)
        .filter(|rows| rows.len() as u64 >= stride * u64::from(height))
        .ok_or_else(|| "the BMP ends before its pixels".to_owned())?;
    let stride = stride as usize;
    let row = |y: u32| {
        let line = if top_down { y } else { height - 1 - y } as usize;
        &rows[line * stride..(line + 1) * stride]
    };
    let (width_us, height_us) = (width as usize, height as usize);
    if let Some(masks) = fields {
        let [red, green, blue] = [masks[0], masks[1], masks[2]].map(Field::new);
        let (red, green, blue) = (red?, green?, blue?);
        let alpha = Field::new(masks[3])?;
        let (Some(red), Some(green), Some(blue)) = (red, green, blue) else {
            return Err("a BMP color bit field is empty".to_owned());
        };
        let layout = Layout {
            width,
            height,
            gray: false,
            alpha: alpha.is_some(),
        };
        let mut samples = Vec::with_capacity(width_us * height_us * layout.channels());
        let size = usize::from(bits / 8);
        for y in 0..height {
            for pixel in row(y).chunks_exact(size).take(width_us) {
                let pixel = pixel
                    .iter()
                    .rev()
                    .fold(0, |value, &byte| (value << 8) | u32::from(byte));
                samples.extend([red.read(pixel), green.read(pixel), blue.read(pixel)]);
                if let Some(alpha) = alpha {
                    samples.push(alpha.read(pixel));
                }
            }
        }
        return Ok(Decoded {
            layout,
            data: samples,
            resolution,
        });
    }
    if bits == 24 {
        let mut samples = Vec::with_capacity(width_us * height_us * 3);
        for y in 0..height {
            for &[blue, green, red] in row(y).as_chunks::<3>().0.iter().take(width_us) {
                samples.extend([red, green, blue]);
            }
        }
        return Ok(Decoded {
            layout: Layout {
                width,
                height,
                gray: false,
                alpha: false,
            },
            data: samples,
            resolution,
        });
    }
    let entry = if core { 3 } else { 4 };
    let stated = if core { 0 } else { u32_at(data, 46)? as usize };
    let colors = if stated == 0 || stated > 1 << bits {
        1 << bits
    } else {
        stated
    };
    let start = 14 + header;
    // A palette shorter than its count keeps the entries that it holds.
    let palette: Vec<[u8; 3]> = data
        .get(start..offset.max(start))
        .unwrap_or_default()
        .chunks_exact(entry)
        .take(colors)
        .map(|bgr| [bgr[2], bgr[1], bgr[0]])
        .collect();
    let gray = palette.iter().all(|&[r, g, b]| r == g && g == b);
    let layout = Layout {
        width,
        height,
        gray,
        alpha: false,
    };
    let mut samples = Vec::with_capacity(width_us * height_us * layout.colors());
    let per_byte = 8 / usize::from(bits);
    for y in 0..height {
        let line = row(y);
        for x in 0..width_us {
            let shift = (per_byte - 1 - x % per_byte) * usize::from(bits);
            let index = usize::from(line[x / per_byte] >> shift) & ((1 << bits) - 1);
            let &color = palette
                .get(index)
                .ok_or_else(|| "a BMP pixel indexes past the palette".to_owned())?;
            if gray {
                samples.push(color[0]);
            } else {
                samples.extend(color);
            }
        }
    }
    Ok(Decoded {
        layout,
        data: samples,
        resolution,
    })
}

/// Writes PAM: the samples as they are, with their alpha.
pub fn write_pam(out: &mut dyn Write, layout: Layout, data: &[u8]) -> io::Result<()> {
    let tuple = match (layout.gray, layout.alpha) {
        (true, false) => "GRAYSCALE",
        (true, true) => "GRAYSCALE_ALPHA",
        (false, false) => "RGB",
        (false, true) => "RGB_ALPHA",
    };
    write!(
        out,
        "P7\nWIDTH {}\nHEIGHT {}\nDEPTH {}\nMAXVAL 255\nTUPLTYPE {tuple}\nENDHDR\n",
        layout.width,
        layout.height,
        layout.channels()
    )?;
    out.write_all(data)
}

/// Writes raw PGM for gray or PPM for RGB samples without alpha.
pub fn write_pnm(out: &mut dyn Write, layout: Layout, data: &[u8]) -> io::Result<()> {
    let kind = if layout.gray { 5 } else { 6 };
    write!(out, "P{kind}\n{} {}\n255\n", layout.width, layout.height)?;
    out.write_all(data)
}

/// Writes raw PBM from gray samples without alpha: a sample below 128 is
/// black.
pub fn write_pbm(out: &mut dyn Write, layout: Layout, data: &[u8]) -> io::Result<()> {
    write!(out, "P4\n{} {}\n", layout.width, layout.height)?;
    let width = layout.width as usize;
    let mut row = vec![0; width.div_ceil(8)];
    for line in data.chunks_exact(width) {
        row.fill(0);
        for (x, &sample) in line.iter().enumerate() {
            if sample < 128 {
                row[x / 8] |= 0x80 >> (x % 8);
            }
        }
        out.write_all(&row)?;
    }
    Ok(())
}

/// Writes a flattened version 1 PSD in grayscale or RGB mode, alpha as its
/// extra channel, with the resolution, without compression.
pub fn write_psd(
    out: &mut dyn Write,
    layout: Layout,
    data: &[u8],
    resolution: (u32, u32),
) -> io::Result<()> {
    if layout.width > MAX_PSD_SIDE || layout.height > MAX_PSD_SIDE {
        return Err(io::Error::other(format!(
            "PSD sides end at {MAX_PSD_SIDE} pixels"
        )));
    }
    let channels = layout.channels();
    out.write_all(b"8BPS\0\x01\0\0\0\0\0\0")?;
    out.write_all(&(channels as u16).to_be_bytes())?;
    out.write_all(&layout.height.to_be_bytes())?;
    out.write_all(&layout.width.to_be_bytes())?;
    out.write_all(&8_u16.to_be_bytes())?;
    out.write_all(&(if layout.gray { 1_u16 } else { 3 }).to_be_bytes())?;
    // No color mode data; one ResolutionInfo resource in pixels per inch.
    out.write_all(&0_u32.to_be_bytes())?;
    out.write_all(&28_u32.to_be_bytes())?;
    out.write_all(b"8BIM\x03\xed\0\0")?;
    out.write_all(&16_u32.to_be_bytes())?;
    for dpi in [resolution.0, resolution.1] {
        out.write_all(&(dpi << 16).to_be_bytes())?;
        out.write_all(&[0, 1, 0, 1])?;
    }
    // No layers, then raw planar samples.
    out.write_all(&0_u32.to_be_bytes())?;
    out.write_all(&0_u16.to_be_bytes())?;
    let width = layout.width as usize;
    let mut plane = vec![0; width];
    for channel in 0..channels {
        for line in data.chunks_exact(width * channels) {
            for (sample, pixel) in plane.iter_mut().zip(line.chunks_exact(channels)) {
                *sample = pixel[channel];
            }
            out.write_all(&plane)?;
        }
    }
    Ok(())
}

/// Writes a one-page PostScript Level 2 document of gray or RGB samples
/// without alpha, the page sized to the image at its resolution, and the
/// samples in ASCII85.
pub fn write_ps(
    out: &mut dyn Write,
    layout: Layout,
    data: &[u8],
    resolution: (u32, u32),
) -> io::Result<()> {
    let (width, height) = (layout.width, layout.height);
    let points = |pixels: u32, dpi: u32| f64::from(pixels) * 72.0 / f64::from(dpi);
    let (x, y) = (points(width, resolution.0), points(height, resolution.1));
    let (space, decode) = if layout.gray {
        ("DeviceGray", "0 1")
    } else {
        ("DeviceRGB", "0 1 0 1 0 1")
    };
    write!(
        out,
        "%!PS-Adobe-3.0\n%%Creator: pyropdf\n%%LanguageLevel: 2\n\
         %%BoundingBox: 0 0 {} {}\n%%HiResBoundingBox: 0 0 {x} {y}\n\
         %%Pages: 1\n%%EndComments\n%%Page: 1 1\n\
         << /PageSize [{x} {y}] >> setpagedevice\ngsave\n{x} {y} scale\n/{space} setcolorspace\n\
         << /ImageType 1 /Width {width} /Height {height} /BitsPerComponent 8 /Decode [{decode}] \
         /ImageMatrix [{width} 0 0 -{height} 0 {height}] /DataSource currentfile /ASCII85Decode filter >>\n\
         image\n",
        x.ceil(),
        y.ceil()
    )?;
    let mut line = Vec::with_capacity(ASCII85_LINE + 1);
    for group in data.chunks(4) {
        let mut word = [0; 4];
        word[..group.len()].copy_from_slice(group);
        let mut value = u32::from_be_bytes(word);
        if group.len() == 4 && value == 0 {
            line.push(b'z');
        } else {
            let mut digits = [0; 5];
            for digit in digits.iter_mut().rev() {
                *digit = (value % 85) as u8 + b'!';
                value /= 85;
            }
            line.extend_from_slice(&digits[..group.len() + 1]);
        }
        if line.len() >= ASCII85_LINE {
            line.push(b'\n');
            out.write_all(&line)?;
            line.clear();
        }
    }
    out.write_all(&line)?;
    out.write_all(b"~>\ngrestore\nshowpage\n%%Trailer\n%%EOF\n")
}
