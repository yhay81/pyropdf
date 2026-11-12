//! Reading field properties, editing fields, creating their widgets, and
//! bootstrapping `/AcroForm` default resources.

mod acroform;
mod details;
mod edit;
mod insert;
mod spec;

pub use details::field_details;
pub use edit::{FieldEdit, edit_field};
pub use insert::add_widget;
pub use spec::FieldSpec;

pub(super) use acroform::{dict_at, form_object, register_standard_font};
pub(super) use spec::FIELD_KEYS;
