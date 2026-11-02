//! Two commands for compare.py.
//!
//! `render FILE.pdf DPI PROFILE.icc OUT REPEAT` renders page 1 with hayro
//! 0.8.0 on white, writes the raster as `OUT.rgba`, converts it to CMYK
//! through PROFILE with the relative colorimetric intent, writes `OUT.cmyk`,
//! and prints the median time of each step over REPEAT runs as JSON.
//!
//! `to-srgb PROFILE.icc C,M,Y,K ...` prints the sRGB value of each CMYK
//! value through PROFILE, so that compare.py can compute colour differences.

use std::time::Instant;

use hayro::hayro_interpret::InterpreterSettings;
use hayro::hayro_syntax::Pdf;
use hayro::vello_cpu::color::AlphaColor;
use hayro::{PixmapSettings, RenderCache, RenderSettings};
use moxcms::{ColorProfile, Layout, RenderingIntent, TransformOptions};

fn options() -> TransformOptions {
    TransformOptions {
        rendering_intent: RenderingIntent::RelativeColorimetric,
        ..TransformOptions::default()
    }
}

fn profile(path: &str) -> ColorProfile {
    ColorProfile::new_from_slice(&std::fs::read(path).expect("readable profile"))
        .expect("an ICC profile")
}

fn median(mut times: Vec<f64>) -> f64 {
    times.sort_by(f64::total_cmp);
    times[times.len() / 2]
}

fn render(args: &[String]) {
    let [path, dpi, icc, out, repeat] = args else {
        panic!("usage: cmyk-raster render FILE.pdf DPI PROFILE.icc OUT REPEAT");
    };
    let scale = dpi.parse::<f32>().expect("a number") / 72.0;
    let repeat = repeat.parse::<usize>().expect("a count");
    let pdf = Pdf::new(std::fs::read(path).expect("readable file")).expect("a PDF");
    let page = &pdf.pages()[0];
    let settings = PixmapSettings {
        x_scale: scale,
        y_scale: scale,
        bg_color: AlphaColor::WHITE,
    };
    // The transform is built once per output profile, as a renderer would
    // cache it; its construction is timed separately.
    let started = Instant::now();
    let transform = ColorProfile::new_srgb()
        .create_transform_8bit(Layout::Rgb, &profile(icc), Layout::Rgba, options())
        .expect("an sRGB to CMYK transform");
    let build_ms = started.elapsed().as_secs_f64() * 1e3;

    let (mut render_ms, mut convert_ms) = (Vec::new(), Vec::new());
    let (mut rgba, mut cmyk) = (Vec::new(), Vec::new());
    for _ in 0..repeat {
        let started = Instant::now();
        let pixmap = hayro::render(
            page,
            &RenderCache::new(),
            &InterpreterSettings::default(),
            &RenderSettings::default(),
            &settings,
        );
        render_ms.push(started.elapsed().as_secs_f64() * 1e3);
        // The background is opaque, so the premultiplied bytes are the
        // straight ones; a raster with alpha would be unpremultiplied first.
        rgba = pixmap.data_as_u8_slice().to_vec();
        let started = Instant::now();
        let rgb: Vec<u8> = rgba
            .as_chunks::<4>()
            .0
            .iter()
            .flat_map(|p| [p[0], p[1], p[2]])
            .collect();
        cmyk = vec![0u8; rgb.len() / 3 * 4];
        transform.transform(&rgb, &mut cmyk).expect("converted");
        convert_ms.push(started.elapsed().as_secs_f64() * 1e3);
    }
    std::fs::write(format!("{out}.rgba"), &rgba).expect("written");
    std::fs::write(format!("{out}.cmyk"), &cmyk).expect("written");
    println!(
        "{{\"pixels\": {}, \"build_ms\": {build_ms:.3}, \"render_ms\": {:.3}, \"convert_ms\": {:.3}}}",
        rgba.len() / 4,
        median(render_ms),
        median(convert_ms)
    );
}

fn to_srgb(args: &[String]) {
    let (icc, values) = args
        .split_first()
        .expect("usage: cmyk-raster to-srgb PROFILE.icc C,M,Y,K ...");
    let transform = profile(icc)
        .create_transform_8bit(
            Layout::Rgba,
            &ColorProfile::new_srgb(),
            Layout::Rgb,
            options(),
        )
        .expect("a CMYK to sRGB transform");
    for value in values {
        let cmyk: Vec<u8> = value
            .split(',')
            .map(|v| v.parse().expect("a byte"))
            .collect();
        let mut rgb = [0u8; 3];
        transform.transform(&cmyk, &mut rgb).expect("converted");
        println!("{} {} {}", rgb[0], rgb[1], rgb[2]);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("render") => render(&args[1..]),
        Some("to-srgb") => to_srgb(&args[1..]),
        _ => panic!("usage: cmyk-raster render|to-srgb ..."),
    }
}
