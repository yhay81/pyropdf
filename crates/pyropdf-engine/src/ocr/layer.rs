//! The invisible text layer of `Page.apply_ocr`, ported from pylopdf's
//! `rust/src/ocr.rs` at 036be111cafe77755db4bb2be17bc70a8f2f9d82.
//!
//! Following ocrmypdf, each character gets a CID in a non-embedded
//! Identity-H font with a ToUnicode CMap, and the words are shown in the
//! invisible rendering mode (`3 Tr`). They appear only in extraction and
//! search, and add almost no file size in any language.

use std::collections::HashMap;
use std::fmt;

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};

use super::Error;

/// One word of a layer: its box in display space and its text.
pub type LayerWord = (f64, f64, f64, f64, String);

/// The most words with text of one layer.
pub const MAX_LAYER_WORDS: usize = 4_096;
/// The most UTF-8 bytes of the text of one layer.
pub const MAX_LAYER_TEXT_BYTES: usize = 1024 * 1024;
/// The most distinct characters of one layer; CID 0 is `.notdef`.
const MAX_OCR_CIDS: usize = 65_534;

/// UTF-8 output whose allocation and formatting are charged before growth.
struct Output<'b> {
    bytes: BudgetVec<'b, u8>,
}

impl<'b> Output<'b> {
    fn new(budget: &'b Budget) -> Self {
        Self {
            bytes: BudgetVec::new(budget),
        }
    }

    fn push_str(&mut self, text: &str) -> Result<(), Error> {
        self.bytes
            .try_extend_from_slice(text.as_bytes())
            .map_err(Error::Limit)
    }

    fn write_fmt(&mut self, arguments: fmt::Arguments<'_>) -> Result<(), Error> {
        struct Formatter<'a, 'b> {
            bytes: &'a mut BudgetVec<'b, u8>,
            refusal: Option<LimitExceeded>,
        }
        impl fmt::Write for Formatter<'_, '_> {
            fn write_str(&mut self, text: &str) -> fmt::Result {
                self.bytes
                    .try_extend_from_slice(text.as_bytes())
                    .map_err(|refusal| {
                        self.refusal = Some(refusal);
                        fmt::Error
                    })
            }
        }
        let mut formatter = Formatter {
            bytes: &mut self.bytes,
            refusal: None,
        };
        fmt::write(&mut formatter, arguments).map_err(|_| {
            formatter.refusal.map_or_else(
                || Error::Failed("failed to format OCR layer output".to_owned()),
                Error::Limit,
            )
        })
    }

    fn into_bytes(self) -> Vec<u8> {
        self.bytes.into_vec()
    }
}

/// The ToUnicode CMap and the content of one layer.
#[derive(Debug)]
pub struct Layer {
    /// The CMap that maps each CID to its character.
    pub to_unicode: Vec<u8>,
    /// The content stream that shows the words, balanced in `q` and `Q`.
    pub content: Vec<u8>,
}

/// Builds the layer of `words` on a page whose display box is `crop`, in
/// unrotated user space, and whose `/Rotate` is `page_rotation`.
///
/// `text_rotation` is the clockwise correction that read the words, which
/// turns their baselines; `font_name` is the font's resource name.
pub fn layer(
    words: &[LayerWord],
    crop: [f64; 4],
    page_rotation: i64,
    text_rotation: u16,
    font_name: &str,
    budget: &Budget,
) -> Result<Layer, Error> {
    if !matches!(text_rotation, 0 | 90 | 180 | 270) {
        return Err(Error::Failed(
            "rotation must be 0, 90, 180, or 270".to_owned(),
        ));
    }
    budget.check(LimitCode::OcrLayerWordCount, words.len() as u64)?;
    for word in words {
        budget.charge(LimitCode::OcrLayerTextSize, word.4.len() as u64)?;
    }
    let cid_map = assign_cids(words, budget)?;
    Ok(Layer {
        to_unicode: build_to_unicode(&cid_map, budget)?,
        content: ocr_ops(
            crop,
            page_rotation,
            words,
            &cid_map,
            font_name,
            text_rotation,
            budget,
        )?,
    })
}

