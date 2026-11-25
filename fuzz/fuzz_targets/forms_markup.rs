//! FDF/XFDF form data parsing and XHTML rich-text parsing/layout (DESIGN.md F6, F9).
//!
//! - `%FDF-x.y` inputs exercise FDF header patching, `FileBase::open`, and
//!   bounded iterative traversal of `/Root /FDF /Fields`, `/Kids`, `/F`, `/ID`,
//!   `/V`, and `/RV` under `Budget`.
//! - `%PDF-` inputs exercise AcroForm traversal (`forms::walk`), field reads,
//!   widget appearance synthesis (`forms::appearance` / `restyled`), rich-text
//!   fields, embedded form fonts, and annotation/line-caption appearances.
//! - All other inputs exercise `forms::rich_text::parse` (with structural
//!   invariants), full rich-text widget & line-caption layout (including CSS,
//!   UTF-8/UTF-16 stream decoding, and Unicode BiDi), and bounded XFDF pull
//!   parsing via `quick-xml`.

#![no_main]

use std::collections::HashSet;
use std::fmt::Write as _;
use std::sync::Arc;
use std::time::Duration;

use libfuzzer_sys::fuzz_target;
use pyropdf_engine::annotations::{self, PageAnnots, Reader};
use pyropdf_engine::forms::{
    self,
    rich_text::{self, Element, Run},
};
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Reference, Value};
use pyropdf_store::version::{Document, Version};
use quick_xml::XmlVersion;
use quick_xml::events::Event;
use quick_xml::name::{Namespace, ResolveResult};
use quick_xml::reader::NsReader;

const HEADER_WINDOW: usize = 1024;
const MAX_FDF_DEPTH: usize = 32;
const MAX_FDF_FIELDS: usize = 4096;
const MAX_FDF_EDGES: usize = 8192;
const MAX_PAGES: usize = 4;
const MAX_WIDGETS: usize = 8;
const MAX_RICH_TEMPLATE: usize = 65_536;
const XFDF_NS: &str = "http://ns.adobe.com/xfdf/";

fn fuzz_budget() -> Budget {
    let mut limits = DocumentLimits::web();
    limits.max_memory_size = Some(64 * 1024 * 1024);
    limits.max_decompressed_size = Some(4 * 1024 * 1024);
    limits.max_total_decompressed_size = Some(8 * 1024 * 1024);
    limits.max_duration = Some(Duration::from_secs(2));
    Budget::new(&limits)
}

/// Match `python/pyropdf/_fdf.py::_patch_fdf_header`: verify `%FDF-x.y` within
/// the first 1,024 bytes and replace `%FDF-` with `%PDF-`.
fn patch_fdf_header(raw: &[u8]) -> Option<Vec<u8>> {
    let end = raw.len().min(HEADER_WINDOW + 8);
    let head = &raw[..end];
    let limit = head.len().saturating_sub(4).min(HEADER_WINDOW);
    let offset = head[..limit.min(head.len())]
        .windows(5)
        .position(|w| w == b"%FDF-")?;
    if head.len() < offset + 8 {
        return None;
    }
    let (major, dot, minor) = (head[offset + 5], head[offset + 6], head[offset + 7]);
    if !(major.is_ascii_digit() && dot == b'.' && minor.is_ascii_digit()) {
        return None;
    }
    if head.get(offset + 8).is_some_and(u8::is_ascii_digit) {
        return None;
    }
    let mut patched = Vec::with_capacity(raw.len());
    patched.extend_from_slice(&raw[..offset]);
    patched.extend_from_slice(b"%PDF-");
    patched.extend_from_slice(&raw[offset + 5..]);
    Some(patched)
}

fn resolve_value(
    version: &Version,
    value: &Value,
    budget: &Budget,
) -> Option<(Option<Reference>, Arc<Value>)> {
    let mut current = match value {
        Value::Reference(r) => *r,
        direct => return Some((None, Arc::new(direct.clone()))),
    };
    for _ in 0..32 {
        budget.poll().ok()?;
        let obj = version.get(current.number(), budget).ok()??;
        if obj.generation() != current.generation() {
            return None;
        }
        match obj.value().as_ref() {
            Value::Reference(next) => current = *next,
            _ => return Some((Some(current), Arc::clone(obj.value()))),
        }
    }
    None
}

