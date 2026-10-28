//! `page.svg` through hayro-svg over the bridge, with the GIL released
//! (pylopdf rust/src/document/rendering.rs at 036be111).

use pyo3::prelude::*;
use pyropdf_engine::render;
use pyropdf_store::value::Reference;

use crate::document::_Document;
use crate::errors::{LimitError, PdfError, StaleObjectError};
use crate::pixmap::bridge_error;

/// Converts the page whose object is `number` and `generation` to SVG,
/// refusing a document of more than `max_size` UTF-8 bytes.
#[pyfunction]
pub fn render_page_svg(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    annotations: bool,
    max_size: Option<usize>,
) -> PyResult<String> {
    let (version, index, budget) = crate::pages::index(document)?;
    py.detach(|| {
        let at = index
            .index_of(Reference::new(number, generation))
            .ok_or_else(|| {
                StaleObjectError::new_err(format!(
                    "page object {number} {generation} is no longer a page of the document"
                ))
            })?;
        let bridged = document
            .bridge
            .get_for_render(&version, &index, &budget, annotations)
            .map_err(|e| bridge_error(&e))?;
        let svg =
            render::svg(&bridged, at, annotations).map_err(|e| PdfError::new_err(e.to_string()))?;
        if let Some(limit) = max_size
            && svg.len() > limit
        {
            return Err(LimitError::new_err((
                "svg_output_size",
                format!("rendered SVG exceeds the {limit}-byte UTF-8 output limit"),
            )));
        }
        Ok(svg)
    })
}
