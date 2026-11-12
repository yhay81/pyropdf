//! Encoding a pixmap's samples to PNG, JPEG, PAM, PBM, PGM, PPM, PSD, and
//! PostScript, and recoloring samples for PDF image replacement.

use std::io::{self, Write};
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};

use jpeg_encoder::{ColorType, Encoder as JpegEncoder, PixelDensity, PixelDensityUnit};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::save::deflate;

use super::{Layout, codecs, device_samples};

/// Metres per inch, for PNG's pixels per metre.
const METRES_PER_INCH: f64 = 0.0254;

/// The most compressed data that one PNG `IDAT` chunk holds.
const PNG_IDAT_SIZE: usize = 1024 * 1024;

/// The formats that [`Encoding::parse`] reads.
pub const FORMATS: &str = "'png', 'jpeg', 'pam', 'pbm', 'pgm', 'ppm', 'pnm', 'psd', or 'ps'";

/// An image format that a pixmap encodes to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Encoding {
    Png,
    /// JPEG at a quality from 1 through 100.
    Jpeg(u8),
    Pam,
    /// PBM, gray thresholded at 128.
    Pbm,
    /// PGM for gray or PPM for RGB, as `"pgm"`, `"ppm"`, or `"pnm"` asks.
    Pnm,
    Psd,
    /// PostScript.
    Ps,
}

impl Encoding {
    /// Reads `format` at `quality` for samples in gray or RGB, with or
    /// without alpha: PNG, PAM, and PSD hold every kind; JPEG, PNM, and
    /// PostScript hold no alpha; PBM and PGM hold only gray, and PPM only
    /// RGB.
    pub fn parse(format: &str, quality: u8, gray: bool, alpha: bool) -> Result<Self, String> {
        let encoding = match format {
            "png" => Self::Png,
            "jpeg" => Self::Jpeg(quality),
            "pam" => Self::Pam,
            "pbm" => Self::Pbm,
            "pgm" | "ppm" | "pnm" => Self::Pnm,
            "psd" => Self::Psd,
            "ps" => Self::Ps,
            _ => return Err(format!("format must be {FORMATS}: {format:?}")),
        };
        let name = format.to_ascii_uppercase();
        if alpha && !matches!(encoding, Self::Png | Self::Pam | Self::Psd) {
            return Err(format!(
                "{} has no alpha channel; drop alpha or write PNG, PAM, or PSD",
                encoding.name()
            ));
        }
        if !gray && matches!(format, "pbm" | "pgm") {
            return Err(format!(
                "{name} holds gray samples; convert the pixmap to gray or write PPM"
            ));
        }
        if gray && format == "ppm" {
            return Err("PPM holds RGB samples; convert the pixmap to RGB or write PGM".to_owned());
        }
        Ok(encoding)
    }

    /// Returns the human-readable format name used in error messages.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Png => "PNG",
            Self::Jpeg(_) => "JPEG",
            Self::Pam => "PAM",
            Self::Pbm => "PBM",
            Self::Pnm => "PNM",
            Self::Psd => "PSD",
            Self::Ps => "PostScript",
        }
    }
}

/// Returns the image format that the extension of `path` names.
#[must_use]
pub fn format_of(path: &Path) -> Option<&'static str> {
    let extension = path.extension()?.to_str()?.to_ascii_lowercase();
    match extension.as_str() {
        "png" => Some("png"),
        "jpg" | "jpeg" => Some("jpeg"),
        "pam" => Some("pam"),
        "pbm" => Some("pbm"),
        "pgm" => Some("pgm"),
        "ppm" => Some("ppm"),
        "pnm" => Some("pnm"),
        "psd" => Some("psd"),
        "ps" => Some("ps"),
        _ => None,
    }
}

/// Why encoding a pixmap failed.
#[derive(Debug)]
pub enum EncodeError {
    /// The operation's budget refused it.
    Limit(LimitExceeded),
    /// The encoder or the output failed.
    Other(String),
}

/// Writes one PNG chunk: its length, type, `data`, and CRC.
fn png_chunk(out: &mut dyn Write, kind: &[u8; 4], data: &[u8]) -> io::Result<()> {
    let length = u32::try_from(data.len())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "a PNG chunk is too long"))?;
    let mut crc = flate2::Crc::new();
    crc.update(kind);
    crc.update(data);
    out.write_all(&length.to_be_bytes())?;
    out.write_all(kind)?;
    out.write_all(data)?;
    out.write_all(&crc.sum().to_be_bytes())
}