fn fuzz_fdf(patched: Vec<u8>, budget: &Budget) {
    let Ok(base) = FileBase::open(Bytes::new(patched), budget) else {
        return;
    };
    let doc = Document::new(Arc::new(base), None);
    let version = doc.current();
    let Some(root_val) = version.trailer().get("Root") else {
        return;
    };
    let Some((_, root_obj)) = resolve_value(&version, root_val, budget) else {
        return;
    };
    let Value::Dict(root) = root_obj.as_ref() else {
        return;
    };
    let Some(fdf_val) = root.get("FDF") else {
        return;
    };
    let Some((_, fdf_obj)) = resolve_value(&version, fdf_val, budget) else {
        return;
    };
    let Value::Dict(fdf) = fdf_obj.as_ref() else {
        return;
    };
    if let Some(f_val) = fdf.get("F")
        && let Some((_, resolved)) = resolve_value(&version, f_val, budget)
    {
        match resolved.as_ref() {
            Value::String(s) => {
                let _ = forms::text_string(s.as_bytes());
            }
            Value::Dict(spec) => {
                for key in ["UF", "F", "Unix", "DOS", "Mac"] {
                    if let Some(v) = spec.get(key)
                        && let Some((_, r)) = resolve_value(&version, v, budget)
                        && let Value::String(s) = r.as_ref()
                    {
                        let _ = forms::text_string(s.as_bytes());
                    }
                }
            }
            _ => {}
        }
    }
    let Some(fields_val) = fdf.get("Fields") else {
        return;
    };
    let Some((_, fields_obj)) = resolve_value(&version, fields_val, budget) else {
        return;
    };
    let Value::Array(fields) = fields_obj.as_ref() else {
        return;
    };
    if fields.len() > MAX_FDF_EDGES {
        return;
    }
    let mut stack: Vec<(Value, usize)> = fields.iter().rev().map(|v| (v.clone(), 1)).collect();
    let mut seen: HashSet<(u32, u16)> = HashSet::new();
    let mut nodes = 0usize;
    let mut edges = fields.len();
    while let Some((entry, depth)) = stack.pop() {
        if budget.poll().is_err() || nodes >= MAX_FDF_FIELDS || depth > MAX_FDF_DEPTH {
            break;
        }
        let Some((id, resolved)) = resolve_value(&version, &entry, budget) else {
            continue;
        };
        if let Some(r) = id
            && !seen.insert((r.number(), r.generation()))
        {
            continue;
        }
        let Value::Dict(dict) = resolved.as_ref() else {
            continue;
        };
        nodes += 1;
        if let Some(t) = dict.get("T")
            && let Some((_, r)) = resolve_value(&version, t, budget)
            && let Value::String(s) = r.as_ref()
        {
            let _ = forms::text_string(s.as_bytes());
        }
        if let Some(v) = dict.get("V")
            && let Some((_, r)) = resolve_value(&version, v, budget)
        {
            match r.as_ref() {
                Value::String(s) => {
                    let _ = forms::text_string(s.as_bytes());
                }
                Value::Stream(stream) => {
                    if let Ok(decoded) = stream.decoded(budget) {
                        let _ = forms::text_string(&decoded);
                    }
                }
                Value::Array(arr) => {
                    for item in arr.iter().take(64) {
                        if let Some((_, elem)) = resolve_value(&version, item, budget)
                            && let Value::String(s) = elem.as_ref()
                        {
                            let _ = forms::text_string(s.as_bytes());
                        }
                    }
                }
                _ => {}
            }
        }
        if let Some(rv) = dict.get("RV")
            && let Some((_, r)) = resolve_value(&version, rv, budget)
            && let Value::String(s) = r.as_ref()
        {
            let text = forms::text_string(s.as_bytes());
            let _ = rich_text::parse(&text, budget);
        }
        if let Some(kids_val) = dict.get("Kids")
            && let Some((_, kids_obj)) = resolve_value(&version, kids_val, budget)
            && let Value::Array(kids) = kids_obj.as_ref()
        {
            edges = edges.saturating_add(kids.len());
            if edges > MAX_FDF_EDGES {
                break;
            }
            for kid in kids.iter().rev() {
                stack.push((kid.clone(), depth + 1));
            }
        }
    }
}

