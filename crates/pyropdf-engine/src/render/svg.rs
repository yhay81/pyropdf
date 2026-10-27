//! Converting a page to SVG through hayro-svg, as pylopdf's `page.svg` does
//! (yhay81/pyropdf#120).

use hayro::hayro_interpret::InterpreterSettings;

use super::RasterError;
use crate::bridge::Bridged;

/// Converts page `index` to an SVG document at 72 dpi, with text drawn as
/// paths and, unless `annotations` is false, annotation appearances.
///
/// hayro-svg builds the whole document before returning it, so a caller's
/// size limit applies to the result, not to the conversion's memory.
pub fn svg(bridged: &Bridged, index: usize, annotations: bool) -> Result<String, RasterError> {
    let pages = bridged.pdf().pages();
    let page = pages.get(index).ok_or(RasterError::NoPage(index))?;
    let settings = InterpreterSettings {
        render_annotations: annotations,
        ..InterpreterSettings::default()
    };
    Ok(hayro_svg::convert(
        page,
        &hayro_svg::RenderCache::new(),
        &settings,
        &hayro_svg::SvgRenderSettings::default(),
    ))
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::Path;

    use pyropdf_store::budget::Budget;
    use pyropdf_store::index::Index;
    use pyropdf_store::memo::Reads;
    use pyropdf_store::pages::PageIndex;
    use pyropdf_store::save::FileBase;
    use pyropdf_store::value::Bytes;
    use pyropdf_store::version::Document;

    use super::*;

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
        assert_eq!(files.len(), 14);
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
                    for at in 0..pages.len() {
                        for annotations in [true, false] {
                            let ours = svg(&bridged, at, annotations).unwrap();
                            let theirs = svg(&original, at, annotations).unwrap();
                            assert!(ours == theirs, "{} page {at}", path.display());
                            assert!(ours.starts_with("<svg"));
                        }
                    }
                    assert_eq!(
                        svg(&bridged, pages.len(), true),
                        Err(RasterError::NoPage(pages.len()))
                    );
                });
            }
        });
    }
}
