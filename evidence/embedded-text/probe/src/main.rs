//! Route C of issue #466 in miniature: shape text with harfrust, write it
//! with pdf-writer as a Type0 font with Identity-H, CID = GID, and register
//! the font once per document, so that every page draws through the same
//! font objects and the document embeds one program.
//!
//! Usage: embedded-text-probe FONT OUT PAGES TEXT...
//!
//! Page `i` draws `TEXT[i % n]`. The program is embedded whole, as the
//! provisional font object of DESIGN.md section 7 would hold it; the
//! measurement script subsets it afterwards with `doc.subset_fonts()`.

use std::collections::BTreeMap;
use std::time::Instant;

use harfrust::{Buffer, ShapeOptions, ShaperFont};
use pdf_writer::types::{CidFontType, FontFlags, SystemInfo, UnicodeCmap};
use pdf_writer::{Content, Filter, Finish, Name, Pdf, Rect, Ref, Str};
use read_fonts::{FontRef, TableProvider};

/// A glyph placed by the shaper, in font units.
struct Placed {
    gid: u16,
    advance: i32,
    x_offset: i32,
    y_offset: i32,
}

/// The document's resource for one face: the glyphs drawn so far, with
/// their widths and the text that they stand for.
struct FontResource {
    data: Vec<u8>,
    shaping: harfrust::Font,
    upem: f32,
    /// Advance widths in thousandths of the em, by glyph.
    widths: BTreeMap<u16, f32>,
    /// The text of a glyph's cluster, for ToUnicode; the first text wins.
    text: BTreeMap<u16, String>,
    /// Glyphs met again with another text, which ToUnicode cannot express.
    conflicts: usize,
    /// Glyphs drawn as .notdef, for characters that the face lacks.
    notdef: usize,
}

impl FontResource {
    fn new(data: Vec<u8>) -> Self {
        let upem = f32::from(FontRef::new(&data).unwrap().head().unwrap().units_per_em());
        Self {
            shaping: harfrust::Font::new(data.clone(), 0).unwrap(),
            data,
            upem,
            widths: BTreeMap::new(),
            text: BTreeMap::new(),
            conflicts: 0,
            notdef: 0,
        }
    }

    /// Shapes `text` and records its glyphs.
    fn shape(&mut self, text: &str) -> Vec<Placed> {
        let shaper = ShaperFont::new(&self.shaping);
        let mut buffer = Buffer::new();
        buffer.push_str(text);
        buffer.guess_segment_properties();
        harfrust::shape(&shaper, &mut buffer, ShapeOptions::new()).unwrap();
        let infos = buffer.glyph_infos();
        let positions = buffer.glyph_positions();
        // The clusters' byte ranges: a cluster runs to the next larger start.
        let mut starts: Vec<usize> = infos.iter().map(|info| info.cluster as usize).collect();
        starts.push(text.len());
        starts.sort_unstable();
        starts.dedup();
        // Each glyph's text for ToUnicode. When every glyph of a cluster is
        // the nominal glyph of one of its characters, as a base and its marks
        // are, each glyph maps to its own character; otherwise, as in a
        // ligature, the cluster's first glyph maps to all of its text.
        let mut glyph_text = vec![String::new(); infos.len()];
        let mut first = 0;
        while first < infos.len() {
            let start = infos[first].cluster as usize;
            let mut last = first;
            while last + 1 < infos.len() && infos[last + 1].cluster as usize == start {
                last += 1;
            }
            let end = starts[starts.partition_point(|&s| s <= start)];
            let cluster = &text[start..end];
            let own: Option<Vec<char>> = (first..=last)
                .map(|at| {
                    cluster.chars().find(|&c| {
                        shaper.nominal_glyph(c as u32).map(u32::from) == Some(infos[at].glyph_id)
                    })
                })
                .collect();
            match own {
                Some(chars) if chars.len() == cluster.chars().count() => {
                    for (at, c) in (first..=last).zip(chars) {
                        glyph_text[at] = c.to_string();
                    }
                }
                _ => glyph_text[first] = cluster.to_owned(),
            }
            first = last + 1;
        }
        let mut placed = Vec::with_capacity(infos.len());
        for ((info, position), cluster_text) in infos.iter().zip(positions).zip(glyph_text) {
            let gid = u16::try_from(info.glyph_id).unwrap();
            let width =
                f32::from(shaper.glyph_h_advance(info.glyph_id.into()) as i16) * 1000.0 / self.upem;
            self.widths.insert(gid, width);
            match self.text.get(&gid) {
                _ if gid == 0 => self.notdef += 1,
                None => {
                    self.text.insert(gid, cluster_text);
                }
                // An occurrence that ToUnicode cannot express: it would need
                // ActualText.
                Some(known) if *known != cluster_text => self.conflicts += 1,
                Some(_) => {}
            }
            placed.push(Placed {
                gid,
                advance: position.x_advance,
                x_offset: position.x_offset,
                y_offset: position.y_offset,
            });
        }
        placed
    }

