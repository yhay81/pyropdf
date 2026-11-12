//! Validating new-field specifications and building widget/field dictionaries.

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::{Array, Dict, Name, PdfString, Value};

use crate::annotations::{
    AnnotationError as Error, AnnotationResult as Result, PageAnnots, Rect, border_style, rgb,
};
use crate::forms::fill::{encode_value, is_button, shows_text};
use crate::forms::values::{MAX_INPUT_BYTES, MAX_VALUE_ITEMS};
use crate::forms::widget_edit::pdf_font_size;
use crate::forms::{self as widgets, RADIO};
use crate::standard::DA_FONTS;

use super::edit::KIND_FLAGS;

/// A field to create with its first widget, or a widget to add to the
/// field of that name and kind, as `page.widgets.add` passes a field
/// specification. Colours are RGB from 0 to 1, the border is its width,
/// `/S` style, and dash lengths, `font_name` is a Standard 14 font, and
/// `align` is a `/Q` code.
pub struct FieldSpec {
    /// `text`, `checkbox`, `radio`, `combobox`, `listbox`, `pushbutton`, or
    /// `signature`.
    pub kind: String,
    pub name: String,
    pub rect: Rect,
    pub value: Option<String>,
    pub default_value: Option<String>,
    pub flags: i64,
    pub label: Option<String>,
    pub max_length: Option<i64>,
    pub choices: Option<Vec<(String, String)>>,
    pub on_state: Option<String>,
    pub caption: Option<String>,
    pub border: Option<(f64, String, Vec<f64>)>,
    pub border_color: Option<(f64, f64, f64)>,
    pub background_color: Option<(f64, f64, f64)>,
    pub text_color: Option<(f64, f64, f64)>,
    pub font_name: Option<String>,
    pub font_size: Option<f64>,
    pub align: Option<u8>,
}

/// The `/Ff` bit of a radio group that keeps one button on.
const NO_TOGGLE_TO_OFF: i64 = 1 << 14;

/// The field entries that a field merged with its widget gives to the new
/// parent that it gets with a second widget.
pub(in crate::forms) const FIELD_KEYS: [&str; 11] = [
    "FT", "T", "TU", "TM", "Ff", "V", "DV", "Opt", "MaxLen", "TI", "I",
];

/// Returns the `/FT` and the `/Ff` bits of a field of `kind`.
fn field_type(kind: &str) -> Result<(&'static str, i64)> {
    Ok(match kind {
        "text" => ("Tx", 0),
        "checkbox" => ("Btn", 0),
        // Radio groups keep one button on, as Acrobat creates them.
        "radio" => ("Btn", RADIO | NO_TOGGLE_TO_OFF),
        "pushbutton" => ("Btn", 1 << 16),
        "combobox" => ("Ch", 1 << 17),
        "listbox" => ("Ch", 0),
        "signature" => ("Sig", 0),
        other => return Err(Error::Pdf(format!("unknown field kind {other:?}"))),
    })
}

