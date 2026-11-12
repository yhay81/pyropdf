//! Editing existing form field properties and resetting field defaults.

use std::borrow::Cow;

use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::value::{Name, Value};
use pyropdf_store::version::Transaction;

use crate::annotations::{AnnotationError as Error, AnnotationResult as Result};
use crate::forms::fill::{After, FillValue, base, change, encode_value, is_button, shows_text};
use crate::forms::fill_index::FillCache;
use crate::forms::traversal::inherited;
use crate::forms::values::{Id, MAX_INPUT_BYTES, MAX_VALUE_BYTES, MAX_VALUE_ITEMS, decode, read};

use super::spec::options;

/// The `/Ff` bits that decide a field's kind, which an edit keeps: radio,
/// pushbutton, and combo box.
pub(super) const KIND_FLAGS: i64 = (1 << 15) | (1 << 16) | (1 << 17);

/// The properties that one edit of a field sets, as `Field._edit` passes
/// them, and the names of those that it removes.
pub struct FieldEdit {
    pub label: Option<String>,
    pub max_length: Option<i64>,
    pub flags: Option<i64>,
    pub choices: Option<Vec<(String, String)>>,
    pub default_value: Option<String>,
    pub reset: bool,
    pub clear: Vec<String>,
}

impl FieldEdit {
    /// Checks what the edit holds on its own, before it reads the document.
    fn check(&self) -> Result<()> {
        let texts: usize = self
            .label
            .iter()
            .chain(&self.default_value)
            .map(String::len)
            .chain(
                self.choices
                    .iter()
                    .flatten()
                    .map(|(value, label)| value.len() + label.len()),
            )
            .sum();
        if texts > MAX_INPUT_BYTES {
            return Err(Error::Limit {
                code: LimitCode::FormFieldInputSize,
                message: format!("field texts exceed the {MAX_INPUT_BYTES}-byte UTF-8 limit"),
            });
        }
        if let Some(flags) = self.flags
            && !(0..=i64::from(u32::MAX)).contains(&flags)
        {
            return Err(Error::Pdf("field flags are a 32-bit value".into()));
        }
        if let Some(property) = self
            .clear
            .iter()
            .find(|property| cleared(property).is_none())
        {
            return Err(Error::Pdf(format!(
                "a field property that cannot be removed: {property:?}"
            )));
        }
        Ok(())
    }
}

/// Returns the key of a field property that an edit can remove.
fn cleared(property: &str) -> Option<&'static str> {
    match property {
        "label" => Some("TU"),
        "max_length" => Some("MaxLen"),
        "default_value" => Some("DV"),
        _ => None,
    }
}

/// Applies a field edit and redraws its widgets in the transaction.
pub fn edit_field(
    transaction: &mut Transaction,
    id: Id,
    edit: &FieldEdit,
    budget: &Budget,
    cache: &FillCache,
    draft: bool,
) -> Result<()> {
    edit.check()?;
    let before = base(transaction, draft);
    change(
        transaction,
        id,
        budget,
        cache,
        before,
        |field, reads, dict| {
            let buttons = is_button(field.kind);
            if let Some(flags) = edit.flags
                && flags & KIND_FLAGS != field.flags & KIND_FLAGS
            {
                return Err(Error::Pdf("the radio, pushbutton, and combo box flags decide a field's kind; create a field of the other kind instead".into()));
            }
            if let Some(length) = edit.max_length
                && (field.kind != "text" || !(0..=1_000_000).contains(&length))
            {
                return Err(Error::Pdf(
                    "only a text field has a maximum length, from 0 to 1000000".into(),
                ));
            }
            if let Some(choices) = &edit.choices
                && (!matches!(field.kind, "combobox" | "listbox")
                    || choices.len() > MAX_VALUE_ITEMS)
            {
                return Err(Error::Pdf(format!(
                    "only a choice field has choices, at most {MAX_VALUE_ITEMS}"
                )));
            }
            // A reset fills the default value that the field had before the edit.
            let default = if edit.reset {
                inherited(reads, id, "DV")?
            } else {
                None
            };
            let default = match read(reads, default.as_ref())? {
                Some(Value::Name(state)) if buttons => Some(FillValue::Text(
                    String::from_utf8_lossy(state.as_bytes()).into_owned(),
                )),
                Some(Value::String(encoded)) => Some(FillValue::Text(decode(
                    &encoded,
                    &mut (0, 0),
                    MAX_VALUE_BYTES,
                    "field default value",
                    budget,
                )?)),
                _ if buttons && edit.reset => Some(FillValue::Flag(false)),
                _ => None,
            };
            if let Some(label) = &edit.label {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /TU key"
                )]
                dict.insert(Name::from("TU"), encode_value(label, budget)?);
            }
            if let Some(length) = edit.max_length {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /MaxLen key"
                )]
                dict.insert(Name::from("MaxLen"), Value::Integer(length));
            }
            if let Some(flags) = edit.flags {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /Ff key"
                )]
                dict.insert(Name::from("Ff"), Value::Integer(flags));
            }
            if let Some(value) = &edit.default_value {
                let value = if matches!(field.kind, "checkbox" | "radio" | "pushbutton") {
                    Value::Name(Name::from(value.as_str()))
                } else {
                    encode_value(value, budget)?
                };
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /DV key"
                )]
                dict.insert(Name::from("DV"), value);
            }
            if let Some(choices) = &edit.choices {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /Opt key"
                )]
                dict.insert(Name::from("Opt"), options(choices, budget)?);
            }
            for property in &edit.clear {
                if let Some(key) = cleared(property) {
                    dict.remove(key);
                }
            }
            let fillable = buttons || shows_text(field.kind);
            if edit.reset && fillable && default.is_none() {
                dict.remove("V");
            }
            Ok(match default {
                _ if !fillable => After::Keep,
                Some(value) => After::Fill(Cow::Owned(value)),
                None => After::Draw,
            })
        },
    )
}