    /// Writes the Type0 font, its CIDFont, descriptor, program, and
    /// ToUnicode CMap.
    fn write(&self, pdf: &mut Pdf, alloc: &mut Ref, type0: Ref) {
        let (cid, descriptor, program, cmap) =
            (alloc.bump(), alloc.bump(), alloc.bump(), alloc.bump());
        let font = FontRef::new(&self.data).unwrap();
        let cff = font.cff().is_ok();
        let scale = 1000.0 / self.upem;
        let head = font.head().unwrap();
        let hhea = font.hhea().unwrap();
        let cap = font
            .os2()
            .ok()
            .and_then(|os2| os2.s_cap_height())
            .map_or(f32::from(hhea.ascender().to_i16()), f32::from);
        let name = Name(b"AAAAAA+Probe");
        let info = SystemInfo {
            registry: Str(b"Adobe"),
            ordering: Str(b"Identity"),
            supplement: 0,
        };

        pdf.type0_font(type0)
            .base_font(name)
            .encoding_predefined(Name(b"Identity-H"))
            .descendant_font(cid)
            .to_unicode(cmap);

        let mut cid_font = pdf.cid_font(cid);
        cid_font
            .subtype(if cff {
                CidFontType::Type0
            } else {
                CidFontType::Type2
            })
            .base_font(name)
            .system_info(info)
            .font_descriptor(descriptor)
            .default_width(0.0);
        if !cff {
            cid_font.cid_to_gid_map_predefined(Name(b"Identity"));
        }
        let mut widths = cid_font.widths();
        for (&gid, &width) in &self.widths {
            widths.consecutive(gid, [width]);
        }
        widths.finish();
        cid_font.finish();

        let mut fd = pdf.font_descriptor(descriptor);
        fd.name(name)
            .flags(FontFlags::SYMBOLIC)
            .bbox(Rect::new(
                f32::from(head.x_min()) * scale,
                f32::from(head.y_min()) * scale,
                f32::from(head.x_max()) * scale,
                f32::from(head.y_max()) * scale,
            ))
            .italic_angle(font.post().map_or(0.0, |post| post.italic_angle().to_f32()))
            .ascent(f32::from(hhea.ascender().to_i16()) * scale)
            .descent(f32::from(hhea.descender().to_i16()) * scale)
            .cap_height(cap * scale)
            .stem_v(80.0);
        if cff {
            fd.font_file3(program);
        } else {
            fd.font_file2(program);
        }
        fd.finish();

        let program_data = if std::env::var_os("PRUNE").is_some() {
            prune(&self.data, cff)
        } else {
            self.data.clone()
        };
        let compressed = miniz_oxide::deflate::compress_to_vec_zlib(&program_data, 6);
        let mut stream = pdf.stream(program, &compressed);
        stream.filter(Filter::FlateDecode);
        if cff {
            stream.pair(Name(b"Subtype"), Name(b"OpenType"));
        } else {
            stream.pair(Name(b"Length1"), i32::try_from(program_data.len()).unwrap());
        }
        stream.finish();

        let mut unicode = UnicodeCmap::new(Name(b"Adobe-Identity-UCS"), info);
        // .notdef stands for a character that the face lacks: no text.
        for (&gid, text) in &self.text {
            if gid != 0 && !text.is_empty() {
                unicode.pair_with_multiple(gid, text.chars());
            }
        }
        pdf.cmap(cmap, &unicode.finish());
    }
}