/// Assigns one-based CIDs to all characters; CID 0 is `.notdef`.
fn assign_cids(words: &[LayerWord], budget: &Budget) -> Result<HashMap<char, u16>, Error> {
    let mut map = HashMap::new();
    budget.charge(LimitCode::MemorySize, 64)?;
    for (_, _, _, _, text) in words {
        for ch in text.chars() {
            budget.poll()?;
            if map.contains_key(&ch) {
                continue;
            }
            budget.charge(LimitCode::OcrLayerCharacterCount, 1)?;
            if map.len() >= MAX_OCR_CIDS {
                return Err(LimitExceeded::over(
                    LimitCode::OcrLayerCharacterCount,
                    MAX_OCR_CIDS as u64,
                    map.len() as u64 + 1,
                )
                .into());
            }
            // Covers bucket overhead and geometric spare capacity.
            budget.charge(LimitCode::MemorySize, 32)?;
            map.try_reserve(1)
                .map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
            let cid =
                u16::try_from(map.len() + 1).expect("the OCR CID count is bounded below u16::MAX");
            map.insert(ch, cid);
        }
    }
    Ok(map)
}

/// Builds a CID-to-Unicode UTF-16BE ToUnicode CMap, with `bfchar` in
/// blocks of 100 entries as the specification requires.
fn build_to_unicode(cid_map: &HashMap<char, u16>, budget: &Budget) -> Result<Vec<u8>, Error> {
    let mut entries = BudgetVec::new(budget);
    entries.try_extend(cid_map.iter().map(|(&ch, &cid)| (cid, ch)))?;
    entries.sort_unstable();
    let mut out = Output::new(budget);
    out.push_str(
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
         /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n\
         /CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n\
         1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n",
    )?;
    for block in entries.chunks(100) {
        writeln!(out, "{} beginbfchar", block.len())?;
        for &(cid, ch) in block {
            write!(out, "<{cid:04X}> <")?;
            let mut buf = [0u16; 2];
            for unit in ch.encode_utf16(&mut buf) {
                write!(out, "{unit:04X}")?;
            }
            out.push_str(">\n")?;
        }
        out.push_str("endbfchar\n")?;
    }
    out.push_str("endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")?;
    Ok(out.into_bytes())
}

/// Builds the operators that show the words invisibly (`3 Tr`).
///
/// The font size follows the box across the baseline, the baseline sits a
/// 0.2 descent inside the box, and a horizontal scale fits the box along
/// it.
fn ocr_ops(
    crop: [f64; 4],
    page_rotation: i64,
    words: &[LayerWord],
    cid_map: &HashMap<char, u16>,
    font_name: &str,
    text_rotation: u16,
    budget: &Budget,
) -> Result<Vec<u8>, Error> {
    let mut out = Output::new(budget);
    out.push_str("q\n")?;
    for (x0, y0, x1, y1, text) in words {
        let (w, h) = (x1 - x0, y1 - y0);
        #[expect(clippy::cast_precision_loss, reason = "a word has few characters")]
        let chars = text.chars().count() as f64;
        let (origin, baseline, up, font_size, line_length) = match text_rotation {
            90 => ((x1 - 0.2 * w, *y1), (0.0, -1.0), (-1.0, 0.0), w, h),
            180 => ((*x1, y0 + 0.2 * h), (-1.0, 0.0), (0.0, 1.0), h, w),
            270 => ((x0 + 0.2 * w, *y0), (0.0, 1.0), (1.0, 0.0), w, h),
            _ => ((*x0, y1 - 0.2 * h), (1.0, 0.0), (0.0, -1.0), h, w),
        };
        let (ox, oy) = display_to_pdf(crop, page_rotation, origin.0, origin.1);
        let (rx, ry) = {
            let p = display_to_pdf(
                crop,
                page_rotation,
                origin.0 + baseline.0,
                origin.1 + baseline.1,
            );
            (p.0 - ox, p.1 - oy)
        };
        let (ux, uy) = {
            let p = display_to_pdf(crop, page_rotation, origin.0 + up.0, origin.1 + up.1);
            (p.0 - ox, p.1 - oy)
        };
        // Scale the natural width of one em per character to the box.
        let sx = line_length / (chars * font_size);
        write!(
            out,
            "BT\n/{font_name} {} Tf\n3 Tr\n{} {} {} {} {} {} Tm\n<",
            fmt(font_size),
            fmt(sx * rx),
            fmt(sx * ry),
            fmt(ux),
            fmt(uy),
            fmt(ox),
            fmt(oy),
        )?;
        for ch in text.chars() {
            let cid = cid_map.get(&ch).copied().unwrap_or(0);
            write!(out, "{cid:04X}")?;
        }
        out.push_str("> Tj\nET\n")?;
    }
    out.push_str("Q\n")?;
    Ok(out.into_bytes())
}

