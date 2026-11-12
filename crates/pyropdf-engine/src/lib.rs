//! The content model and provenance, rendering, writing, understanding, and
//! budgets (DESIGN.md section 11).

#![warn(clippy::allow_attributes_without_reason)]
#![cfg_attr(
    test,
    allow(
        clippy::disallowed_methods,
        reason = "tests grow standard collections freely; F6 binds the engine's own code"
    )
)]

pub mod annotations;
pub mod bridge;
pub mod content;
pub mod display;
pub mod extract;
pub mod fonts;
pub mod forms;
pub mod ocr;
pub mod pages;
pub mod pixmap;
pub mod render;
pub mod sign;
pub mod standard;