impl FieldSpec {
    /// Returns the `/DA` resource name of the specification's Standard 14
    /// font, Helvetica by default.
    pub(super) fn font_resource(&self) -> Result<&'static str> {
        let Some(name) = &self.font_name else {
            return Ok("Helv");
        };
        DA_FONTS
            .iter()
            .find(|(_, font)| font == name)
            .map(|(resource, _)| *resource)
            .ok_or_else(|| Error::Pdf(format!("unknown Standard 14 font {name:?}")))
    }

    /// Returns the on state of a check box or radio button.
    pub(super) fn on_state(&self) -> &str {
        self.on_state.as_deref().unwrap_or("Yes")
    }

    /// Checks what the specification holds on its own, before the edit
    /// reads the document.
    pub(super) fn check(&self) -> Result<()> {
        let (_, _) = field_type(&self.kind)?;
        let texts: usize = [
            Some(&self.name),
            self.value.as_ref(),
            self.default_value.as_ref(),
            self.label.as_ref(),
            self.on_state.as_ref(),
            self.caption.as_ref(),
        ]
        .into_iter()
        .flatten()
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
        if self.name.is_empty() {
            return Err(Error::Pdf("a field has a name".into()));
        }
        if !(0..=i64::from(u32::MAX)).contains(&self.flags)
            || self.flags & (KIND_FLAGS | NO_TOGGLE_TO_OFF) != 0
        {
            return Err(Error::Pdf(
                "field flags are a 32-bit value without the bits that the kind sets".into(),
            ));
        }
        let (x0, y0, x1, y1) = self.rect;
        if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) || x1 <= x0 || y1 <= y0 {
            return Err(Error::Pdf(
                "a widget rectangle is finite and not empty".into(),
            ));
        }
        if is_button(&self.kind) && matches!(self.on_state(), "" | widgets::OFF) {
            return Err(Error::Pdf(
                "a button's on state is a name other than Off".into(),
            ));
        }
        if let Some(length) = self.max_length
            && (self.kind != "text" || !(0..=1_000_000).contains(&length))
        {
            return Err(Error::Pdf(
                "only a text field has a maximum length, from 0 to 1000000".into(),
            ));
        }
        if let Some(choices) = &self.choices {
            if !matches!(self.kind.as_str(), "combobox" | "listbox") {
                return Err(Error::Pdf("only a choice field has choices".into()));
            }
            if choices.len() > MAX_VALUE_ITEMS {
                return Err(Error::Pdf(format!(
                    "a choice field has at most {MAX_VALUE_ITEMS} choices"
                )));
            }
        }
        if shows_text(&self.kind) || self.kind == "pushbutton" {
            rgb(self.text_color.unwrap_or((0.0, 0.0, 0.0)), "text colour")?;
            self.font_resource()?;
            let size = self.font_size.unwrap_or(0.0);
            if !(size.is_finite() && (0.0..=10_000.0).contains(&size)) {
                return Err(Error::Pdf(
                    "a widget font size is from 0, which fits the text, to 10000".into(),
                ));
            }
            if self.align.is_some_and(|align| align > 2) {
                return Err(Error::Pdf(
                    "a field aligns left (0), centre (1), or right (2)".into(),
                ));
            }
        }
        Ok(())
    }

    /// Returns the widget dictionary and the field dictionary that the
    /// checked specification makes on the page of `annots`, before either
    /// joins the document.
    #[expect(
        clippy::disallowed_methods,
        reason = "the collected dictionary entries come from fixed-size literal arrays"
    )]
    pub(super) fn dictionaries(
        &self,
        annots: &PageAnnots<'_>,
        budget: &Budget,
    ) -> Result<(Dict, Dict)> {
        let (field_type, kind_flags) = field_type(&self.kind)?;
        let mut widget: Dict = [
            ("Type", Value::Name(Name::from("Annot"))),
            ("Subtype", Value::Name(Name::from("Widget"))),
            ("Rect", annots.to_pdf(self.rect)),
            ("F", Value::Integer(4)),
        ]
        .into_iter()
        .map(|(key, value)| (Name::from(key), value))
        .collect();
        if let Some(page) = annots.page().object() {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /P key"
            )]
            widget.insert(Name::from("P"), Value::Reference(page));
        }
        let mut mark = Dict::new();
        if let Some(color) = self.border_color {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /BC key"
            )]
            mark.insert(Name::from("BC"), rgb(color, "border colour")?);
        }
        if let Some(color) = self.background_color {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /BG key"
            )]
            mark.insert(Name::from("BG"), rgb(color, "background colour")?);
        }
        if let Some(caption) = &self.caption {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /CA key"
            )]
            mark.insert(Name::from("CA"), encode_value(caption, budget)?);
        }
        // A widget on a turned page shows upright.
        if annots.rotation != 0 {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /R key"
            )]
            mark.insert(Name::from("R"), Value::Integer(annots.rotation));
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /MK key"
        )]
        widget.insert(Name::from("MK"), Value::Dict(mark));
        let solid = (1.0, "S".to_owned(), Vec::new());
        let border = self.border.as_ref().unwrap_or(&solid);
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /BS key"
        )]
        widget.insert(Name::from("BS"), border_style(border, annots.unit, budget)?);
        if shows_text(&self.kind) || self.kind == "pushbutton" {
            let (red, green, blue) = self.text_color.unwrap_or((0.0, 0.0, 0.0));
            let size = pdf_font_size(self.font_size.unwrap_or(0.0), annots.unit)?;
            let resource = self.font_resource()?;
            let appearance = format!("/{resource} {size} Tf {red} {green} {blue} rg");
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /DA key"
            )]
            widget.insert(
                Name::from("DA"),
                Value::String(PdfString::literal(appearance.into_bytes())),
            );
            if let Some(align) = self.align {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /Q key"
                )]
                widget.insert(Name::from("Q"), Value::Integer(i64::from(align)));
            }
        }
        let mut field: Dict = [
            ("FT", Value::Name(Name::from(field_type))),
            ("T", encode_value(&self.name, budget)?),
        ]
        .into_iter()
        .map(|(key, value)| (Name::from(key), value))
        .collect();
        if kind_flags | self.flags != 0 {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Ff key"
            )]
            field.insert(Name::from("Ff"), Value::Integer(kind_flags | self.flags));
        }
        if let Some(label) = &self.label {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /TU key"
            )]
            field.insert(Name::from("TU"), encode_value(label, budget)?);
        }
        if let Some(length) = self.max_length {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /MaxLen key"
            )]
            field.insert(Name::from("MaxLen"), Value::Integer(length));
        }
        if let Some(choices) = &self.choices {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Opt key"
            )]
            field.insert(Name::from("Opt"), options(choices, budget)?);
        }
        if let Some(default) = &self.default_value {
            let default = if field_type == "Btn" {
                Value::Name(Name::from(default.as_str()))
            } else {
                encode_value(default, budget)?
            };
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /DV key"
            )]
            field.insert(Name::from("DV"), default);
        }
        if shows_text(&self.kind)
            && let Some(value) = &self.value
        {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /V key"
            )]
            field.insert(Name::from("V"), encode_value(value, budget)?);
        }
        Ok((widget, field))
    }
}

/// Returns the `/Opt` array of `choices`: a text string for an option whose
/// label is its export value, else an array of the two.
pub(super) fn options(choices: &[(String, String)], budget: &Budget) -> Result<Value> {
    let mut options = BudgetVec::new(budget);
    for (value, label) in choices {
        let option = if value == label {
            encode_value(value, budget)?
        } else {
            let mut pair = BudgetVec::new(budget);
            pair.try_push(encode_value(value, budget)?)?;
            pair.try_push(encode_value(label, budget)?)?;
            Value::Array(Array::from(pair.into_vec()))
        };
        options.try_push(option)?;
    }
    Ok(Value::Array(Array::from(options.into_vec())))
}
