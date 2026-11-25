//! Type 1 font subsetting and CMap parsing/authoring (DESIGN.md F6, F9).
//!
//! - Type 1 programs (both PDF `FontFile` streams and PFB-framed inputs) are
//!   subset via `pyropdf_engine::fonts::type1::subset`, checking size, glyph
//!   count, re-parseability, and idempotency invariants.
//! - CMap streams are parsed with `hayro_font::hayro_cmap::CMap::parse` (with
//!   `lookup_cid_code` and `lookup_bf_string` probes) and fed through the
//!   engine's Type 0 form-font CMap/ToUnicode pipeline (`forms::appearance` ->
//!   `rich_codes::stream_codes` & `read_unicode`).
//! - ToUnicode CMaps (`ocr::layer::layer`) and PostScript CID encoding CMaps
//!   are authored from input bytes and round-tripped back through `CMap::parse`.

#![no_main]

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use hayro_font::hayro_cmap::{BfString, CMap};
use libfuzzer_sys::fuzz_target;
use pyropdf_engine::annotations::Reader;
use pyropdf_engine::fonts::type1;
use pyropdf_engine::forms;
use pyropdf_engine::ocr;
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Value};
use pyropdf_store::version::Document;
use read_fonts::ps::type1::Type1Font;

/// Small valid TrueType font used as the CIDFontType2 backing program when
/// exercising the Type 0 CMap/ToUnicode parser through `forms::appearance`.
const TEMPLATE_TTF: &[u8] =
    include_bytes!("../../crates/pyropdf-engine/src/fonts/testdata/variable-latin.ttf");
const MAX_CMAP_INPUT: usize = 64 * 1024;
const MAX_RICH_CMAP_INPUT: usize = 32 * 1024;

fn fuzz_budget() -> Budget {
    let mut limits = DocumentLimits::web();
    limits.max_memory_size = Some(64 * 1024 * 1024);
    limits.max_decompressed_size = Some(4 * 1024 * 1024);
    limits.max_total_decompressed_size = Some(8 * 1024 * 1024);
    limits.max_duration = Some(Duration::from_secs(2));
    Budget::new(&limits)
}

/// Unpack a PFB-framed Type 1 font (`0x80 0x01` ASCII / `0x80 0x02` binary)
/// into PDF `FontFile` bytes plus `(Length1, Length2)`.
fn unpack_pfb(data: &[u8]) -> Option<(Vec<u8>, usize, usize)> {
    if data.len() < 6 || data[0] != 0x80 || data[1] != 0x01 {
        return None;
    }
    let mut pos = 0usize;
    let mut out = Vec::with_capacity(data.len());
    let mut lengths = [0usize; 3];
    let mut seg = 0usize;
    while pos < data.len() {
        if *data.get(pos)? != 0x80 {
            return None;
        }
        let kind = *data.get(pos + 1)?;
        if kind == 0x03 {
            break;
        }
        if !matches!(kind, 0x01 | 0x02) || seg >= 3 {
            return None;
        }
        let len = u32::from_le_bytes(data.get(pos + 2..pos + 6)?.try_into().ok()?) as usize;
        let body = data.get(pos + 6..pos + 6 + len)?;
        out.extend_from_slice(body);
        lengths[seg] = len;
        seg += 1;
        pos += 6 + len;
    }
    (lengths[0] > 0 && lengths[1] > 0).then_some((out, lengths[0], lengths[1]))
}

/// Locate `(clear, encrypted)` segment boundaries around `eexec` in a Type 1 stream.
fn find_eexec_segments(data: &[u8]) -> Option<(usize, usize)> {
    let search_end = data.len().min(65_536);
    let at = data[..search_end].windows(5).position(|w| w == b"eexec")?;
    let mut clear = at + 5;
    if clear < data.len() && data[clear] == b'\r' {
        clear += 1;
        if clear < data.len() && data[clear] == b'\n' {
            clear += 1;
        }
    } else if clear < data.len() && matches!(data[clear], b'\n' | b' ' | b'\t') {
        clear += 1;
    }
    if clear >= data.len() {
        return None;
    }
    let rest = &data[clear..];
    // Find `cleartomark` trailer (preceded by optional ASCII zeros/whitespace).
    let encrypted = if let Some(ctm) = rest.windows(11).rposition(|w| w == b"cleartomark") {
        let mut end = ctm;
        while end > 0 && matches!(rest[end - 1], b'0' | b' ' | b'\t' | b'\r' | b'\n') {
            end -= 1;
        }
        if end >= 4 { end } else { rest.len() }
    } else {
        rest.len()
    };
    Some((clear, encrypted))
}

