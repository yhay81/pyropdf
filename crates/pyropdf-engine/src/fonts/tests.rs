//! Invariants of subsetting over the corpus's embedded programs: a subset
//! draws each kept glyph as the program did, draws nothing for the others,
//! is stable when subset again, and never panics on damaged programs.

use std::fs;
use std::path::Path;
use std::sync::Arc;

use proptest::prelude::*;
use pyropdf_store::budget::Budget;
use pyropdf_store::index::Index as FileIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Value};
use pyropdf_store::version::Document;
use skrifa::instance::{LocationRef, Size};
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::raw::TableProvider;
use skrifa::raw::ps::cff::CffFontRef;
use skrifa::raw::types::GlyphId;
use skrifa::{FontRef, MetadataProvider};

use super::{Format, subset};

/// The embedded TrueType, OpenType, and CFF programs of the corpus.
fn programs() -> Vec<(String, Format, Vec<u8>)> {
    let budget = Budget::default();
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
    let mut found = Vec::new();
    let mut paths: Vec<_> = fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    paths.sort();
    for path in paths
        .into_iter()
        .filter(|path| path.extension().is_some_and(|ext| ext == "pdf"))
    {
        let index = FileIndex::open(Bytes::new(fs::read(&path).unwrap()), &budget).unwrap();
        let document = Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None);
        let version = document.current();
        let size = match version.trailer().get("Size") {
            Some(Value::Integer(size)) => u32::try_from(*size).unwrap(),
            _ => 0,
        };
        for number in 1..size {
            let Ok(Some(object)) = version.get(number, &budget) else {
                continue;
            };
            let Value::Stream(stream) = object.value().as_ref() else {
                continue;
            };
            let subtype = stream.dict().get("Subtype").and_then(|value| match value {
                Value::Name(name) => Some(name.as_bytes().to_vec()),
                _ => None,
            });
            let format = match subtype.as_deref() {
                Some(b"Type1C" | b"CIDFontType0C") => Format::Cff,
                Some(b"OpenType") => Format::Sfnt,
                None if stream.dict().get("Length1").is_some()
                    && stream.dict().get("Length2").is_none() =>
                {
                    Format::Sfnt
                }
                _ => continue,
            };
            let name = format!("{} {number}", path.file_name().unwrap().to_string_lossy());
            found.push((name, format, stream.decoded(&budget).unwrap()));
        }
    }
    found
}

#[derive(Default)]
struct Commands(Vec<String>);

impl OutlinePen for Commands {
    fn move_to(&mut self, x: f32, y: f32) {
        self.0.push(format!("M{x},{y}"));
    }
    fn line_to(&mut self, x: f32, y: f32) {
        self.0.push(format!("L{x},{y}"));
    }
    fn quad_to(&mut self, cx0: f32, cy0: f32, x: f32, y: f32) {
        self.0.push(format!("Q{cx0},{cy0},{x},{y}"));
    }
    fn curve_to(&mut self, cx0: f32, cy0: f32, cx1: f32, cy1: f32, x: f32, y: f32) {
        self.0.push(format!("C{cx0},{cy0},{cx1},{cy1},{x},{y}"));
    }
    fn close(&mut self) {
        self.0.push("Z".into());
    }
}

/// Returns the outline commands of every glyph of `data`, `None` for a glyph
/// that does not draw.
fn outlines(format: Format, data: &[u8]) -> Vec<Option<Vec<String>>> {
    match format {
        Format::Sfnt => {
            let font = FontRef::new(data).unwrap();
            let glyphs = font.outline_glyphs();
            (0..font.maxp().map_or(0, |maxp| maxp.num_glyphs()))
                .map(|glyph| {
                    let outline = glyphs.get(GlyphId::new(u32::from(glyph)))?;
                    let mut pen = Commands::default();
                    let settings = DrawSettings::unhinted(Size::unscaled(), LocationRef::default());
                    outline.draw(settings, &mut pen).ok()?;
                    Some(pen.0)
                })
                .collect()
        }
        Format::Cff => {
            let font = CffFontRef::new_cff(data, 0, None).unwrap();
            (0..font.num_glyphs())
                .map(|glyph| {
                    let glyph = GlyphId::new(glyph);
                    let subfont = font.subfont(font.subfont_index(glyph)?, &[]).ok()?;
                    let mut pen = Commands::default();
                    font.draw(&subfont, glyph, &[], None, &mut pen).ok()?;
                    Some(pen.0)
                })
                .collect()
        }
    }
}

#[test]
fn the_corpus_has_programs_of_both_formats() {
    let programs = programs();
    assert!(
        programs
            .iter()
            .any(|(_, format, _)| *format == Format::Sfnt)
    );
    assert!(programs.iter().any(|(_, format, _)| *format == Format::Cff));
}

#[test]
fn a_subset_draws_the_kept_glyphs_as_the_program_did_and_empties_the_others() {
    for (name, format, data) in programs() {
        let before = outlines(format, &data);
        // Keep every other glyph, which composites and accents may extend.
        let keep: Vec<u32> = (0..before.len())
            .step_by(2)
            .map(|glyph| u32::try_from(glyph).unwrap())
            .collect();
        let Some(result) = subset(&data, format, &keep) else {
            panic!("{name}: the subsetter does not read the program");
        };
        assert_eq!(result.glyphs, before.len(), "{name}");
        let after = outlines(format, &result.data);
        assert_eq!(after.len(), before.len(), "{name}");
        for (glyph, (before, after)) in before.iter().zip(&after).enumerate() {
            let asked = glyph == 0 || keep.contains(&u32::try_from(glyph).unwrap());
            // A dropped glyph draws nothing, unless a kept glyph draws it.
            if asked || after.as_ref().is_some_and(|after| !after.is_empty()) {
                assert_eq!(before, after, "{name}: glyph {glyph}");
            }
        }
        assert!(result.kept >= keep.len().min(result.glyphs), "{name}");
    }
}

#[test]
fn keeping_every_glyph_draws_the_program_unchanged() {
    for (name, format, data) in programs() {
        let before = outlines(format, &data);
        let all: Vec<u32> = (0..u32::try_from(before.len()).unwrap()).collect();
        let result = subset(&data, format, &all).unwrap();
        assert_eq!(result.kept, before.len(), "{name}");
        assert_eq!(outlines(format, &result.data), before, "{name}");
    }
}

#[test]
fn subsetting_a_subset_again_changes_nothing() {
    for (name, format, data) in programs() {
        let once = subset(&data, format, &[1, 2, 3]).unwrap();
        let twice = subset(&once.data, format, &[1, 2, 3]).unwrap();
        assert_eq!(twice, once, "{name}");
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]
    #[test]
    fn a_damaged_program_never_panics(pick in any::<prop::sample::Index>(), cut in any::<prop::sample::Index>(), flips in prop::collection::vec((any::<prop::sample::Index>(), any::<u8>()), 0..16)) {
        let programs = programs_once();
        let (_, format, data) = pick.get(programs);
        let mut data = data[..cut.index(data.len() + 1)].to_vec();
        for (at, byte) in flips {
            if !data.is_empty() {
                let at = at.index(data.len());
                data[at] = byte;
            }
        }
        let _ = subset(&data, *format, &[0, 1, 2, 3, 10, 100]);
    }
}

fn programs_once() -> &'static [(String, Format, Vec<u8>)] {
    static PROGRAMS: std::sync::OnceLock<Vec<(String, Format, Vec<u8>)>> =
        std::sync::OnceLock::new();
    PROGRAMS.get_or_init(programs)
}
