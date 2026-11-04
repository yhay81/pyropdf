//! Writes a static TrueType program for one instance of a variable face and
//! reports what it took (issue #598). Usage:
//! `font-instances FACE OUT AXIS=VALUE...`, for example `wght=700`.
//!
//! Every glyph keeps its ID, composites are flattened, and instructions go,
//! so the result is a face that pyropdf's canvas draws and subsets as it
//! does any static face. The report also compares harfrust's shaping of the
//! source at the instance with its shaping of the result.

use harfrust::{Buffer, Direction, ShapeOptions, ShaperFont};
use skrifa::instance::{Location, Size};
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::raw::TableProvider;
use skrifa::{FontRef, GlyphId, MetadataProvider, Tag};
use std::sync::Arc;
use std::time::Instant;

/// Tables copied from the source: what `Face::read` needs and the shaping
/// tables that this prototype's static face shapes with.
const COPIED: [&[u8; 4]; 7] = [
    b"GDEF", b"GPOS", b"GSUB", b"OS/2", b"cmap", b"name", b"post",
];

type Point = (i32, i32, bool);

#[derive(Default)]
struct Pen {
    contours: Vec<Vec<Point>>,
    cubic: bool,
}

fn round(value: f32) -> i32 {
    value.round() as i32
}

impl OutlinePen for Pen {
    fn move_to(&mut self, x: f32, y: f32) {
        self.contours.push(vec![(round(x), round(y), true)]);
    }
    fn line_to(&mut self, x: f32, y: f32) {
        if let Some(contour) = self.contours.last_mut() {
            contour.push((round(x), round(y), true));
        }
    }
    fn quad_to(&mut self, cx: f32, cy: f32, x: f32, y: f32) {
        if let Some(contour) = self.contours.last_mut() {
            contour.push((round(cx), round(cy), false));
            contour.push((round(x), round(y), true));
        }
    }
    fn curve_to(&mut self, _: f32, _: f32, _: f32, _: f32, _: f32, _: f32) {
        self.cubic = true;
    }
    fn close(&mut self) {
        if let Some(contour) = self.contours.last_mut()
            && contour.len() > 1
            && contour.first().map(|p| (p.0, p.1)) == contour.last().map(|p| (p.0, p.1))
        {
            contour.pop();
        }
    }
}

/// Drops the on-curve points that lie halfway between two off-curve points.
fn drop_implied(contour: &[Point]) -> Vec<Point> {
    let count = contour.len();
    let mut kept = Vec::with_capacity(count);
    for (index, &point) in contour.iter().enumerate() {
        let before = contour[(index + count - 1) % count];
        let after = contour[(index + 1) % count];
        let implied = point.2
            && !before.2
            && !after.2
            && (before.0 + after.0 - 2 * point.0).abs() <= 1
            && (before.1 + after.1 - 2 * point.1).abs() <= 1;
        if !implied {
            kept.push(point);
        }
    }
    kept
}

/// One coordinate column of a simple glyph, and the flag bits it sets.
fn encode_axis(deltas: &[i32], short: u8, same: u8, flags: &mut [u8], out: &mut Vec<u8>) {
    for (&delta, flag) in deltas.iter().zip(flags.iter_mut()) {
        if delta == 0 {
            *flag |= same;
        } else if delta.abs() < 256 {
            *flag |= short;
            if delta > 0 {
                *flag |= same;
            }
            out.push(delta.unsigned_abs() as u8);
        } else {
            out.extend_from_slice(&(delta as i16).to_be_bytes());
        }
    }
}

struct Outline {
    data: Vec<u8>,
    bounds: [i32; 4],
    points: usize,
    contours: usize,
}