/// An encoded image sink whose growth is charged to the operation budget.
pub struct BudgetedPngOutput<'b> {
    bytes: BudgetVec<'b, u8>,
    refusal: Option<LimitExceeded>,
}

impl<'b> BudgetedPngOutput<'b> {
    /// Creates an empty budgeted output buffer.
    #[must_use]
    pub fn new(budget: &'b Budget) -> Self {
        Self {
            bytes: BudgetVec::new(budget),
            refusal: None,
        }
    }

    /// Returns the encoded bytes written so far.
    #[must_use]
    pub fn as_slice(&self) -> &[u8] {
        self.bytes.as_slice()
    }

    /// Takes the budget refusal if a write exceeded the budget.
    pub const fn take_refusal(&mut self) -> Option<LimitExceeded> {
        self.refusal.take()
    }
}

impl Write for BudgetedPngOutput<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        match self.bytes.try_extend_from_slice(bytes) {
            Ok(()) => Ok(bytes.len()),
            Err(refusal) => {
                self.refusal = Some(refusal);
                Err(io::Error::other("PNG output exceeded its budget"))
            }
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// A `Write` that counts what it keeps in `written`, which the writers of
/// one batch share, and refuses the write that would take the count past
/// `limit`.
pub struct Bounded<'a> {
    bytes: Vec<u8>,
    written: &'a AtomicUsize,
    limit: Option<usize>,
    exceeded: bool,
}

impl<'a> Bounded<'a> {
    /// Creates a bounded output buffer sharing `written`.
    #[must_use]
    pub const fn new(written: &'a AtomicUsize, limit: Option<usize>) -> Self {
        Self {
            bytes: Vec::new(),
            written,
            limit,
            exceeded: false,
        }
    }

    /// Returns whether a write exceeded `limit`.
    #[must_use]
    pub const fn exceeded(&self) -> bool {
        self.exceeded
    }

    /// Returns the bytes written so far.
    #[must_use]
    pub fn as_slice(&self) -> &[u8] {
        &self.bytes
    }

    /// Consumes the buffer and returns the written bytes.
    #[must_use]
    pub fn into_bytes(self) -> Vec<u8> {
        self.bytes
    }
}

