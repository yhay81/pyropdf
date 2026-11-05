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

/// The tables of a TrueType program of `glyphs` glyphs, whose one outline of
/// `outline` bytes each glyph's range of `loca` addresses as `offsets` gives.
fn truetype_tables(
    glyphs: u16,
    outline: usize,
    offsets: impl Fn(usize) -> u32,
) -> Vec<([u8; 4], Vec<u8>)> {
    let mut head = vec![0; 54];
    // indexToLocFormat: 32-bit offsets.
    head[50..52].copy_from_slice(&1_u16.to_be_bytes());
    let mut maxp = vec![0, 0, 0x50, 0];
    maxp.extend_from_slice(&glyphs.to_be_bytes());
    let mut hhea = vec![0; 36];
    // numberOfHMetrics: one long metric, then a bearing per glyph.
    hhea[34..36].copy_from_slice(&1_u16.to_be_bytes());
    let hmtx = vec![0; 4 + 2 * (usize::from(glyphs) - 1)];
    // A simple glyph: one contour, and the rest of its bytes.
    let mut glyf = vec![0; outline];
    glyf[0..2].copy_from_slice(&1_u16.to_be_bytes());
    let loca = (0..=usize::from(glyphs))
        .flat_map(|at| offsets(at).to_be_bytes())
        .collect();
    vec![
        (*b"glyf", glyf),
        (*b"head", head),
        (*b"hhea", hhea),
        (*b"hmtx", hmtx),
        (*b"loca", loca),
        (*b"maxp", maxp),
    ]
}

#[test]
fn a_program_whose_tables_overlap_is_left_whole() {
    // Records that all address one large table would copy it once each.
    let mut tables = truetype_tables(2, 12, |at| if at == 0 { 0 } else { 12 });
    let whole = super::sfnt::write(&[0, 1, 0, 0], &tables).unwrap();
    assert!(subset(&whole, Format::Sfnt, &[1]).is_some());
    let shared = 16;
    tables.extend((0..shared).map(|at: u8| ([b'x', b'0', b'a' + at, b' '], vec![0; 4])));
    tables.push((*b"xbig", vec![0; 4096]));
    let shared = usize::from(shared);
    let mut data = super::sfnt::write(&[0, 1, 0, 0], &tables).unwrap();
    let record = |at: usize| 12 + 16 * at;
    let big = record(tables.len() - 1);
    let (offset, length) = (big + 8, big + 12);
    let location: [u8; 8] = data[offset..length + 4].try_into().unwrap();
    for at in tables.len() - 1 - shared..tables.len() - 1 {
        data[record(at) + 8..record(at) + 16].copy_from_slice(&location);
    }
    assert!(subset(&data, Format::Sfnt, &[1]).is_none());
}

#[test]
fn duplicate_table_tags_cannot_multiply_rebuilt_tables() {
    // Empty duplicate records pass the sum of input table lengths, but a
    // rewrite used to replace each of them with the first table's bytes.
    let mut tables = truetype_tables(2, 4096, |at| if at == 0 { 0 } else { 4096 });
    let whole = super::sfnt::write(&[0, 1, 0, 0], &tables).unwrap();
    assert!(subset(&whole, Format::Sfnt, &[1]).is_some());
    tables.extend(std::iter::repeat_n((*b"glyf", Vec::new()), 32));
    let data = super::sfnt::write(&[0, 1, 0, 0], &tables).unwrap();
    assert!(subset(&data, Format::Sfnt, &[1]).is_none());
    assert!(super::sfnt::compact(&data, &[1].into_iter().collect()).is_none());
}

#[test]
fn a_program_whose_glyphs_overlap_is_left_whole() {
    // Offsets that return to the start give every other glyph the whole
    // outline, which a rewrite would copy once for each.
    let (glyphs, outline) = (64, 4096_u32);
    let apart = truetype_tables(glyphs, 4096, |at| if at == 0 { 0 } else { outline });
    let apart = super::sfnt::write(&[0, 1, 0, 0], &apart).unwrap();
    let overlapping = truetype_tables(glyphs, 4096, |at| if at % 2 == 0 { 0 } else { outline });
    let overlapping = super::sfnt::write(&[0, 1, 0, 0], &overlapping).unwrap();
    let all: Vec<u32> = (0..u32::from(glyphs)).collect();
    assert!(subset(&apart, Format::Sfnt, &all).is_some());
    assert!(subset(&overlapping, Format::Sfnt, &all).is_none());
    let all = all.into_iter().collect();
    assert!(super::sfnt::compact(&apart, &all).is_some());
    assert!(super::sfnt::compact(&overlapping, &all).is_none());
}

#[test]
fn a_cff_whose_font_dicts_share_a_private_dict_is_left_whole() {
    // Font DICTs that all address one large Private DICT would copy it once
    // each.
    let fonts = 8_u8;
    // Forty BlueValues entries of forty zero operands each.
    let big: Vec<u8> = (0..40)
        .flat_map(|_| [[139; 40].as_slice(), &[6]].concat())
        .collect();
    let mut privates = vec![big.clone()];
    privates.resize(usize::from(fonts), Vec::new());
    let charstrings = vec![vec![14]; usize::from(fonts)];
    let cids: Vec<u16> = (0..u16::from(fonts)).collect();
    let fds: Vec<u8> = (0..fonts).collect();
    let mut data = super::cff::cid_keyed("Shared", &charstrings, &cids, &fds, &privates).unwrap();
    assert!(subset(&data, Format::Cff, &[1]).is_some());
    // Each Font DICT is its Private entry: two five-byte integers and the
    // operator, the first one's size being the large DICT's with its
    // default width.
    let size = u32::try_from(big.len() + 6).unwrap().to_be_bytes();
    let first = data
        .windows(11)
        .position(|dict| dict[0] == 29 && dict[1..5] == size && dict[5] == 29 && dict[10] == 18)
        .unwrap();
    let dict: [u8; 11] = data[first..first + 11].try_into().unwrap();
    for at in 1..usize::from(fonts) {
        let at = first + 11 * at;
        assert!(data[at] == 29 && data[at + 10] == 18);
        data[at..at + 11].copy_from_slice(&dict);
    }
    assert!(subset(&data, Format::Cff, &[1]).is_none());
}