/// Writes a simple glyph description; an empty outline writes nothing.
fn encode(contours: &[Vec<Point>]) -> Outline {
    let contours: Vec<_> = contours
        .iter()
        .map(|c| drop_implied(c))
        .filter(|c| !c.is_empty())
        .collect();
    let points: Vec<Point> = contours.iter().flatten().copied().collect();
    if points.is_empty() {
        return Outline {
            data: Vec::new(),
            bounds: [0; 4],
            points: 0,
            contours: 0,
        };
    }
    let bounds = points
        .iter()
        .fold([i32::MAX, i32::MAX, i32::MIN, i32::MIN], |b, p| {
            [b[0].min(p.0), b[1].min(p.1), b[2].max(p.0), b[3].max(p.1)]
        });
    let mut data = Vec::new();
    data.extend_from_slice(&(contours.len() as i16).to_be_bytes());
    for value in bounds {
        data.extend_from_slice(&(value as i16).to_be_bytes());
    }
    let mut end = 0usize;
    for contour in &contours {
        end += contour.len();
        data.extend_from_slice(&((end - 1) as u16).to_be_bytes());
    }
    data.extend_from_slice(&0u16.to_be_bytes());
    let mut flags: Vec<u8> = points.iter().map(|p| u8::from(p.2)).collect();
    let (mut last_x, mut last_y) = (0, 0);
    let (mut dx, mut dy) = (
        Vec::with_capacity(points.len()),
        Vec::with_capacity(points.len()),
    );
    for point in &points {
        dx.push(point.0 - last_x);
        dy.push(point.1 - last_y);
        last_x = point.0;
        last_y = point.1;
    }
    let (mut xs, mut ys) = (Vec::new(), Vec::new());
    encode_axis(&dx, 0x02, 0x10, &mut flags, &mut xs);
    encode_axis(&dy, 0x04, 0x20, &mut flags, &mut ys);
    let mut index = 0;
    while index < flags.len() {
        let flag = flags[index];
        let mut run = 1;
        while index + run < flags.len() && flags[index + run] == flag && run < 256 {
            run += 1;
        }
        if run > 1 {
            data.push(flag | 0x08);
            data.push((run - 1) as u8);
        } else {
            data.push(flag);
        }
        index += run;
    }
    data.extend_from_slice(&xs);
    data.extend_from_slice(&ys);
    data.resize(data.len().next_multiple_of(4), 0);
    Outline {
        data,
        bounds,
        points: points.len(),
        contours: contours.len(),
    }
}

fn checksum(data: &[u8]) -> u32 {
    data.chunks(4).fold(0u32, |sum, chunk| {
        let mut word = [0u8; 4];
        word[..chunk.len()].copy_from_slice(chunk);
        sum.wrapping_add(u32::from_be_bytes(word))
    })
}

/// Assembles an sfnt from tables, sorted by tag.
fn assemble(mut tables: Vec<([u8; 4], Vec<u8>)>) -> Vec<u8> {
    tables.sort_by(|a, b| a.0.cmp(&b.0));
    let count = tables.len() as u16;
    let log = 15 - count.leading_zeros() as u16;
    let mut out = Vec::new();
    out.extend_from_slice(&0x0001_0000u32.to_be_bytes());
    for value in [count, 16 << log, log, count * 16 - (16 << log)] {
        out.extend_from_slice(&value.to_be_bytes());
    }
    let mut offset = 12 + tables.len() * 16;
    let mut head_at = None;
    for (tag, data) in &tables {
        out.extend_from_slice(tag);
        out.extend_from_slice(&checksum(data).to_be_bytes());
        out.extend_from_slice(&(offset as u32).to_be_bytes());
        out.extend_from_slice(&(data.len() as u32).to_be_bytes());
        if tag == b"head" {
            head_at = Some(offset);
        }
        offset += data.len().next_multiple_of(4);
    }
    for (_, data) in &tables {
        out.extend_from_slice(data);
        out.resize(out.len().next_multiple_of(4), 0);
    }
    if let Some(at) = head_at {
        let adjustment = 0xB1B0_AFBAu32.wrapping_sub(checksum(&out));
        out[at + 8..at + 12].copy_from_slice(&adjustment.to_be_bytes());
    }
    out
}

fn table(font: &FontRef<'_>, tag: &[u8; 4]) -> Option<Vec<u8>> {
    font.table_data(Tag::new(tag))
        .map(|data| data.as_bytes().to_vec())
}