fn fuzz_pdf_forms(data: &[u8], budget: &Budget) {
    let Ok(base) = FileBase::open(Bytes::new(data.to_vec()), budget) else {
        return;
    };
    let doc = Document::new(Arc::new(base), None);
    let version = doc.current();
    let Ok(pages) = PageIndex::build(&mut Reads::new(&version, budget)).map(Arc::new) else {
        return;
    };
    if let Ok(form) = forms::walk(&mut Reads::new(&version, budget)) {
        if let Ok(fields) = forms::form_fields(&form, budget) {
            for (name, id) in fields.into_iter().take(MAX_WIDGETS) {
                let _ = forms::form_field(&form, &name);
                let _ = forms::form_field_read(&form, id, budget);
                let _ = forms::form_field_choices(&form, &version, id, budget);
                let _ = forms::form_field_widgets(&form, &version, &pages, id, budget);
            }
        }
        let reader = Reader::on_page(&version, budget, 1.0);
        for page in pages.pages().iter().take(MAX_PAGES) {
            let Some(page_ref) = page.object() else {
                continue;
            };
            if let Ok(Some(annots)) = PageAnnots::read(
                Arc::clone(&version),
                Arc::clone(&pages),
                budget,
                page_ref.number(),
                page_ref.generation(),
            ) {
                let _ = forms::widget_list(&annots, &form);
                if let Some(Value::Array(items)) = page.dict().get("Annots") {
                    for item in items.iter().take(MAX_WIDGETS) {
                        if let Some((_, resolved)) = resolve_value(&version, item, budget)
                            && let Value::Dict(dict) = resolved.as_ref()
                        {
                            let _ = forms::appearance(&reader, dict);
                            let _ = forms::restyled(&reader, dict);
                            let _ = annotations::appearance(&reader, dict);
                        }
                    }
                }
            }
        }
    }
}

/// Check balanced-scope invariants of `forms::rich_text::parse`.
fn check_rich_text(xml: &str, budget: &Budget) {
    let Ok(runs) = rich_text::parse(xml, budget) else {
        return;
    };
    assert!(
        matches!(runs.first(), Some(Run::Open(Element::Body, _))),
        "valid rich text starts with <body>"
    );
    assert!(
        matches!(runs.last(), Some(Run::Close(Element::Body))),
        "valid rich text ends with </body>"
    );
    let mut stack = Vec::new();
    for run in &runs {
        match run {
            Run::Open(el, _) => {
                if !stack.is_empty() {
                    assert_ne!(*el, Element::Body, "nested <body> must be rejected");
                }
                stack.push(*el);
            }
            Run::Close(el) => {
                assert_eq!(stack.pop(), Some(*el), "unbalanced rich text tags");
            }
            Run::Text(_) => {
                assert!(!stack.is_empty(), "text outside <body>");
            }
        }
    }
    assert!(stack.is_empty(), "unclosed rich text tags");
}

fn pdf_hex_string(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    let mut out = String::with_capacity(bytes.len() * 2 + 2);
    out.push('<');
    for &b in bytes {
        out.push(char::from(HEX[usize::from(b >> 4)]));
        out.push(char::from(HEX[usize::from(b & 15)]));
    }
    out.push('>');
    out
}

