//! The display space of a page, which rendering and extraction share:
//! the visible page with its origin at the top-left, `y` growing downward,
//! and `/Rotate` applied, measured in points (yhay81/pyropdf#729).
//!
//! hayro's initial transform maps user space to that space in user space
//! units. A page's `/UserUnit` (ISO 32000-2 section 14.11.2) sets the size
//! of that unit, so the display transform scales hayro's by it, read by
//! the rule of [`pyropdf_store::pages::user_unit`] that the page boxes
//! also use.

use hayro::hayro_interpret::TransformExt;
use hayro::hayro_syntax::page::Page;
use hayro::kurbo::Affine;

/// Returns the size of the default user space unit of `page`, in points.
#[must_use]
pub fn user_unit(page: &Page<'_>) -> f64 {
    pyropdf_store::pages::user_unit(page.raw().get::<f64>(b"UserUnit"))
}

/// Returns the width and height of `page` in display space: hayro's
/// render dimensions, in points.
#[must_use]
pub fn size(page: &Page<'_>) -> (f64, f64) {
    let unit = user_unit(page);
    let (width, height) = page.render_dimensions();
    (f64::from(width) * unit, f64::from(height) * unit)
}

/// Returns the transform from the user space of `page` to its display
/// space.
#[must_use]
pub fn transform(page: &Page<'_>) -> Affine {
    Affine::scale(user_unit(page)) * page.initial_transform(true).to_kurbo()
}
