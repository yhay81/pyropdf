use proptest::prelude::*;
use pyropdf_store::budget::{Budget, LimitCode};

use super::{
    DecodeError, Encoding, Layout, MAX_PIXELS, Projection, add_alpha, check_size, color_runs,
    convert, crop, decode, drop_alpha, encode, fax, fill, flip, gamma_table, images, invert,
    map_colors, recolor_samples, rotate, scale, tint_table, warp,
};

fn layout_and_samples() -> impl Strategy<Value = (Layout, Vec<u8>)> {
    (1_u32..=16, 1_u32..=16, any::<bool>(), any::<bool>()).prop_flat_map(
        |(width, height, gray, alpha)| {
            let layout = Layout {
                width,
                height,
                gray,
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
        prop_assert_eq!((w, h, gray, alpha.is_some()), (layout.width, layout.height, layout.gray, layout.alpha));
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
        let layout = Layout { width, height, gray, alpha: false };
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
        gray: true,
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
        gray: false,
        alpha: false,
    };
    let data: Vec<u8> = (0..36).map(|i| (i * 7) as u8).collect();

    let mut psd = Vec::new();
    encode(&mut psd, layout, &data, (150, 150), Encoding::Psd, &budget).unwrap();
    assert!(psd.starts_with(b"8BPS"));
    let wide = Layout {
        width: 30_001,
        height: 1,
        gray: true,
        alpha: false,
    };
    assert!(encode(&mut Vec::new(), wide, &[], (96, 96), Encoding::Psd, &budget).is_err());

    let mut ps = Vec::new();
    encode(&mut ps, layout, &data, (72, 72), Encoding::Ps, &budget).unwrap();
    assert!(ps.starts_with(b"%!PS-Adobe-3.0\n"));
    assert!(ps.ends_with(b"~>\ngrestore\nshowpage\n%%Trailer\n%%EOF\n"));

    let cmyk = [20_u8, 40, 60, 80].repeat(4);
    let with_adobe = images::encode_jpeg_samples(&cmyk, 2, 2, 4, 90, true).unwrap();
    let without_adobe = images::encode_jpeg_samples(&cmyk, 2, 2, 4, 90, false).unwrap();
    assert!(with_adobe.windows(5).any(|w| w == b"Adobe"));
    assert!(!without_adobe.windows(5).any(|w| w == b"Adobe"));
    let (w, h, comps, _, alpha) = images::image_samples(&with_adobe, false).unwrap();
    assert_eq!((w, h, comps, alpha), (2, 2, 4, None));

    assert_eq!(recolor_samples(&data, layout, 4, None).unwrap().len(), 48);
    assert!(recolor_samples(&data, layout, 4, Some(85)).is_err());

    // Exercise crop, scale, fill, gamma, tint, convert, and projective warp.
    let cropped = crop(&data, layout, (1, 1, 3, 3));
    assert_eq!(cropped.len(), 2 * 2 * 3);
    let scaled = scale(&data, layout, 6, 5);
    assert_eq!(scaled.len(), 6 * 5 * 3);
    let filled = fill(&data, layout, (0, 0, 2, 1), &[9, 8, 7]);
    assert_eq!(&filled[..6], &[9, 8, 7, 9, 8, 7]);
    let gray = convert(&data, layout, true);
    assert_eq!(gray.len(), 12);
    let gamma = gamma_table(1.0);
    let identity = tint_table(0, 255);
    assert_eq!(gamma, identity);
    assert_eq!(map_colors(&data, layout, &[identity; 3]), data);
    let proj = Projection::new([(0.0, 0.0), (4.0, 0.0), (0.0, 3.0), (4.0, 3.0)]).unwrap();
    assert_eq!(warp(&data, layout, proj, 4, 3).len(), 36);
    assert!(fax::encode_checked(&[0xFF], (MAX_PIXELS as usize) + 1, 1, 64).is_err());
    assert!(fax::encode_checked(&[0xFF], 8, 2, 64).is_err());
}
