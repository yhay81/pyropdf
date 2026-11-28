use proptest::prelude::*;
use pyropdf_store::budget::{Budget, LimitCode};

use super::{
    Colorspace, DecodeError, EncodeError, Encoding, Layout, MAX_PIXELS, Projection, add_alpha,
    check_size, color_runs, convert, crop, decode, drop_alpha, encode, fax, fill, flip,
    gamma_table, images, invert, map_colors, recolor_samples, rotate, scale, tint_table, warp,
};

fn layout_and_samples() -> impl Strategy<Value = (Layout, Vec<u8>)> {
    (1_u32..=16, 1_u32..=16, any::<bool>(), any::<bool>()).prop_flat_map(
        |(width, height, gray, alpha)| {
            let layout = Layout {
                width,
                height,
                colorspace: Colorspace::from_gray(gray),
                alpha,
            };
            let len = width as usize * height as usize * layout.channels();
            (Just(layout), proptest::collection::vec(any::<u8>(), len))
        },
    )
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(48))]

    #[test]
    fn png_and_pam_round_trip_every_layout(
        (layout, data) in layout_and_samples(),
        xdpi in 72_u32..=600,
        ydpi in 72_u32..=600,
    ) {
        let budget = Budget::default();
        let mut png = Vec::new();
        encode(&mut png, layout, &data, (xdpi, ydpi), Encoding::Png, &budget).unwrap();
        let decoded = decode(&png, &budget).unwrap();
        prop_assert_eq!(decoded.layout, layout);
        prop_assert_eq!(&decoded.data, &data);
        prop_assert!(decoded.resolution.0.abs_diff(xdpi) <= 1);
        prop_assert!(decoded.resolution.1.abs_diff(ydpi) <= 1);

        let (w, h, gray, color, alpha) = images::decode_png_buffers(&png, &budget).unwrap();
        prop_assert_eq!((w, h, gray, alpha.is_some()), (layout.width, layout.height, layout.colorspace.is_gray(), layout.alpha));
        if let Some(alpha) = alpha {
            let colors = layout.colors();
            let mut joined = Vec::new();
            for (px, a) in color.chunks_exact(colors).zip(alpha.as_slice()) {
                joined.extend_from_slice(px);
                joined.push(*a);
            }
            prop_assert_eq!(&joined, &data);
        } else {
            prop_assert_eq!(color.as_slice(), data.as_slice());
        }

        let mut pam = Vec::new();
        encode(&mut pam, layout, &data, (96, 96), Encoding::Pam, &budget).unwrap();
        let decoded_pam = decode(&pam, &budget).unwrap();
        prop_assert_eq!(decoded_pam.layout, layout);
        prop_assert_eq!(&decoded_pam.data, &data);
    }

    #[test]
    fn pnm_and_pbm_round_trip_opaque_layouts(
        width in 1_u32..=19,
        height in 1_u32..=19,
        gray in any::<bool>(),
    ) {
        let layout = Layout { width, height, colorspace: Colorspace::from_gray(gray), alpha: false };
        let len = width as usize * height as usize * layout.channels();
        let data: Vec<u8> = (0..len).map(|i| ((i * 73 + 19) % 256) as u8).collect();
        let budget = Budget::default();

        let mut pnm = Vec::new();
        encode(&mut pnm, layout, &data, (96, 96), Encoding::Pnm, &budget).unwrap();
        let decoded = decode(&pnm, &budget).unwrap();
        prop_assert_eq!(decoded.layout, layout);
        prop_assert_eq!(&decoded.data, &data);

        if gray {
            let mut pbm = Vec::new();
            encode(&mut pbm, layout, &data, (96, 96), Encoding::Pbm, &budget).unwrap();
            let decoded_pbm = decode(&pbm, &budget).unwrap();
            let expected: Vec<u8> = data
                .iter()
                .map(|&sample| if sample < 128 { 0 } else { 255 })
                .collect();
            prop_assert_eq!(decoded_pbm.layout, layout);
            prop_assert_eq!(decoded_pbm.data, expected);
        }
    }

    #[test]
    fn pixel_transforms_preserve_algebraic_invariants(
        (layout, data) in layout_and_samples(),
        bg in any::<[u8; 3]>(),
    ) {
        let full = (0, 0, layout.width, layout.height);

        // Double inversion is the identity.
        let once = invert(&data, layout, full);
        prop_assert_eq!(&invert(&once, layout, full), &data);

        // Horizontal and vertical flips are involutions, and H + V equals a 180-degree turn.
        let h = flip(&data, layout, false);
        prop_assert_eq!(&flip(&h, layout, false), &data);
        let v = flip(&data, layout, true);
        prop_assert_eq!(&flip(&v, layout, true), &data);
        prop_assert_eq!(flip(&h, layout, true), rotate(&data, layout, 2));

        // 90 degrees followed by 270 degrees restores the original samples.
        let turned = rotate(&data, layout, 1);
        let swapped = Layout { width: layout.height, height: layout.width, ..layout };
        prop_assert_eq!(&rotate(&turned, swapped, 3), &data);

        // Adding opaque alpha and compositing over any background restores an opaque raster.
        if !layout.alpha {
            let with_a = add_alpha(&data, layout);
            let alpha_layout = Layout { alpha: true, ..layout };
            prop_assert_eq!(&drop_alpha(&with_a, alpha_layout, bg), &data);
        }

        // Color runs partition the area in strictly ascending pixel order.
        let mut total = 0_u64;
        let mut previous: Option<Vec<u8>> = None;
        color_runs(&data, layout, full, |pixel, count| {
            if let Some(prev) = &previous {
                assert!(prev < &pixel);
            }
            previous = Some(pixel);
            total += count;
            true
        });
        prop_assert_eq!(total, u64::from(layout.width) * u64::from(layout.height));
    }

    #[test]
    fn ccitt_g4_respects_output_limits(
        width in 1_usize..=72,
        height in 1_usize..=24,
        seed in any::<u8>(),
        limit in 0_usize..=64,
    ) {
        let len = width.div_ceil(8) * height;
        let data: Vec<u8> = (0..len).map(|i| seed.wrapping_add(i as u8).wrapping_mul(37)).collect();
        if let Some(encoded) = fax::encode_checked(&data, width, height, limit).unwrap() {
            prop_assert!(encoded.len() <= limit);
        }
        let unbounded = fax::encode_checked(&data, width, height, 1 << 20).unwrap();
        prop_assert!(unbounded.is_some());
    }
}