fn check_type1_subset(
    program: &[u8],
    clear: usize,
    encrypted: usize,
    keep: &[u32],
    budget: &Budget,
) {
    let Ok(Some(sub)) = type1::subset(program, clear, encrypted, keep, budget) else {
        return;
    };
    assert!(sub.glyphs > 0, "subsetted Type 1 font must have glyphs");
    assert!(
        sub.kept >= 1 && sub.kept <= sub.glyphs,
        "kept glyph count {} out of 1..={}",
        sub.kept,
        sub.glyphs
    );
    assert!(
        sub.data.len() <= program.len(),
        "Type 1 subset outgrew input ({} > {})",
        sub.data.len(),
        program.len()
    );
    let reparsed = Type1Font::new(&sub.data).expect("subsetted Type 1 font parses again");
    assert_eq!(reparsed.num_glyphs() as usize, sub.glyphs);
    // Idempotency: subsetting the subset with the same kept glyphs changes nothing.
    let shrink = program.len() - sub.data.len();
    if let Some(new_encrypted) = encrypted.checked_sub(shrink)
        && let Ok(Some(twice)) = type1::subset(&sub.data, clear, new_encrypted, keep, budget)
    {
        assert_eq!(twice, sub, "Type 1 subsetting must be idempotent");
    }
}

fn fuzz_type1(data: &[u8], budget: &Budget) {
    let keep: Vec<u32> = data.iter().rev().take(16).map(|&b| u32::from(b)).collect();

    if let Some((flat, len1, len2)) = unpack_pfb(data) {
        check_type1_subset(&flat, len1, len2, &keep, budget);
    }
    if let Some((clear, encrypted)) = find_eexec_segments(data) {
        check_type1_subset(data, clear, encrypted, &keep, budget);
    }
    // Also probe raw / edge-case segment lengths derived from the input tail.
    if data.len() >= 4 {
        let clear = usize::from(u16::from_le_bytes([data[0], data[1]]));
        let encrypted = usize::from(u16::from_le_bytes([data[2], data[3]]));
        let _ = type1::subset(data, clear, encrypted, &keep, budget);
    }
}

/// Static PDF prefix containing objects 1..=7 (Catalog, Pages, Page, Widget,
/// Type0 font, CIDFontType2, FontDescriptor, and FontFile2), leaving objects 9
/// (`/Encoding` CMap) and 10 (`/ToUnicode` CMap) to be appended per iteration.
struct Type0Template {
    prefix: Vec<u8>,
    offsets: [usize; 8],
}