/// Build a tiny one-page PDF with a rich-text widget (using either stream or
/// hex-string `/RV` and CSS `/DS`) and a rich-caption Line annotation (`/RC`),
/// then synthesize appearances for both.
fn fuzz_rich_appearance(data: &[u8], budget: &Budget) {
    if data.is_empty() || data.len() > MAX_RICH_TEMPLATE {
        return;
    }
    let selector = data[data.len() - 1];
    let (rv_bytes, ds_bytes) = match data.iter().position(|&b| b == 0) {
        Some(pos) if pos < data.len() - 1 => (&data[..pos], &data[pos + 1..]),
        _ => (data, &b""[..]),
    };
    let use_stream = selector & 1 == 0;
    let ff: i64 = (1 << 25) // RICH_TEXT
        | if selector & 2 != 0 { 1 << 12 } else { 0 }; // MULTILINE
    let auto_size = selector & 4 != 0;
    let da = if auto_size {
        "/Helv 0 Tf 0 g"
    } else {
        "/Helv 11 Tf 0 g"
    };
    let ds_hex = pdf_hex_string(&ds_bytes[..ds_bytes.len().min(4096)]);
    let rv_ref_or_hex = if use_stream {
        "5 0 R".to_owned()
    } else {
        pdf_hex_string(&rv_bytes[..rv_bytes.len().min(16_384)])
    };

    let mut out = b"%PDF-1.7\n".to_vec();
    let mut offsets = Vec::with_capacity(6);
    fn push_obj(out: &mut Vec<u8>, offsets: &mut Vec<usize>, num: usize, body: &[u8]) {
        offsets.push(out.len());
        out.extend_from_slice(format!("{num} 0 obj\n").as_bytes());
        out.extend_from_slice(body);
        out.extend_from_slice(b"\nendobj\n");
    }
    push_obj(
        &mut out,
        &mut offsets,
        1,
        b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R] /DA (/Helv 11 Tf 0 g) >> >>",
    );
    push_obj(
        &mut out,
        &mut offsets,
        2,
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    );
    push_obj(
        &mut out,
        &mut offsets,
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 220 100] /Annots [4 0 R 6 0 R] >>",
    );
    let widget = format!(
        "<< /Type /Annot /Subtype /Widget /FT /Tx /T (rich) /Ff {ff} /Rect [4 4 200 80] /DA ({da}) /DS {ds_hex} /RV {rv_ref_or_hex} >>"
    );
    push_obj(&mut out, &mut offsets, 4, widget.as_bytes());
    offsets.push(out.len());
    out.extend_from_slice(
        format!("5 0 obj\n<< /Length {} >>\nstream\n", rv_bytes.len()).as_bytes(),
    );
    out.extend_from_slice(rv_bytes);
    out.extend_from_slice(b"\nendstream\nendobj\n");
    let line = format!(
        "<< /Type /Annot /Subtype /Line /Rect [4 4 200 80] /L [10 40 190 40] /Cap true /RC {rv_ref_or_hex} >>"
    );
    push_obj(&mut out, &mut offsets, 6, line.as_bytes());

    let startxref = out.len();
    let mut xref = format!("xref\n0 {}\n0000000000 65535 f \n", offsets.len() + 1);
    for off in offsets {
        let _ = writeln!(xref, "{off:010} 00000 n ");
    }
    let _ = write!(
        xref,
        "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n",
        7
    );
    out.extend_from_slice(xref.as_bytes());

    let Ok(base) = FileBase::open(Bytes::new(out), budget) else {
        return;
    };
    let doc = Document::new(Arc::new(base), None);
    let version = doc.current();
    let reader = Reader::on_page(&version, budget, 1.0);
    if let Ok(Some(obj)) = version.get(4, budget)
        && let Value::Dict(dict) = obj.value().as_ref()
    {
        let _ = forms::appearance(&reader, dict);
    }
    if let Ok(Some(obj)) = version.get(6, budget)
        && let Value::Dict(dict) = obj.value().as_ref()
    {
        let _ = annotations::appearance(&reader, dict);
    }
}