#[test]
fn size_and_malformed_header_bounds_refuse_before_allocation() {
    assert!(check_size(0, 10).is_err());
    assert!(check_size(10, 0).is_err());
    assert!(check_size(8_000, 8_000).is_ok());
    assert!(check_size(8_001, 8_000).is_err());

    let budget = Budget::default();
    for bad in [
        &b"P6 8001 8000 255\n"[..],
        &b"P6 4 4 255\nshort"[..],
        &b"P7\nWIDTH 8001\nHEIGHT 8000\nDEPTH 3\nMAXVAL 255\nENDHDR\n"[..],
        &b"BM\x00\x00"[..],
        &b"II\xbc\x01"[..],
        &b"not an image"[..],
    ] {
        assert!(matches!(decode(bad, &budget), Err(DecodeError::Invalid(_))));
    }

    let tiny_budget = Budget::default().with_limit(LimitCode::TotalDecompressedSize, Some(8));
    let layout = Layout {
        width: 4,
        height: 4,
        colorspace: Colorspace::Gray,
        alpha: false,
    };
    let mut png = Vec::new();
    encode(
        &mut png,
        layout,
        &[128; 16],
        (96, 96),
        Encoding::Png,
        &budget,
    )
    .unwrap();
    assert!(matches!(
        images::decode_png_buffers(&png, &tiny_budget),
        Err(DecodeError::Limit(_))
    ));
}