impl Write for Bounded<'_> {
    #[expect(
        clippy::disallowed_methods,
        reason = "the shared atomic output count checks every extension against the caller output-size limit first"
    )]
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        let counted = self
            .written
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |written| {
                written
                    .checked_add(buffer.len())
                    .filter(|&total| self.limit.is_none_or(|limit| total <= limit))
            });
        if counted.is_err() {
            self.exceeded = true;
            return Err(io::Error::other("the encoded image passes its limit"));
        }
        self.bytes.extend_from_slice(buffer);
        Ok(buffer.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// Writes `data` in `layout` at `resolution` encoded as `encoding`.
pub fn encode(
    out: &mut dyn Write,
    layout: Layout,
    data: &[u8],
    resolution: (u32, u32),
    encoding: Encoding,
    budget: &Budget,
) -> Result<(), EncodeError> {
    if matches!(encoding, Encoding::Png) {
        return encode_png(out, layout, data, resolution, budget);
    }
    encode_other(out, layout, data, resolution, encoding).map_err(EncodeError::Other)
}

/// Writes the pixels in a format other than PNG.
fn encode_other(
    out: &mut dyn Write,
    layout: Layout,
    data: &[u8],
    resolution: (u32, u32),
    encoding: Encoding,
) -> Result<(), String> {
    match encoding {
        Encoding::Png => unreachable!("PNG goes through encode_png"),
        Encoding::Jpeg(quality) => {
            let side = |length: u32| {
                u16::try_from(length).map_err(|_| "JPEG sides end at 65535 pixels".to_owned())
            };
            let mut encoder = JpegEncoder::new(out, quality);
            // Resolutions never pass the 65,535 of JFIF's densities.
            encoder.set_density(PixelDensity {
                density: (resolution.0 as u16, resolution.1 as u16),
                unit: PixelDensityUnit::Inches,
            });
            let color = if layout.gray {
                ColorType::Luma
            } else {
                ColorType::Rgb
            };
            encoder
                .encode(data, side(layout.width)?, side(layout.height)?, color)
                .map_err(|e| e.to_string())
        }
        Encoding::Pam => codecs::write_pam(out, layout, data).map_err(|e| e.to_string()),
        Encoding::Pbm => codecs::write_pbm(out, layout, data).map_err(|e| e.to_string()),
        Encoding::Pnm => codecs::write_pnm(out, layout, data).map_err(|e| e.to_string()),
        Encoding::Psd => {
            codecs::write_psd(out, layout, data, resolution).map_err(|e| e.to_string())
        }
        Encoding::Ps => codecs::write_ps(out, layout, data, resolution).map_err(|e| e.to_string()),
    }
}

/// Writes the pixels as an 8-bit PNG with its resolution in `pHYs`.
/// Each row is stored unfiltered and the image data is compressed with
/// `deflate`, so the buffers are charged to `budget` before they grow
/// and the work polls it.
#[expect(
    clippy::disallowed_methods,
    reason = "the PNG header and physical-size chunks hold exactly 13 and 9 bytes"
)]
pub fn encode_png(
    out: &mut dyn Write,
    layout: Layout,
    data: &[u8],
    resolution: (u32, u32),
    budget: &Budget,
) -> Result<(), EncodeError> {
    let io = |e: io::Error| EncodeError::Other(e.to_string());
    let limit = EncodeError::Limit;
    let row = layout.stride();
    let mut filtered = BudgetVec::new(budget);
    filtered
        .try_reserve_exact((row + 1).saturating_mul(layout.height as usize))
        .map_err(limit)?;
    for (index, line) in data.chunks_exact(row.max(1)).enumerate() {
        if index % 256 == 0 {
            budget.poll().map_err(limit)?;
        }
        filtered.try_push(0).map_err(limit)?;
        filtered.try_extend_from_slice(line).map_err(limit)?;
    }
    if row == 0 {
        filtered
            .try_extend(std::iter::repeat_n(0, layout.height as usize))
            .map_err(limit)?;
    }
    let compressed = deflate(filtered.as_slice(), 1, usize::MAX, budget)
        .map_err(|e| match e.limit() {
            Some(refusal) => EncodeError::Limit(*refusal),
            None => EncodeError::Other(e.to_string()),
        })?
        .ok_or_else(|| EncodeError::Other("the image data could not be compressed".into()))?;
    drop(filtered);
    let color = match (layout.gray, layout.alpha) {
        (true, false) => 0,
        (true, true) => 4,
        (false, false) => 2,
        (false, true) => 6,
    };
    let mut header = Vec::with_capacity(13);
    header.extend_from_slice(&layout.width.to_be_bytes());
    header.extend_from_slice(&layout.height.to_be_bytes());
    header.extend_from_slice(&[8, color, 0, 0, 0]);
    let per_metre = |dpi: u32| (f64::from(dpi) / METRES_PER_INCH).round() as u32;
    let mut physical = Vec::with_capacity(9);
    physical.extend_from_slice(&per_metre(resolution.0).to_be_bytes());
    physical.extend_from_slice(&per_metre(resolution.1).to_be_bytes());
    physical.push(1);
    out.write_all(b"\x89PNG\r\n\x1a\n").map_err(io)?;
    png_chunk(out, b"IHDR", &header).map_err(io)?;
    png_chunk(out, b"pHYs", &physical).map_err(io)?;
    for part in compressed.chunks(PNG_IDAT_SIZE) {
        budget.poll().map_err(limit)?;
        png_chunk(out, b"IDAT", part).map_err(io)?;
    }
    png_chunk(out, b"IEND", &[]).map_err(io)
}

/// Returns the colors of `data` without alpha, as the samples of an image
/// in 1 (gray), 3 (RGB), or 4 (CMYK) `components`, or encoded as a JPEG at
/// `quality` when given, for gray or RGB.
pub fn recolor_samples(
    data: &[u8],
    layout: Layout,
    components: usize,
    quality: Option<u8>,
) -> Result<Vec<u8>, String> {
    let color = match (components, quality) {
        (1 | 3 | 4, None) => None,
        (1, Some(_)) => Some(ColorType::Luma),
        (3, Some(_)) => Some(ColorType::Rgb),
        _ => {
            return Err(format!(
                "no {components}-component samples{}",
                if quality.is_some() { " in JPEG" } else { "" }
            ));
        }
    };
    let samples = device_samples(data, layout, components);
    let (Some(color), Some(quality)) = (color, quality) else {
        return Ok(samples);
    };
    let side = |length: u32| {
        u16::try_from(length).map_err(|_| "JPEG sides end at 65535 pixels".to_owned())
    };
    let (width, height) = (side(layout.width)?, side(layout.height)?);
    let mut out = Vec::new();
    JpegEncoder::new(&mut out, quality)
        .encode(&samples, width, height, color)
        .map_err(|e| e.to_string())?;
    Ok(out)
}