fn type0_template() -> &'static Type0Template {
    static CACHE: OnceLock<Type0Template> = OnceLock::new();
    CACHE.get_or_init(|| {
        let mut out = b"%PDF-1.7\n".to_vec();
        let mut offsets = [0usize; 8];
        let mut push_obj = |idx: usize, body: &[u8]| {
            offsets[idx] = out.len();
            out.extend_from_slice(format!("{} 0 obj\n", idx + 1).as_bytes());
            out.extend_from_slice(body);
            out.extend_from_slice(b"\nendobj\n");
        };
        push_obj(
            0,
            b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R] /DR << /Font << /FuzzFont 5 0 R >> >> /DA (/Helv 11 Tf 0 g) >> >>",
        );
        push_obj(1, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>");
        push_obj(
            2,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Annots [4 0 R] >>",
        );
        push_obj(
            3,
            b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (cmap) /Ff 33558528 /Rect [4 4 190 80] /RV (<body xmlns=\"http://www.w3.org/1999/xhtml\"><p style=\"font-family:FuzzFont\">AB</p></body>) >>",
        );
        push_obj(
            4,
            b"<< /Type /Font /Subtype /Type0 /BaseFont /FuzzFont /Encoding 9 0 R /ToUnicode 10 0 R /DescendantFonts [6 0 R] >>",
        );
        push_obj(
            5,
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /FuzzFont /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /FontDescriptor 7 0 R /CIDToGIDMap /Identity >>",
        );
        push_obj(
            6,
            b"<< /Type /FontDescriptor /FontName /FuzzFont /FontFamily (FuzzFont) /Flags 32 /FontBBox [0 0 1000 1000] /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile2 8 0 R >>",
        );
        offsets[7] = out.len();
        out.extend_from_slice(
            format!("8 0 obj\n<< /Length {} >>\nstream\n", TEMPLATE_TTF.len()).as_bytes(),
        );
        out.extend_from_slice(TEMPLATE_TTF);
        out.extend_from_slice(b"\nendstream\nendobj\n");
        Type0Template {
            prefix: out,
            offsets,
        }
    })
}

fn fuzz_cmap_parsing(data: &[u8], budget: &Budget) {
    if data.is_empty() || data.len() > MAX_CMAP_INPUT {
        return;
    }
    if budget
        .check(LimitCode::FontInputSize, data.len() as u64)
        .is_err()
    {
        return;
    }
    // 1. Direct hayro-cmap parser + CID / ToUnicode lookups.
    if let Some(cmap) = CMap::parse(data, |_| None) {
        let _ = cmap.metadata();
        for code in [0u32, 1, 0x20, 0x41, 0xFF, 0x0041, 0xFFFF] {
            let _ = cmap.lookup_bf_string(code);
            for len in 1..=4 {
                let _ = cmap.lookup_cid_code(code, len);
            }
        }
        for chunk in data.rchunks_exact(4).take(16) {
            let code = u32::from_be_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
            let _ = cmap.lookup_bf_string(code);
            let _ = cmap.lookup_cid_code(code, (chunk[0] % 4) + 1);
        }
    }

    // 2. Engine's Type 0 CMap stream parser (`rich_codes::stream_codes` &
    //    `read_unicode`) via `forms::appearance`.
    if data.len() <= MAX_RICH_CMAP_INPUT {
        let tmpl = type0_template();
        let mut pdf = Vec::with_capacity(tmpl.prefix.len() + data.len() * 2 + 512);
        pdf.extend_from_slice(&tmpl.prefix);
        let o9 = pdf.len();
        pdf.extend_from_slice(
            format!(
                "9 0 obj\n<< /Type /CMap /CMapName /FuzzEnc /Length {} >>\nstream\n",
                data.len()
            )
            .as_bytes(),
        );
        pdf.extend_from_slice(data);
        pdf.extend_from_slice(b"\nendstream\nendobj\n");
        let o10 = pdf.len();
        pdf.extend_from_slice(
            format!("10 0 obj\n<< /Length {} >>\nstream\n", data.len()).as_bytes(),
        );
        pdf.extend_from_slice(data);
        pdf.extend_from_slice(b"\nendstream\nendobj\n");
        let startxref = pdf.len();
        let mut xref = String::from("xref\n0 11\n0000000000 65535 f \n");
        for off in tmpl.offsets {
            let _ = writeln!(xref, "{off:010} 00000 n ");
        }
        let _ = writeln!(xref, "{o9:010} 00000 n ");
        let _ = writeln!(xref, "{o10:010} 00000 n ");
        let _ = write!(
            xref,
            "trailer\n<< /Size 11 /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n"
        );
        pdf.extend_from_slice(xref.as_bytes());
        if let Ok(base) = FileBase::open(Bytes::new(pdf), budget) {
            let doc = Document::new(Arc::new(base), None);
            let version = doc.current();
            let reader = Reader::on_page(&version, budget, 1.0);
            if let Ok(Some(widget)) = version.get(4, budget)
                && let Value::Dict(dict) = widget.value().as_ref()
            {
                let _ = forms::appearance(&reader, dict);
            }
        }
    }
}

