//! Interactive form fields, widgets, and their appearance streams.
//! A text or choice widget draws its value in a Standard 14 or embedded font.
//! A check box or radio button draws its selected state (DESIGN.md section 10).
//! Edits write into the caller's transaction. On failure the caller drops
//! the transaction or rolls it back to the edit's savepoint.
//!
//! Setting a field's value marks its widgets' appearances stale, and
//! [`AppearanceSettler`](crate::annotations::AppearanceSettler) draws each
//! with [`fn@settle`] once per transaction. A widget reads its field's entries
//! along the `/Parent` chain, where a null entry, or a reference to a null
//! or missing object, inherits as an absent one does; the interactive
//! form's `/DA` and `/Q` are the defaults. Ported from pylopdf's
//! `rust/src/form.rs` and the appearances of `rust/src/document/forms.rs`
//! at ebb01d3c4c742046e8ce7fd72957cd15a2933321.

mod appearance;
pub mod copy;
pub mod defaults;
pub mod face;
pub mod field_edit;
pub mod fill;
mod fill_index;
pub mod flatten;
mod form_font;
mod import;
mod radio;
mod read;
pub mod remove;
mod settle;
mod style;
mod text;
mod traversal;
mod tree_edit;
mod values;
pub mod widget_edit;

pub use appearance::{OFF, appearance, face_appearance, restyled};
pub use fill_index::FillCache;
pub use form_font::{MAX_PARENTS, text_appearance};
pub use import::{ImportDefaults, plan_imported_defaults, register_imported};
pub use radio::repair_radio_groups;
pub use read::{
    WidgetRecord, find_widget, form_field, form_field_choices, form_field_read, form_field_widgets,
    form_fields, widget_list, widget_read,
};
pub use settle::{
    Drawing, WidgetAppearance, has_normal_stream, has_state, has_state_stream, is_widget, settle,
    settle_in_unit, with_normal_states, write,
};
pub use text::text_string;
pub use traversal::{Form, walk};
pub use values::{Id, MAX_VALUE_ITEMS};

pub(crate) use text::append_text;

use std::borrow::Cow;

use appearance::RADIO;
use pyropdf_store::{Error, LimitExceeded};
use style::fmt;

use crate::standard::LayoutError;

/// Why a widget's appearance cannot be drawn.
#[derive(Debug)]
pub enum WidgetError {
    /// The widget or its field holds what the appearance cannot show.
    Malformed(Cow<'static, str>),
    /// The value lays out in more lines than [`crate::standard::MAX_LINES`].
    Lines,
    /// A read or the budget refused.
    Store(Error),
}

impl From<Error> for WidgetError {
    fn from(error: Error) -> Self {
        Self::Store(error)
    }
}

impl From<LimitExceeded> for WidgetError {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Store(refusal.into())
    }
}

impl From<LayoutError> for WidgetError {
    fn from(error: LayoutError) -> Self {
        match error {
            LayoutError::Lines => Self::Lines,
            LayoutError::Limit(refusal) => refusal.into(),
            LayoutError::Unfit => malformed("form field text cannot fit inside the widget"),
            other => Self::Malformed(other.to_string().into()),
        }
    }
}

impl std::fmt::Display for WidgetError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Malformed(message) => f.write_str(message),
            Self::Lines => LayoutError::Lines.fmt(f),
            Self::Store(error) => error.fmt(f),
        }
    }
}

const fn malformed(message: &'static str) -> WidgetError {
    WidgetError::Malformed(Cow::Borrowed(message))
}

#[cfg(test)]
mod tests;