/// Maps a display point, from the top left with y downward, to unrotated
/// user space on a page whose display box is `crop` and whose `/Rotate`,
/// normalized, is `rotation`. PDF displays a rotation clockwise.
fn display_to_pdf(crop: [f64; 4], rotation: i64, x: f64, y: f64) -> (f64, f64) {
    let [cx0, cy0, cx1, cy1] = crop;
    match rotation {
        90 => (cx0 + y, cy0 + x),
        180 => (cx1 - x, cy0 + y),
        270 => (cx1 - y, cy1 - x),
        _ => (cx0 + x, cy1 - y),
    }
}

/// Formats a finite number without rounding away a narrow text matrix.
/// Rust's display form uses decimal digits without PDF-invalid exponents.
fn fmt(v: f64) -> String {
    if v == 0.0 {
        "0".to_owned()
    } else {
        v.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_cmap_maps_each_cid_to_its_character_in_blocks_of_100() {
        let text: String = ('A'..='z').chain(['日', '𠀋']).collect();
        let words = [(0.0, 0.0, 10.0, 10.0, text.clone())];
        let layer = layer(
            &words,
            [0.0, 0.0, 100.0, 100.0],
            0,
            0,
            "F",
            &Budget::default(),
        )
        .unwrap();
        let cmap = String::from_utf8(layer.to_unicode).unwrap();

        assert!(cmap.contains("<0001> <0041>\n"));
        assert!(cmap.contains(&format!("<{:04X}> <D840DC0B>\n", text.chars().count())));
        assert_eq!(cmap.matches("beginbfchar").count(), 1);
    }

    #[test]
    fn the_content_is_balanced_and_invisible() {
        let words = [(10.0, 20.0, 50.0, 30.0, "ab".to_owned())];
        let content = layer(
            &words,
            [0.0, 0.0, 200.0, 100.0],
            0,
            0,
            "OCR",
            &Budget::default(),
        )
        .unwrap();
        let content = String::from_utf8(content.content).unwrap();

        assert_eq!(
            content,
            "q\nBT\n/OCR 10 Tf\n3 Tr\n2 0 0 1 10 72 Tm\n<00010002> Tj\nET\nQ\n"
        );
    }

    #[test]
    fn a_display_point_maps_through_each_rotation() {
        let crop = [10.0, 20.0, 110.0, 220.0];
        assert_eq!(display_to_pdf(crop, 0, 1.0, 2.0), (11.0, 218.0));
        assert_eq!(display_to_pdf(crop, 90, 1.0, 2.0), (12.0, 21.0));
        assert_eq!(display_to_pdf(crop, 180, 1.0, 2.0), (109.0, 22.0));
        assert_eq!(display_to_pdf(crop, 270, 1.0, 2.0), (108.0, 219.0));
        assert_eq!(fmt(-0.00001), "-0.00001");
        assert_eq!(fmt(1.23456), "1.23456");
    }

    #[test]
    fn a_layer_refuses_too_many_characters() {
        let text: String = (1..=0x1_1000)
            .filter_map(char::from_u32)
            .take(65_535)
            .collect();
        let words = [(0.0, 0.0, 10.0, 10.0, text)];
        let error = layer(
            &words,
            [0.0, 0.0, 100.0, 100.0],
            0,
            0,
            "F",
            &Budget::default(),
        )
        .unwrap_err();

        assert!(
            matches!(error, Error::Limit(refusal) if refusal.code() == LimitCode::OcrLayerCharacterCount)
        );
    }
    #[test]
    fn layer_construction_observes_the_operation_limits() {
        let words = [(0.0, 0.0, 10.0, 10.0, "ab".to_owned())];
        for (code, limit) in [
            (LimitCode::OcrLayerWordCount, 0),
            (LimitCode::OcrLayerTextSize, 1),
            (LimitCode::OcrLayerCharacterCount, 1),
            (LimitCode::MemorySize, 1),
        ] {
            let budget = Budget::default().with_limit(code, Some(limit));
            let error = layer(&words, [0.0, 0.0, 100.0, 100.0], 0, 0, "F", &budget).unwrap_err();
            assert!(matches!(error, Error::Limit(refusal) if refusal.code() == code));
        }
        let budget = Budget::default().with_timeout(std::time::Duration::ZERO);
        let error = layer(&words, [0.0, 0.0, 100.0, 100.0], 0, 0, "F", &budget).unwrap_err();
        assert!(matches!(error, Error::Limit(refusal) if refusal.code() == LimitCode::Deadline));
    }
}
