//! Converting a page to SVG through hayro-svg, as pylopdf's `page.svg` does
//! (yhay81/pyropdf#120).

use crate::display::Display;
use hayro::hayro_interpret::{InterpreterSettings, RenderContent};
use hayro::kurbo::Point;
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, LimitCode};

use super::{Fallback, RasterError};
use crate::bridge::Bridged;

/// Converts page `index` to an SVG document at 72 dpi, with text drawn as
/// paths and, unless `annotations` is false, annotation appearances;
/// `fallback` draws the CJK fonts that the document does not embed. Its
/// width and height are the page's display size in points.
///
/// hayro-svg builds the whole document before returning it, so a caller's
/// size limit applies to the result, not to the conversion's memory.
pub fn svg(
    bridged: &Bridged,
    fallback: &Fallback,
    index: usize,
    annotations: bool,
    budget: &Budget,
) -> Result<String, RasterError> {
    let (page, display) = bridged.page(index).ok_or(RasterError::NoPage(index))?;
    let settings = InterpreterSettings {
        render_content: if annotations {
            RenderContent::all()
        } else {
            RenderContent::PAGE
        },
        ..fallback.settings()
    };
    let converted = hayro_svg::convert(
        page,
        &hayro_svg::RenderCache::new(),
        &settings,
        &hayro_svg::SvgRenderSettings::default(),
    );
    if display.unit == 1.0 {
        budget.check(LimitCode::SvgOutputSize, converted.len() as u64)?;
        return Ok(converted);
    }
    // hayro-svg paints in its native view. Locate the true top-left of the
    // crop there, including any offset from hayro's one-unit minimum.
    let native = Display::native(page);
    let origin = native.user_to_view * (display.user_to_view.inverse() * Point::ORIGIN);
    let (vw, vh) = display.view_size;
    let (width, height) = display.size();
    let attributes = format!(
        r#"viewBox="{} {} {vw} {vh}" width="{width}" height="{height}""#,
        origin.x, origin.y,
    );
    set_viewport(converted, &attributes, budget)
}

/// Replaces hayro-svg's three leading viewport attributes in the original
/// buffer. Check the final output length before reserving any extra bytes.
fn set_viewport(mut svg: String, attributes: &str, budget: &Budget) -> Result<String, RasterError> {
    let header = svg
        .split_once('>')
        .map_or(svg.as_str(), |(header, _)| header);
    let end = header
        .find(" xmlns=")
        .filter(|_| header.starts_with("<svg viewBox="))
        .ok_or_else(|| RasterError::Geometry("missing SVG viewport".to_owned()))?;
    let range = "<svg ".len()..end;
    let len = (svg.len() - range.len())
        .checked_add(attributes.len())
        .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
    budget.check(LimitCode::SvgOutputSize, len as u64)?;
    if len > svg.capacity() {
        budget.charge(LimitCode::MemorySize, (len - svg.capacity()) as u64)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "only the charged increase over the converter's existing buffer is reserved"
        )]
        svg.try_reserve_exact(len - svg.len())
            .map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
    }
    svg.replace_range(range, attributes);
    Ok(svg)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::Path;

    use pyropdf_store::index::Index;
    use pyropdf_store::memo::Reads;
    use pyropdf_store::pages::PageIndex;
    use pyropdf_store::save::FileBase;
    use pyropdf_store::value::Bytes;
    use pyropdf_store::version::Document;

    use super::*;

    #[test]
    fn viewport_edits_reuse_capacity_and_check_limits_before_growing() {
        let original = r#"<svg viewBox="0 0 1 1" width="1" height="1" xmlns="svg"><path/></svg>"#;
        let viewport = r#"viewBox="0 0 1 1" width="30000" height="30000""#;
        let expected =
            original.replace(r#"width="1" height="1""#, r#"width="30000" height="30000""#);
        let no_growth = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
        let mut svg = String::with_capacity(expected.len());
        svg.push_str(original);
        let buffer = svg.as_ptr();
        let result = set_viewport(svg, viewport, &no_growth).unwrap();
        assert_eq!(result, expected);
        assert_eq!(result.as_ptr(), buffer);

        let exact = || original.to_owned().into_boxed_str().into_string();
        let error = set_viewport(exact(), viewport, &no_growth).unwrap_err();
        assert!(
            matches!(error, RasterError::Limit(limit) if limit.code() == LimitCode::MemorySize)
        );
        let capped =
            no_growth.with_limit(LimitCode::SvgOutputSize, Some(expected.len() as u64 - 1));
        let error = set_viewport(exact(), viewport, &capped).unwrap_err();
        assert!(
            matches!(error, RasterError::Limit(limit) if limit.code() == LimitCode::SvgOutputSize)
        );
    }

    /// The bridge converts every corpus page as hayro-svg converts the
    /// original bytes, which is what pylopdf converts for an unedited
    /// document.
    #[test]
    fn corpus_pages_convert_as_the_original_bytes_do() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
        let mut files: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
            .collect();
        files.sort();
        assert_eq!(files.len(), 15);
        std::thread::scope(|scope| {
            for path in &files {
                scope.spawn(move || {
                    let bytes = fs::read(path).unwrap();
                    let budget = Budget::default();
                    let index = Index::open(Bytes::new(bytes.clone()), &budget).unwrap();
                    let base = FileBase::new(index, &budget).unwrap();
                    let document = Document::new(std::sync::Arc::new(base), None);
                    let version = document.current();
                    let pages = PageIndex::build(&mut Reads::new(&version, &budget)).unwrap();
                    let bridged = Bridged::new(&version, &pages, &budget).unwrap();
                    let original = Bridged::of_bytes(bytes);
                    let none = Fallback::default();
                    for at in 0..pages.len() {
                        for annotations in [true, false] {
                            let ours = svg(&bridged, &none, at, annotations, &budget).unwrap();
                            let theirs = svg(&original, &none, at, annotations, &budget).unwrap();
                            assert!(ours == theirs, "{} page {at}", path.display());
                            assert!(ours.starts_with("<svg"));
                        }
                    }
                    assert_eq!(
                        svg(&bridged, &none, pages.len(), true, &budget),
                        Err(RasterError::NoPage(pages.len()))
                    );
                });
            }
        });
    }
}