#[test]
fn jpeg_psd_ps_and_other_ops_behave_as_specified() {
    let budget = Budget::default();
    let layout = Layout {
        width: 4,
        height: 3,
        colorspace: Colorspace::Rgb,
        alpha: false,
    };
    let data: Vec<u8> = (0..36).map(|i| (i * 7) as u8).collect();

    let mut psd = Vec::new();
    encode(&mut psd, layout, &data, (150, 150), Encoding::Psd, &budget).unwrap();
    assert!(psd.starts_with(b"8BPS"));
    let wide = Layout {
        width: 30_001,
        height: 1,
        colorspace: Colorspace::Gray,
        alpha: false,
    };
    assert!(encode(&mut Vec::new(), wide, &[], (96, 96), Encoding::Psd, &budget).is_err());

    let mut ps = Vec::new();
    encode(&mut ps, layout, &data, (72, 72), Encoding::Ps, &budget).unwrap();
    assert!(ps.starts_with(b"%!PS-Adobe-3.0\n"));
    assert!(ps.ends_with(b"~>\ngrestore\nshowpage\n%%Trailer\n%%EOF\n"));

    let cmyk = [20_u8, 40, 60, 80].repeat(4);
    let with_adobe = images::encode_jpeg_samples(&cmyk, 2, 2, 4, 90, true, &budget).unwrap();
    let without_adobe = images::encode_jpeg_samples(&cmyk, 2, 2, 4, 90, false, &budget).unwrap();
    assert!(with_adobe.windows(5).any(|w| w == b"Adobe"));
    assert!(!without_adobe.windows(5).any(|w| w == b"Adobe"));
    let (w, h, comps, _, alpha) = images::image_samples(&with_adobe, false, &budget).unwrap();
    assert_eq!((w, h, comps, alpha), (2, 2, 4, None));

    assert_eq!(
        recolor_samples(&data, layout, 4, None, &budget)
            .unwrap()
            .len(),
        48
    );
    assert!(recolor_samples(&data, layout, 4, Some(85), &budget).is_err());

    // Exercise crop, scale, fill, gamma, tint, convert, and projective warp.
    let cropped = crop(&data, layout, (1, 1, 3, 3));
    assert_eq!(cropped.len(), 2 * 2 * 3);
    let scaled = scale(&data, layout, 6, 5);
    assert_eq!(scaled.len(), 6 * 5 * 3);
    let filled = fill(&data, layout, (0, 0, 2, 1), &[9, 8, 7]);
    assert_eq!(&filled[..6], &[9, 8, 7, 9, 8, 7]);
    let gray = convert(&data, layout, Colorspace::Gray);
    assert_eq!(gray.len(), 12);
    let gamma = gamma_table(1.0);
    let identity = tint_table(0, 255);
    assert_eq!(gamma, identity);
    assert_eq!(map_colors(&data, layout, &[identity; 4]), data);
    let proj = Projection::new([(0.0, 0.0), (4.0, 0.0), (0.0, 3.0), (4.0, 3.0)]).unwrap();
    assert_eq!(warp(&data, layout, proj, 4, 3).len(), 36);
    assert!(fax::encode_checked(&[0xFF], (MAX_PIXELS as usize) + 1, 1, 64).is_err());
    assert!(fax::encode_checked(&[0xFF], 8, 2, 64).is_err());
}

#[test]
fn zero_width_interlaced_gif_does_not_panic() {
    // GIF89a 1x1 logical screen with a 2-color global palette and a 0x0 interlaced frame.
    let gif = [
        b'G', b'I', b'F', b'8', b'9', b'a', 1, 0, 1, 0, 0x80, 0, 0, 0, 0, 0, 255, 255, 255, 0x2C,
        0, 0, 0, 0, 0, 0, 0, 0, 0x40, 2, 2, 0x4C, 0x01, 0, 0x3B,
    ];
    let decoded = decode(&gif, &Budget::default()).unwrap();
    assert_eq!((decoded.layout.width, decoded.layout.height), (1, 1));
}

#[test]
fn png_phys_dpi_clamps_to_u16_max_and_encoders_never_truncate() {
    // Build a minimal 1x1 RGB PNG with pHYs = 2_580_158 px/m (~65536 DPI).
    let mut png = Vec::new();
    encode(
        &mut png,
        Layout {
            width: 1,
            height: 1,
            colorspace: Colorspace::Rgb,
            alpha: false,
        },
        &[10, 20, 30],
        (96, 96),
        Encoding::Png,
        &Budget::default(),
    )
    .unwrap();
    // Locate the pHYs chunk and overwrite xppu/yppu with 2_580_158 and valid CRC.
    let phys_pos = png.windows(4).position(|w| w == b"pHYs").unwrap();
    let payload = [
        2_580_158_u32.to_be_bytes()[0],
        2_580_158_u32.to_be_bytes()[1],
        2_580_158_u32.to_be_bytes()[2],
        2_580_158_u32.to_be_bytes()[3],
        2_580_158_u32.to_be_bytes()[0],
        2_580_158_u32.to_be_bytes()[1],
        2_580_158_u32.to_be_bytes()[2],
        2_580_158_u32.to_be_bytes()[3],
        1,
    ];
    png[phys_pos + 4..phys_pos + 13].copy_from_slice(&payload);
    let mut crc = flate2::Crc::new();
    crc.update(b"pHYs");
    crc.update(&payload);
    png[phys_pos + 13..phys_pos + 17].copy_from_slice(&crc.sum().to_be_bytes());

    let decoded = decode(&png, &Budget::default()).unwrap();
    assert_eq!(decoded.resolution, (65_535, 65_535));

    // Encoding with an unclamped resolution directly also clamps cleanly for JPEG and PSD.
    let mut psd = Vec::new();
    encode(
        &mut psd,
        decoded.layout,
        &decoded.data,
        (70_000, 70_000),
        Encoding::Psd,
        &Budget::default(),
    )
    .unwrap();
    let res_pos = psd.windows(6).position(|w| w == b"8BIM\x03\xed").unwrap();
    let h_res = u32::from_be_bytes(psd[res_pos + 12..res_pos + 16].try_into().unwrap());
    assert_eq!(h_res, 65_535_u32 << 16);
}