/// Bounded XFDF pull-parser exercising `quick-xml` namespace resolution,
/// field hierarchy tracking, `<ids>` hex validation, `<f href=...>`, `<value>`,
/// and passing `<value-richtext>` inner XHTML to `forms::rich_text::parse`.
fn fuzz_xfdf(xml: &str, budget: &Budget) {
    if budget
        .check(LimitCode::FormFieldInputSize, xml.len() as u64)
        .is_err()
    {
        return;
    }
    let mut reader = NsReader::from_str(xml);
    reader.config_mut().expand_empty_elements = true;
    reader.config_mut().check_comments = true;
    let mut depth = 0usize;
    let mut fields = 0usize;
    let mut in_richtext = false;
    let mut richtext_depth = 0usize;
    let mut richtext_buf = String::new();

    loop {
        if budget.poll().is_err() {
            return;
        }
        let Ok((ns, event)) = reader.read_resolved_event() else {
            return;
        };
        match event {
            Event::Start(start) => {
                depth += 1;
                if depth > MAX_FDF_DEPTH + 4 {
                    return;
                }
                let local = start.local_name();
                let name = local.as_ref();
                if in_richtext {
                    richtext_depth += 1;
                    if richtext_buf.len() < 16_384 {
                        richtext_buf.push('<');
                        richtext_buf.push_str(name);
                        for attr in start.attributes().take(32).flatten() {
                            if let Ok(val) = attr.normalized_value(XmlVersion::Implicit1_0) {
                                let _ = write!(
                                    richtext_buf,
                                    " {}=\"{}\"",
                                    attr.key.0,
                                    val.replace('"', "&quot;")
                                );
                            }
                        }
                        richtext_buf.push('>');
                    }
                    continue;
                }
                let allowed_ns = matches!(
                    ns,
                    ResolveResult::Unbound | ResolveResult::Bound(Namespace(XFDF_NS))
                );
                if allowed_ns {
                    match name {
                        "field" => {
                            fields += 1;
                            if fields > MAX_FDF_FIELDS {
                                return;
                            }
                            for attr in start.attributes().take(16).flatten() {
                                let _ = attr.normalized_value(XmlVersion::Implicit1_0);
                            }
                        }
                        "f" | "ids" => {
                            for attr in start.attributes().take(16).flatten() {
                                let _ = attr.normalized_value(XmlVersion::Implicit1_0);
                            }
                        }
                        "value-richtext" => {
                            in_richtext = true;
                            richtext_depth = 0;
                            richtext_buf.clear();
                        }
                        _ => {}
                    }
                }
            }
            Event::End(end) => {
                if in_richtext {
                    if richtext_depth == 0 {
                        in_richtext = false;
                        check_rich_text(&richtext_buf, budget);
                        richtext_buf.clear();
                    } else {
                        richtext_depth -= 1;
                        if richtext_buf.len() < 16_384 {
                            let _ = write!(richtext_buf, "</{}>", end.local_name().as_ref());
                        }
                    }
                }
                depth = depth.saturating_sub(1);
            }
            Event::Text(text) => {
                let s = text.xml_content(XmlVersion::Implicit1_0);
                if in_richtext && richtext_buf.len() < 16_384 {
                    richtext_buf.push_str(&s);
                } else {
                    let _ = forms::text_string(s.as_bytes());
                }
            }
            Event::DocType(_) => return,
            Event::Eof => break,
            _ => {}
        }
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
        if let Some(patched) = patch_fdf_header(data) {
            fuzz_fdf(patched, &budget);
            return;
        }
        if data.starts_with(b"%PDF-") {
            fuzz_pdf_forms(data, &budget);
            return;
        }
        // Direct XHTML rich-text parsing (valid UTF-8 and lossy fallback).
        if let Ok(xml) = std::str::from_utf8(data) {
            check_rich_text(xml, &budget);
            fuzz_xfdf(xml, &budget);
        } else if data.len() <= MAX_RICH_TEMPLATE {
            let lossy = String::from_utf8_lossy(data);
            check_rich_text(&lossy, &budget);
            fuzz_xfdf(&lossy, &budget);
        }
        // Full rich-text widget + line-caption appearance synthesis.
        fuzz_rich_appearance(data, &budget);
    }
);
