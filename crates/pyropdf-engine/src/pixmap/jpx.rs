//! Lossless JPEG 2000 output for gray and RGB image XObjects (ISO 32000-2,
//! 7.4.9). A JP2 wrapper carries the image header and enumerated color space.

use j2k_native::{J2kCodeBlockStyle, J2kSubBandType, encode_j2k_code_block_scalar_with_style};

const TILE: u32 = 64;
const BLOCK_STYLE: J2kCodeBlockStyle = J2kCodeBlockStyle {
    selective_arithmetic_coding_bypass: false,
    reset_context_probabilities: false,
    termination_on_each_pass: false,
    vertically_causal_context: false,
    segmentation_symbols: false,
};

/// Encodes 8-bit interleaved gray or RGB samples in the reversible Part 1 profile.
/// The shared raster limit bounds the sample planes, and the tile dimensions
/// bound the block-coding workspace independently of image width.
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
    let Some(codestream) = codestream(data, width, height, components, limit - HEADER)? else {
        return Ok(None);
    };
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

/// A tile is one low-pass code block per component, with one packet per block,
/// one resolution and one quality layer. ISO/IEC 15444-1 A.5 and B.10.
/// The scalar Tier-1 encoder supplies the same arithmetic coding as the full
/// codec without linking its wavelet transforms, HTJ2K or alternate progressions.
#[expect(
    clippy::disallowed_methods,
    reason = "the tile and output limit bound all buffers"
)]
fn codestream(
    data: &[u8],
    width: u32,
    height: u32,
    components: u16,
    limit: usize,
) -> Result<Option<Vec<u8>>, String> {
    let mut out = Vec::new();
    out.extend_from_slice(&[0xff, 0x4f, 0xff, 0x51]); // SOC, SIZ
    out.extend_from_slice(&(38 + 3 * components).to_be_bytes());
    out.extend_from_slice(&0_u16.to_be_bytes()); // Part 1
    for v in [width, height, 0, 0, TILE, TILE, 0, 0] {
        out.extend_from_slice(&v.to_be_bytes());
    }
    out.extend_from_slice(&components.to_be_bytes());
    for _ in 0..components {
        out.extend_from_slice(&[7, 1, 1]); // Unsigned 8-bit, no subsampling
    }
    // COD: LRCP, one layer, no MCT, no decompositions, 64x64 blocks, reversible.
    out.extend_from_slice(&[0xff, 0x52, 0, 12, 0, 0, 0, 1, 0, 0, 4, 4, 0, 1]);
    // QCD: no quantization, one guard bit and an 8-bit LL band.
    out.extend_from_slice(&[0xff, 0x5c, 0, 4, 0x20, 0x40]);
    let across = width.div_ceil(TILE);
    let tiles = across * height.div_ceil(TILE);
    if tiles > u32::from(u16::MAX) {
        return Err("too many JPEG 2000 tiles".into());
    }
    let mut coefficients = [0_i32; (TILE * TILE) as usize];
    for tile in 0..tiles {
        let x = tile % across * TILE;
        let y = tile / across * TILE;
        let w = (width - x).min(TILE);
        let h = (height - y).min(TILE);
        let start = out.len();
        out.extend_from_slice(&[0xff, 0x90, 0, 10]);
        out.extend_from_slice(&(tile as u16).to_be_bytes());
        out.extend_from_slice(&[0, 0, 0, 0, 0, 1, 0xff, 0x93]); // Psot, TPsot, TNsot, SOD
        for component in 0..components {
            let plane = &mut coefficients[..(w * h) as usize];
            for (row, dest) in plane.chunks_exact_mut(w as usize).enumerate() {
                let source =
                    ((y as usize + row) * width as usize + x as usize) * usize::from(components);
                for (col, coefficient) in dest.iter_mut().enumerate() {
                    *coefficient = i32::from(
                        data[source + col * usize::from(components) + usize::from(component)],
                    ) - 128;
                }
            }
            let block = encode_j2k_code_block_scalar_with_style(
                plane,
                w,
                h,
                J2kSubBandType::LowLow,
                8,
                BLOCK_STYLE,
            )
            .map_err(|e| e.to_string())?;
            let header = packet_header(
                block.number_of_coding_passes,
                block.missing_bit_planes,
                block.data.len(),
            );
            if out.len() + header.len() + block.data.len() + 2 > limit {
                return Ok(None);
            }
            out.extend_from_slice(&header);
            out.extend_from_slice(&block.data);
        }
        let length = u32::try_from(out.len() - start).map_err(|_| "oversized JPEG 2000 tile")?;
        out[start + 6..start + 10].copy_from_slice(&length.to_be_bytes());
    }
    out.extend_from_slice(&[0xff, 0xd9]); // EOC
    Ok(Some(out))
}

/// Encodes one first-layer packet header (B.10.5 through B.10.7). With one
/// code block the inclusion and zero-bitplane tag trees each have one leaf.
#[expect(
    clippy::disallowed_methods,
    reason = "an eight-bit block needs at most eight header bytes"
)]
fn packet_header(passes: u8, missing: u8, length: usize) -> Vec<u8> {
    if passes == 0 {
        return vec![0];
    }
    let mut bytes = Vec::with_capacity(8);
    let mut byte = 0_u8;
    let mut free = 8;
    let mut write = |value: usize, count: u32| {
        for bit in (0..count).rev() {
            free -= 1;
            byte |= (((value >> bit) & 1) as u8) << free;
            if free == 0 {
                bytes.push(byte);
                free = if byte == 0xff { 7 } else { 8 };
                byte = 0;
            }
        }
    };
    write(3, 2); // Nonempty packet, included on layer zero.
    write(1, u32::from(missing) + 1);
    match passes {
        1 => write(0, 1),
        2 => write(2, 2),
        3..=5 => write(12 + usize::from(passes - 3), 4),
        _ => write(480 + usize::from(passes - 6), 9),
    }
    let initial = 3 + passes.ilog2();
    let bits = (length.ilog2() + 1).max(initial);
    for _ in initial..bits {
        write(1, 1);
    }
    write(0, 1);
    write(length, bits);
    if free != 8 {
        bytes.push(byte);
    }
    bytes
}
