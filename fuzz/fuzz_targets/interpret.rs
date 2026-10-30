//! Interpretation (DESIGN.md I3, #19): rendering, SVG conversion, text
//! extraction, and content items of the first pages fail on any panic,
//! including one that the store or the engine would catch.
//!
//! An input that starts with `%PDF-` is a whole file. Any other input is the
//! content stream of a one-page file whose resources name colour spaces,
//! functions, a shading, a pattern, an image, a form, a font, and a graphics
//! state, so that content-stream mutations reach hayro's interpreter and
//! vello_cpu's rasterizer without first having to build a valid file.

#![no_main]

use std::fmt::Write as _;
use std::sync::Arc;

use libfuzzer_sys::fuzz_target;
use pyropdf_engine::bridge::Bridged;
use pyropdf_engine::extract::{self, ItemKinds, TextOptions};
use pyropdf_engine::render;
use pyropdf_store::budget::{Budget, DocumentLimits};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::Bytes;
use pyropdf_store::version::Document;

/// The pages interpreted of a whole file, which bounds a run's time.
const MAX_PAGES: usize = 4;

/// The objects of the template page, numbered from 1; the content stream is
/// object 4 and follows them.
const TEMPLATE: &[&str] = &[
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R \
     /Resources << \
     /ColorSpace << /Ix [/Indexed /DeviceRGB 1 <ff000000ff00>] \
     /Sep [/Separation /Spot /DeviceCMYK 5 0 R] \
     /DN [/DeviceN [/A /B] /DeviceRGB 6 0 R] \
     /Lab [/Lab << /WhitePoint [0.95 1 1.09] /Range [-100 100 -100 100] >>] \
     /Cal [/CalRGB << /WhitePoint [0.95 1 1.09] /Gamma [2.2 2.2 2.2] >>] \
     /Pat [/Pattern /DeviceRGB] >> \
     /Shading << /Sh 7 0 R >> \
     /Pattern << /P1 8 0 R /P2 << /PatternType 2 /Shading 7 0 R >> >> \
     /XObject << /Im 9 0 R /Fm 10 0 R >> \
     /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> \
     /F2 << /Type /Font /Subtype /Type1 /BaseFont /Times-Roman \
     /Encoding /WinAnsiEncoding >> >> \
     /ExtGState << /G << /CA 0.5 /ca 0.5 /BM /Multiply /LW 2 >> >> >> >>",
    "",
    "<< /FunctionType 2 /Domain [0 1] /C0 [0 0 0 0] /C1 [0 1 1 0] /N 1 >>",
    "<< /FunctionType 4 /Domain [0 1 0 1] /Range [0 1 0 1 0 1] /Length 20 >>\n\
     stream\n{ exch dup 0.5 mul }\nendstream",
    "<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 200 200] \
     /Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> \
     /Extend [true true] >>",
    "<< /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 10 10] /XStep 10 \
     /YStep 10 /Resources << >> /Length 16 >>\nstream\n0 0 5 5 re f\n   \nendstream",
    "<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray \
     /BitsPerComponent 8 /Filter /ASCIIHexDecode /Length 9 >>\nstream\n004080ff>\nendstream",
    "<< /Type /XObject /Subtype /Form /BBox [0 0 50 50] /Group << /S /Transparency >> \
     /Resources << >> /Length 15 >>\nstream\n0 0 50 50 re f\n\nendstream",
];

/// A one-page file whose content stream is `content`.
fn template(content: &[u8]) -> Vec<u8> {
    let mut out = b"%PDF-1.7\n".to_vec();
    let mut offsets = Vec::with_capacity(TEMPLATE.len());
    for (index, body) in TEMPLATE.iter().enumerate() {
        offsets.push(out.len());
        out.extend_from_slice(format!("{} 0 obj\n", index + 1).as_bytes());
        if index == 3 {
            out.extend_from_slice(format!("<< /Length {} >>\nstream\n", content.len()).as_bytes());
            out.extend_from_slice(content);
            out.extend_from_slice(b"\nendstream");
        } else {
            out.extend_from_slice(body.as_bytes());
        }
        out.extend_from_slice(b"\nendobj\n");
    }
    let start = out.len();
    let mut xref = format!("xref\n0 {}\n0000000000 65535 f \n", TEMPLATE.len() + 1);
    for offset in offsets {
        let _ = write!(xref, "{offset:010} 00000 n \n");
    }
    let _ = write!(
        xref,
        "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n",
        TEMPLATE.len() + 1
    );
    out.extend_from_slice(xref.as_bytes());
    out
}

fuzz_target!(|data: &[u8]| {
    let file = if data.starts_with(b"%PDF-") {
        data.to_vec()
    } else {
        template(data)
    };
    let budget = Budget::new(&DocumentLimits::web());
    let Ok(base) = FileBase::open(Bytes::new(file), &budget) else {
        return;
    };
    let document = Document::new(Arc::new(base), None);
    let version = document.current();
    let Ok(pages) = PageIndex::build(&mut Reads::new(&version, &budget)) else {
        return;
    };
    let Ok(bridged) = Bridged::new(&version, &pages, &budget).map(Arc::new) else {
        return;
    };
    let count = bridged.pdf().pages().len().min(MAX_PAGES);
    let kinds = ItemKinds {
        text_runs: true,
        paths: true,
        images: true,
    };
    for index in 0..count {
        let options = render::Options::new((0.5, 0.0, 0.0, 0.5), true);
        let _ = render::raster(&bridged, index, &options);
        let _ = render::svg(&bridged, index, true);
        let budget = Budget::new(&DocumentLimits::web());
        let _ = extract::page_content(&bridged, index, kinds, true, None, &budget);
        let options = TextOptions {
            styles: true,
            ..TextOptions::default()
        };
        let _ = extract::page_text(&bridged, index, &options, &budget, &mut String::new());
    }
});