/// Returns the program with only the tables that a PDF consumer reads from
/// an embedded program (ISO 32000-2 9.9; `cmap` is needed only by simple
/// symbolic TrueType fonts), dropping shaping and variation tables such as
/// GSUB, GPOS, and gvar. A variable font keeps its default instance.
fn prune(data: &[u8], cff: bool) -> Vec<u8> {
    const TRUETYPE: [&[u8; 4]; 9] = [
        b"cvt ", b"fpgm", b"glyf", b"head", b"hhea", b"hmtx", b"loca", b"maxp", b"prep",
    ];
    const OPENTYPE: [&[u8; 4]; 4] = [b"CFF ", b"head", b"hhea", b"hmtx"];
    let keep: &[&[u8; 4]] = if cff { &OPENTYPE } else { &TRUETYPE };
    let count = usize::from(u16::from_be_bytes([data[4], data[5]]));
    let mut tables = Vec::new();
    for index in 0..count {
        let record = &data[12 + 16 * index..28 + 16 * index];
        let tag: [u8; 4] = record[..4].try_into().unwrap();
        let offset = u32::from_be_bytes(record[8..12].try_into().unwrap()) as usize;
        let length = u32::from_be_bytes(record[12..16].try_into().unwrap()) as usize;
        if keep.contains(&&tag) {
            tables.push((tag, &data[offset..offset + length]));
        }
    }
    let mut out = data[..4].to_vec();
    let n = u16::try_from(tables.len()).unwrap();
    let power = 1u16 << (15 - n.leading_zeros());
    out.extend(n.to_be_bytes());
    out.extend((power * 16).to_be_bytes());
    out.extend(u16::try_from(power.trailing_zeros()).unwrap().to_be_bytes());
    out.extend((n * 16 - power * 16).to_be_bytes());
    let mut offset = 12 + 16 * tables.len();
    for (tag, table) in &tables {
        let sum = table
            .chunks(4)
            .map(|chunk| {
                let mut word = [0; 4];
                word[..chunk.len()].copy_from_slice(chunk);
                u32::from_be_bytes(word)
            })
            .fold(0u32, u32::wrapping_add);
        out.extend(tag);
        out.extend(sum.to_be_bytes());
        out.extend(u32::try_from(offset).unwrap().to_be_bytes());
        out.extend(u32::try_from(table.len()).unwrap().to_be_bytes());
        offset += table.len().next_multiple_of(4);
    }
    for (_, table) in &tables {
        out.extend(*table);
        out.resize(out.len().next_multiple_of(4), 0);
    }
    out
}

/// Appends a text drawing that shows `placed` from `(x, y)` at `size`.
fn draw(content: &mut Content, font: &FontResource, placed: &[Placed], x: f32, y: f32, size: f32) {
    content.begin_text().set_font(Name(b"F1"), size);
    content.set_text_matrix([1.0, 0.0, 0.0, 1.0, x, y]);
    let to_thousandths = 1000.0 / font.upem;
    let mut items = Vec::new();
    // A pending adjustment, in TJ units: positive moves left.
    let mut pending = 0.0f32;
    let mut rise = 0;
    let flush = |content: &mut Content, items: &mut Vec<(Vec<u8>, f32)>| {
        if items.is_empty() {
            return;
        }
        let mut shown = content.show_positioned();
        let mut list = shown.items();
        for (bytes, adjust) in items.drain(..) {
            if adjust != 0.0 {
                list.adjust(adjust);
            }
            if !bytes.is_empty() {
                list.show(Str(&bytes));
            }
        }
    };
    for glyph in placed {
        if glyph.y_offset != rise {
            flush(content, &mut items);
            rise = glyph.y_offset;
            content.set_rise(rise as f32 * size / font.upem);
        }
        let width = font.widths[&glyph.gid];
        let x_offset = glyph.x_offset as f32 * to_thousandths;
        // Move right by the offset, show the glyph, then move back by the
        // offset and by the difference between its width and its advance.
        items.push((glyph.gid.to_be_bytes().to_vec(), pending - x_offset));
        pending = width - glyph.advance as f32 * to_thousandths + x_offset;
    }
    flush(content, &mut items);
    if rise != 0 {
        content.set_rise(0.0);
    }
    content.end_text();
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let [_, font_path, out, pages, texts @ ..] = args.as_slice() else {
        eprintln!("usage: embedded-text-probe FONT OUT PAGES TEXT...");
        std::process::exit(2);
    };
    let pages: usize = pages.parse().unwrap();
    let started = Instant::now();
    let mut font = FontResource::new(std::fs::read(font_path).unwrap());

    let mut alloc = Ref::new(1);
    let catalog = alloc.bump();
    let tree = alloc.bump();
    let type0 = alloc.bump();
    let mut pdf = Pdf::new();
    let mut kids = Vec::with_capacity(pages);
    for index in 0..pages {
        let (page, contents) = (alloc.bump(), alloc.bump());
        kids.push(page);
        let placed = font.shape(&texts[index % texts.len()]);
        let mut content = Content::new();
        draw(&mut content, &font, &placed, 72.0, 720.0, 24.0);
        pdf.stream(contents, &content.finish());
        let mut page_writer = pdf.page(page);
        page_writer
            .parent(tree)
            .media_box(Rect::new(0.0, 0.0, 612.0, 792.0))
            .contents(contents);
        page_writer.resources().fonts().pair(Name(b"F1"), type0);
    }
    pdf.pages(tree)
        .kids(kids.iter().copied())
        .count(i32::try_from(pages).unwrap());
    pdf.catalog(catalog).pages(tree);
    font.write(&mut pdf, &mut alloc, type0);
    let bytes = pdf.finish();
    std::fs::write(out, &bytes).unwrap();
    println!(
        "{{\"bytes\": {}, \"glyphs\": {}, \"tounicode_conflicts\": {}, \"notdef\": {}, \"seconds\": {:.4}}}",
        bytes.len(),
        font.widths.len(),
        font.conflicts,
        font.notdef,
        started.elapsed().as_secs_f64()
    );
}
