//! Lossless JPEG 2000 output for gray and RGB image XObjects (ISO 32000-2,
//! 7.4.9). A JP2 wrapper carries the image header and enumerated color space.

use j2k_native::{EncodeOptions, encode};

/// Encodes 8-bit interleaved gray or RGB samples with reversible 5/3 coding.
/// The shared raster limit bounds the sample planes, and the tile dimensions
/// bound the wavelet and block-coding workspace independently of image width.
#[expect(
    clippy::disallowed_methods,
    reason = "the JP2 container uses an exact size checked against the caller's limit"
)]
pub fn encode_jp2(
    data: &[u8],
    width: u32,
    height: u32,
    components: u16,
    limit: usize,
) -> Result<Option<Vec<u8>>, String> {
    super::check_size(width, height)?;
    if !matches!(components, 1 | 3)
        || u64::from(width) * u64::from(height) * u64::from(components) != data.len() as u64
    {
        return Err("expected 8-bit gray or RGB samples matching the image dimensions".into());
    }
    const HEADER: usize = 85;
    if limit < HEADER {
        return Ok(None);
    }
    let options = EncodeOptions {
        tile_size: Some((width.min(256), height.min(256))),
        use_mct: components == 3,
        ..EncodeOptions::default()
    };
    let codestream = encode(data, width, height, components, 8, false, &options)
        .map_err(|error| error.to_string())?;
    let total = HEADER + codestream.len();
    if total > limit {
        return Ok(None);
    }
    let box_size =
        u32::try_from(8 + codestream.len()).map_err(|_| "oversized JP2 codestream box")?;
    let mut bytes = Vec::with_capacity(total);
    // Signature, file type, JP2 header box and image header box.
    bytes.extend_from_slice(b"\0\0\0\x0cjP  \r\n\x87\n\0\0\0\x14ftypjp2 \0\0\0\0jp2 ");
    bytes.extend_from_slice(b"\0\0\0\x2djp2h\0\0\0\x16ihdr");
    bytes.extend_from_slice(&height.to_be_bytes());
    bytes.extend_from_slice(&width.to_be_bytes());
    bytes.extend_from_slice(&components.to_be_bytes());
    bytes.extend_from_slice(&[7, 7, 0, 0]); // 8-bit unsigned, JPEG 2000, known color space
    bytes.extend_from_slice(b"\0\0\0\x0fcolr\x01\0\0"); // enumerated color space
    bytes.extend_from_slice(&(if components == 3 { 16u32 } else { 17 }).to_be_bytes());
    bytes.extend_from_slice(&box_size.to_be_bytes());
    bytes.extend_from_slice(b"jp2c");
    bytes.extend_from_slice(&codestream);
    Ok(Some(bytes))
}
