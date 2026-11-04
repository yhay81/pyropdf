//! Checks and paints every COLR glyph of a face, each in a document of its
//! own under the default limits, as canvas text paints it (DESIGN.md section
//! 7, issue #633). Prints a JSON summary: what each glyph's check says, whether
//! its paint agrees, and what the painted glyphs write.
//!
//! `cargo run --release --locked --manifest-path evidence/colr-glyphs/paint/Cargo.toml -- FACE`

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Instant;

use pyropdf_engine::content::shading::Shadings;
use pyropdf_engine::fonts::colr::{Colr, ColrError, Verdict};
use pyropdf_store::budget::{Budget, DocumentLimits};
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Name, Value};
use pyropdf_store::version::Document;
use skrifa::instance::{LocationRef, Size};
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::raw::TableProvider;
use skrifa::{FontRef, GlyphId, MetadataProvider};

const PDF: &[u8] = b"%PDF-1.7\n1 0 obj\n<</Type/Catalog/Pages 2 0 R>>\nendobj\n\
2 0 obj\n<</Type/Pages/Count 0/Kids[]>>\nendobj\n\
trailer\n<</Root 1 0 R/Size 3>>\n%%EOF\n";

/// Counts the points of an outline.
#[derive(Default)]
struct Points(usize);

impl OutlinePen for Points {
    fn move_to(&mut self, _: f32, _: f32) {
        self.0 += 1;
    }
    fn line_to(&mut self, _: f32, _: f32) {
        self.0 += 1;
    }
    fn quad_to(&mut self, _: f32, _: f32, _: f32, _: f32) {
        self.0 += 2;
    }
    fn curve_to(&mut self, _: f32, _: f32, _: f32, _: f32, _: f32, _: f32) {
        self.0 += 3;
    }
    fn close(&mut self) {}
}

/// What one painted glyph wrote.
struct Written {
    objects: usize,
    stream_bytes: usize,
    micros: u128,
}

fn percentile(sorted: &[usize], at: f64) -> usize {
    if sorted.is_empty() {
        return 0;
    }
    let index = ((sorted.len() - 1) as f64 * at).round() as usize;
    sorted[index]
}

fn quote(text: &str) -> String {
    format!("\"{}\"", text.replace('"', "\\\""))
}

fn main() {
    let path = std::env::args().nth(1).expect("the path of a face");
    let data = std::fs::read(&path).unwrap();
    let font = FontRef::new(&data).unwrap();
    let colr = Colr::read(&font).expect("a face with COLR and outlines");
    let outlines = font.outline_glyphs();
    let glyph_count = u32::from(font.maxp().unwrap().num_glyphs());

    let mut verdicts: BTreeMap<String, usize> = BTreeMap::new();
    let mut disagreements = Vec::new();
    let mut written = Vec::new();
    let mut fallback = [0_usize; 2];
    let started = Instant::now();
    for id in 0..glyph_count {
        let glyph = GlyphId::new(id);
        let budget = Budget::new(&DocumentLimits::default());
        let checked = Instant::now();
        let Some(verdict) = colr.check(glyph, &budget).unwrap() else {
            continue;
        };
        let has_outline = outlines.get(glyph).is_some_and(|outline| {
            let mut points = Points::default();
            let settings = DrawSettings::unhinted(Size::unscaled(), LocationRef::default());
            outline.draw(settings, &mut points).is_ok() && points.0 >= 3
        });
        let key = match verdict {
            Verdict::Paints => "paints".to_owned(),
            Verdict::Cannot(paint) => {
                fallback[usize::from(has_outline)] += 1;
                format!("cannot: {paint}")
            }
            Verdict::Damaged(reason) => format!("damaged: {reason}"),
        };
        *verdicts.entry(key).or_default() += 1;

        let base = FileBase::open(Bytes::new(PDF.to_vec()), &budget).unwrap();
        let document = Document::new(Arc::new(base), None);
        let mut transaction = document.begin();
        let mut shadings = Shadings::new();
        let name = Name::from(format!("g{id}c").as_str());
        let painted = colr.paint(&mut transaction, &mut shadings, glyph, name, 1000.0, &budget);
        let micros = checked.elapsed().as_micros();
        match (verdict, painted) {
            (Verdict::Paints, Ok(painted)) => {
                let next = transaction.allocate().unwrap().number();
                let version = transaction.version();
                let mut stream_bytes = painted.procedure.len();
                for number in 3..next {
                    if let Some(object) = version.get(number, &budget).unwrap()
                        && let Value::Stream(stream) = &**object.value()
                    {
                        stream_bytes += stream.data().len();
                    }
                }
                written.push(Written {
                    objects: (next - 3) as usize,
                    stream_bytes,
                    micros,
                });
            }
            (Verdict::Paints, Err(error)) => disagreements.push((id, format!("{error}"))),
            (_, Ok(_)) => disagreements.push((id, "painted what the check refused".to_owned())),
            (_, Err(ColrError::Refused(_))) => {}
            (_, Err(error)) => disagreements.push((id, format!("{error}"))),
        }
    }
    let seconds = started.elapsed().as_secs_f64();

    let mut objects: Vec<usize> = written.iter().map(|glyph| glyph.objects).collect();
    let mut bytes: Vec<usize> = written.iter().map(|glyph| glyph.stream_bytes).collect();
    let mut micros: Vec<usize> = written.iter().map(|glyph| glyph.micros as usize).collect();
    objects.sort_unstable();
    bytes.sort_unstable();
    micros.sort_unstable();
    let total: usize = verdicts.values().sum();
    println!("{{");
    println!("  \"face\": {},", quote(&path));
    println!("  \"glyphs\": {glyph_count},");
    println!("  \"colour_glyphs\": {total},");
    println!("  \"verdicts\": {{");
    let lines: Vec<String> = verdicts
        .iter()
        .map(|(key, count)| format!("    {}: {count}", quote(key)))
        .collect();
    println!("{}", lines.join(",\n"));
    println!("  }},");
    println!(
        "  \"cannot_with_fallback_outline\": {}, \"cannot_without_outline\": {},",
        fallback[1], fallback[0]
    );
    println!("  \"painted\": {},", written.len());
    let lines: Vec<String> = disagreements
        .iter()
        .map(|(id, reason)| format!("[{id}, {}]", quote(reason)))
        .collect();
    println!("  \"disagreements\": [{}],", lines.join(", "));
    for (name, values) in [("objects", &objects), ("stream_bytes", &bytes), ("micros", &micros)] {
        println!(
            "  \"{name}\": {{\"median\": {}, \"p95\": {}, \"max\": {}, \"sum\": {}}},",
            percentile(values, 0.5),
            percentile(values, 0.95),
            values.last().copied().unwrap_or(0),
            values.iter().sum::<usize>()
        );
    }
    println!("  \"seconds\": {seconds:.2}");
    println!("}}");
}