fn shape(font: &harfrust::Font, text: &str) -> Vec<(u32, i32)> {
    let mut buffer = Buffer::new();
    buffer.push_str(text);
    buffer.set_direction(Direction::LeftToRight);
    buffer.guess_segment_properties();
    let shaper = ShaperFont::new(font);
    harfrust::shape(&shaper, &mut buffer, ShapeOptions::new()).expect("shape");
    let glyphs = buffer.glyph_infos().iter().map(|info| info.glyph_id);
    let advances = buffer
        .glyph_positions()
        .iter()
        .map(|position| position.x_advance);
    glyphs.zip(advances).collect()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let data = std::fs::read(&args[1]).expect("read the face");
    let axes: Vec<(String, f32)> = args[3..]
        .iter()
        .map(|arg| {
            let (tag, value) = arg.split_once('=').expect("AXIS=VALUE");
            (tag.to_owned(), value.parse().expect("a number"))
        })
        .collect();
    let font = FontRef::new(&data).expect("a font");
    let started = Instant::now();
    let settings: Vec<(&str, f32)> = axes
        .iter()
        .map(|(tag, value)| (tag.as_str(), *value))
        .collect();
    let location: Location = font.axes().location(settings.iter().copied());
    let outlines = font.outline_glyphs();
    let metrics = font.glyph_metrics(Size::unscaled(), &location);
    let glyph_count = font.maxp().expect("maxp").num_glyphs();
    let (mut glyf, mut loca, mut hmtx) = (Vec::new(), Vec::new(), Vec::new());
    let mut bounds = [i32::MAX, i32::MAX, i32::MIN, i32::MIN];
    let (mut max_points, mut max_contours, mut advance_max) = (0usize, 0usize, 0u16);
    let mut cubic = 0u32;
    for id in 0..u32::from(glyph_count) {
        let glyph = GlyphId::new(id);
        let mut pen = Pen::default();
        if let Some(outline) = outlines.get(glyph) {
            outline
                .draw(
                    DrawSettings::unhinted(Size::unscaled(), &location),
                    &mut pen,
                )
                .expect("draw");
        }
        cubic += u32::from(pen.cubic);
        let outline = encode(&pen.contours);
        loca.extend_from_slice(&(glyf.len() as u32).to_be_bytes());
        glyf.extend_from_slice(&outline.data);
        max_points = max_points.max(outline.points);
        max_contours = max_contours.max(outline.contours);
        if outline.contours > 0 {
            bounds = [
                bounds[0].min(outline.bounds[0]),
                bounds[1].min(outline.bounds[1]),
                bounds[2].max(outline.bounds[2]),
                bounds[3].max(outline.bounds[3]),
            ];
        }
        let advance = round(metrics.advance_width(glyph).unwrap_or(0.0)).clamp(0, 65535) as u16;
        advance_max = advance_max.max(advance);
        hmtx.extend_from_slice(&advance.to_be_bytes());
        hmtx.extend_from_slice(&(outline.bounds[0] as i16).to_be_bytes());
    }
    loca.extend_from_slice(&(glyf.len() as u32).to_be_bytes());

    let mut head = table(&font, b"head").expect("head");
    head[8..12].fill(0);
    for (at, value) in [36, 38, 40, 42].into_iter().zip(bounds) {
        head[at..at + 2].copy_from_slice(&(value as i16).to_be_bytes());
    }
    head[50..52].copy_from_slice(&1i16.to_be_bytes());
    let mut hhea = table(&font, b"hhea").expect("hhea");
    hhea[10..12].copy_from_slice(&advance_max.to_be_bytes());
    hhea[34..36].copy_from_slice(&glyph_count.to_be_bytes());
    let mut maxp = vec![0u8; 32];
    maxp[0..4].copy_from_slice(&0x0001_0000u32.to_be_bytes());
    maxp[4..6].copy_from_slice(&glyph_count.to_be_bytes());
    maxp[6..8].copy_from_slice(&(max_points as u16).to_be_bytes());
    maxp[8..10].copy_from_slice(&(max_contours as u16).to_be_bytes());
    maxp[14..16].copy_from_slice(&1u16.to_be_bytes());

    let weight = axes
        .iter()
        .find(|(tag, _)| tag == "wght")
        .map(|(_, value)| *value);
    let mut tables: Vec<([u8; 4], Vec<u8>)> = vec![
        (*b"glyf", glyf),
        (*b"head", head),
        (*b"hhea", hhea),
        (*b"hmtx", hmtx),
        (*b"loca", loca),
        (*b"maxp", maxp),
    ];
    for tag in COPIED {
        if let Some(mut data) = table(&font, tag) {
            if tag == b"OS/2"
                && data.len() > 6
                && let Some(weight) = weight
            {
                data[4..6].copy_from_slice(&(weight.round() as u16).to_be_bytes());
            }
            if tag == b"post" && data.len() >= 32 {
                data.truncate(32);
                data[0..4].copy_from_slice(&0x0003_0000u32.to_be_bytes());
            }
            tables.push((*tag, data));
        }
    }
    let program = assemble(tables);
    let elapsed = started.elapsed();
    std::fs::write(&args[2], &program).expect("write");

    // Shaping at the instance with harfrust's own variations, against
    // shaping the static result.
    let source: Arc<dyn AsRef<[u8]> + Send + Sync> = Arc::new(data.clone());
    let source = harfrust::Font::new(source, 0).expect("shape the source");
    let at_instance = source
        .instance_builder()
        .variations(settings.iter().copied())
        .build();
    let result: Arc<dyn AsRef<[u8]> + Send + Sync> = Arc::new(program.clone());
    let result = harfrust::Font::new(result, 0).expect("shape the result");
    let text = "Hello, office AVA 123 日本語";
    let (a, b) = (shape(&at_instance, text), shape(&result, text));
    let differ = a.iter().zip(&b).filter(|(x, y)| x != y).count() + a.len().abs_diff(b.len());
    for (x, y) in a.iter().zip(&b).filter(|(x, y)| x != y) {
        eprintln!("shaped differently: at the instance {x:?}, static {y:?}");
    }
    println!(
        "{{\"glyphs\":{glyph_count},\"instance_ms\":{:.1},\"source_bytes\":{},\"static_bytes\":{},\"cubic_glyphs\":{cubic},\"shaped\":{},\"shaped_differ\":{differ}}}",
        elapsed.as_secs_f64() * 1000.0,
        data.len(),
        program.len(),
        a.len(),
    );
}