fn fuzz_cmap_authoring(data: &[u8], budget: &Budget) {
    if data.is_empty() {
        return;
    }
    // 1. Author a ToUnicode CMap through `ocr::layer::layer` and verify it
    //    parses cleanly with `CMap::parse` and maps every character back.
    let slice = &data[..data.len().min(512)];
    let text = String::from_utf8_lossy(slice);
    let words: Vec<(f64, f64, f64, f64, String)> = text
        .split_whitespace()
        .take(16)
        .enumerate()
        .map(|(i, w)| {
            let y = 20.0 + (i as f64) * 14.0;
            (10.0, y, 200.0, y + 12.0, w.to_owned())
        })
        .collect();
    if !words.is_empty()
        && let Ok(built) = ocr::layer(&words, [0.0, 0.0, 612.0, 792.0], 0, 1.0, 0, "F1", budget)
    {
        let parsed = CMap::parse(&built.to_unicode, |_| None)
            .expect("authored OCR ToUnicode CMap must parse");
        let mut expected_chars = Vec::new();
        for (_, _, _, _, word) in &words {
            for ch in word.chars() {
                if !expected_chars.contains(&ch) {
                    expected_chars.push(ch);
                }
            }
        }
        for (idx, ch) in expected_chars.into_iter().enumerate() {
            let cid = (idx + 1) as u32;
            match parsed.lookup_bf_string(cid) {
                Some(BfString::Char(got)) => assert_eq!(got, ch),
                Some(BfString::String(got)) => assert_eq!(got, ch.to_string()),
                None => panic!("authored CID {cid} missing from ToUnicode CMap"),
            }
        }
    }

    // 2. Author a PostScript CID encoding CMap (matching `_font_cmaps.py`) from
    //    unique 2-byte codes derived from `data`, and verify round-trip lookup.
    let mut mapping: BTreeMap<u16, u16> = BTreeMap::new();
    for chunk in data.chunks_exact(4).take(64) {
        let code = u16::from_be_bytes([chunk[0], chunk[1]]);
        let cid = u16::from_be_bytes([chunk[2], chunk[3]]);
        mapping.insert(code, cid);
    }
    if mapping.is_empty() {
        return;
    }
    let mut cmap = String::from(
        "/CIDInit /ProcSet findresource begin\n\
         12 dict begin\n\
         begincmap\n\
         /CIDSystemInfo << /Registry <41646f6265> /Ordering <4964656e74697479> /Supplement 0 >> def\n\
         /CMapName /FuzzEnc def\n\
         /CMapType 1 def\n\
         /WMode 0 def\n\
         1 begincodespacerange\n\
         <0000> <FFFF>\n\
         endcodespacerange\n",
    );
    let _ = writeln!(cmap, "{} begincidchar", mapping.len());
    for (&code, &cid) in &mapping {
        let _ = writeln!(cmap, "<{code:04X}> {cid}");
    }
    cmap.push_str("endcidchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n");
    let parsed = CMap::parse(cmap.as_bytes(), |_| None).expect("authored encoding CMap must parse");
    for (&code, &cid) in &mapping {
        assert_eq!(
            parsed.lookup_cid_code(u32::from(code), 2),
            Some(u32::from(cid)),
            "authored CMap code {code:#06x} -> CID {cid}"
        );
    }
}

fuzz_target!(
    // The store turns a panic of hayro-syntax into an error (DESIGN.md I3),
    // but libFuzzer's hook aborts on any panic: keep the default hook, so
    // that only a panic that escapes the target is a crash.
    init: {
        let _ = std::panic::take_hook();
    },
    |data: &[u8]| {
        let budget = fuzz_budget();
        fuzz_type1(data, &budget);
        fuzz_cmap_parsing(data, &budget);
        fuzz_cmap_authoring(data, &budget);
    }
);