#[test]
fn projection_rejects_overflowing_finite_quad_and_rotate_handles_zero_and_four() {
    assert!(Projection::new([(0.0, 0.0), (1e200, 0.0), (0.0, 1e200), (1e200, 2e200),]).is_none());
    let layout = Layout {
        width: 2,
        height: 4,
        colorspace: Colorspace::Gray,
        alpha: false,
    };
    let pixels: Vec<u8> = (0..8).collect();
    assert_eq!(rotate(&pixels, layout, 0), pixels);
    assert_eq!(rotate(&pixels, layout, 4), pixels);
}

#[test]
fn recolor_and_encode_jpeg_samples_obey_budget_limits() {
    use pyropdf_store::budget::{CancellationToken, LimitCode};

    let layout = Layout {
        width: 4,
        height: 4,
        colorspace: Colorspace::Rgb,
        alpha: false,
    };
    let data = vec![128_u8; 4 * 4 * 3];
    let tight = Budget::default().with_limit(LimitCode::MemorySize, Some(10));
    assert!(matches!(
        recolor_samples(&data, layout, 3, None, &tight),
        Err(EncodeError::Limit(refusal)) if refusal.code() == LimitCode::MemorySize
    ));
    assert!(matches!(
        recolor_samples(&data, layout, 3, Some(80), &tight),
        Err(EncodeError::Limit(refusal)) if refusal.code() == LimitCode::MemorySize
    ));
    let cmyk = vec![10_u8; 4 * 4 * 4];
    assert!(matches!(
        images::encode_jpeg_samples(&cmyk, 4, 4, 4, 80, true, &tight),
        Err(EncodeError::Limit(refusal)) if refusal.code() == LimitCode::MemorySize
    ));

    let token = CancellationToken::new();
    token.cancel();
    let cancelled = Budget::default().with_cancellation(token);
    assert!(matches!(
        recolor_samples(&data, layout, 3, None, &cancelled),
        Err(EncodeError::Limit(refusal)) if refusal.code() == LimitCode::Cancelled
    ));
    assert!(matches!(
        images::encode_jpeg_samples(&data, 4, 4, 3, 80, false, &cancelled),
        Err(EncodeError::Limit(refusal)) if refusal.code() == LimitCode::Cancelled
    ));

    let jpeg = images::encode_jpeg_samples(&cmyk, 4, 4, 4, 80, true, &Budget::default()).unwrap();
    assert!(matches!(
        images::image_samples(&jpeg, false, &tight),
        Err(DecodeError::Limit(refusal)) if refusal.code() == LimitCode::MemorySize
    ));
    let tiny_decompressed =
        Budget::default().with_limit(LimitCode::TotalDecompressedSize, Some(10));
    assert!(matches!(
        images::image_samples(&jpeg, false, &tiny_decompressed),
        Err(DecodeError::Limit(refusal)) if refusal.code() == LimitCode::TotalDecompressedSize
    ));
    assert!(matches!(
        images::image_samples(&jpeg, false, &cancelled),
        Err(DecodeError::Limit(refusal)) if refusal.code() == LimitCode::Cancelled
    ));
    let jp2 = super::jpx::encode_jp2(&data, 4, 4, 3, 1 << 20)
        .unwrap()
        .unwrap();
    assert!(matches!(
        images::image_samples(&jp2, true, &tight),
        Err(DecodeError::Limit(refusal)) if refusal.code() == LimitCode::MemorySize
    ));
    assert!(matches!(
        images::image_samples(&jp2, true, &cancelled),
        Err(DecodeError::Limit(refusal)) if refusal.code() == LimitCode::Cancelled
    ));
}
