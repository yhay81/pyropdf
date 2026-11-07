//! `page.svg` through hayro-svg over the bridge, with the GIL released
//! (pylopdf rust/src/document/rendering.rs at 036be111).

use pyo3::prelude::*;
use pyropdf_engine::render;
use pyropdf_store::budget::LimitCode;
use pyropdf_store::value::Reference;

use crate::document::_Document;
use crate::errors::StaleObjectError;
use crate::pixmap::{bridge_error, raster_error};

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
    crate::errors::boundary(|| {
        let budget =
            document.call_budget(&[(LimitCode::SvgOutputSize, max_size.map(|n| n as u64))])?;
        let (version, index) = crate::pages::index_with(document, None, &budget)?;
        let fallback = document.fallback();
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
            render::svg(&bridged, &fallback, at, annotations, &budget).map_err(raster_error)
        })
    })?
}
