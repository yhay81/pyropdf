//! Reading field properties, editing fields, and creating their widgets.

use std::borrow::Cow;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::settle::{Scope, Structure};
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::annotations::{
    AnnotationError as Error, AnnotationResult as Result, PageAnnots, Rect, border_style, rgb,
};
use crate::forms as widgets;
use crate::standard::DA_FONTS;

use super::RADIO;
use super::copy::copy_array;
use super::fill::{After, FillValue, base, change, encode_value, is_button, shows_text};
use super::fill_index::{FillCache, indexed_value};
use super::traversal::{
    Form, Id, MAX_INPUT_BYTES, MAX_VALUE_BYTES, MAX_VALUE_ITEMS, decode, id, inherited, object,
    own, read, stale, text,
};
use super::tree_edit::{edit_array, join};
use super::widget_edit::pdf_font_size;

/// Reads field `id`'s label (`/TU`), maximum length (inherited `/MaxLen`,
/// unless negative), and default value (inherited `/DV`, a name or a text
/// string), all from one version.
pub fn field_details(
    version: &Version,
    budget: &Budget,
    form: &Form,
    id: Id,
) -> Result<(Option<String>, Option<i64>, Option<String>)> {
    form.field(id).ok_or_else(|| stale(id))?;
    let mut reads = Reads::new(version, budget);
    let mut totals = (0, 0);
    let field = object(&mut reads, id)?;
    let label = match field.as_deref() {
        Some(Value::Dict(dict)) => match own(&mut reads, dict, "TU")? {
            Some(label) => text(
                &mut reads,
                label,
                &mut totals,
                MAX_VALUE_BYTES,
                "field label",
            )?,
            None => None,
        },
        _ => None,
    };
    let length = inherited(&mut reads, id, "MaxLen")?;
    let max_length = match read(&mut reads, length.as_ref())? {
        Some(Value::Integer(length)) if length >= 0 => Some(length),
        _ => None,
    };
    let default = inherited(&mut reads, id, "DV")?;
    let default = match read(&mut reads, default.as_ref())? {
        Some(Value::Name(name)) => Some(String::from_utf8_lossy(name.as_bytes()).into_owned()),
        Some(Value::String(encoded)) => Some(decode(
            &encoded,
            &mut totals,
            MAX_VALUE_BYTES,
            "field default value",
            budget,
        )?),
        _ => None,
    };
    Ok((label, max_length, default))
}

/// The `/Ff` bits that decide a field's kind, which an edit keeps: radio,
/// pushbutton, and combo box.
const KIND_FLAGS: i64 = (1 << 15) | (1 << 16) | (1 << 17);

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
pub(super) const FIELD_KEYS: [&str; 11] = [
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
    fn font_resource(&self) -> Result<&'static str> {
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
    fn on_state(&self) -> &str {
        self.on_state.as_deref().unwrap_or("Yes")
    }

    /// Checks what the specification holds on its own, before the edit
    /// reads the document.
    fn check(&self) -> Result<()> {
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
    fn dictionaries(&self, annots: &PageAnnots<'_>, budget: &Budget) -> Result<(Dict, Dict)> {
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
fn options(choices: &[(String, String)], budget: &Budget) -> Result<Value> {
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

/// Adds the field's widget to the page, returning its object and position.
pub fn add_widget(
    transaction: &mut Transaction,
    annots: &PageAnnots<'_>,
    spec: &FieldSpec,
    budget: &Budget,
    cache: &FillCache,
    draft: bool,
) -> Result<(Id, u32)> {
    spec.check()?;
    budget.check(LimitCode::AnnotationCount, (annots.items.len() + 1) as u64)?;
    if !matches!(
        annots.resolved(annots.page().dict().get("Annots"))?,
        None | Some(Value::Array(_))
    ) {
        return Err(Error::Pdf("the page's /Annots is not an array".into()));
    }
    let (widget, field) = spec.dictionaries(annots, budget)?;
    let index = u32::try_from(annots.items.len()).unwrap_or(u32::MAX);
    let id = insert_widget(
        transaction,
        annots,
        spec,
        (widget, field),
        budget,
        (cache, draft),
    )?;
    Ok((id, index))
}

#[expect(
    clippy::disallowed_methods,
    reason = "adds one widget reference to each charged array and copies only the new field's bounded dictionary"
)]
fn insert_widget(
    transaction: &mut Transaction,
    annots: &PageAnnots<'_>,
    spec: &FieldSpec,
    (mut widget, mut field): (Dict, Dict),
    budget: &Budget,
    (cache, draft): (&FillCache, bool),
) -> Result<Id> {
    let before = base(transaction, draft);
    let index = cache.get(&before, budget)?;
    let form = &index.topology.form;
    let mut existing = None;
    for (at, other) in form.fields.iter().enumerate() {
        if at % 1024 == 0 {
            budget.poll()?;
        }
        if other.name == spec.name {
            existing = Some(other);
        } else if other
            .name
            .strip_prefix(&spec.name)
            .is_some_and(|suffix| suffix.starts_with('.'))
            && existing.is_none()
            && !spec.name.contains('.')
        {
            return Err(Error::Pdf(
                "the form already has a non-terminal field of that name".into(),
            ));
        }
    }
    match existing {
        Some(existing) if existing.kind != spec.kind => {
            return Err(Error::Pdf(format!(
                "the form already has a {} field named {:?}",
                existing.kind, spec.name
            )));
        }
        None if spec.name.contains('.') => {
            return Err(Error::Pdf(
                "a new field's name has no dots; dots join the names of a field's parents".into(),
            ));
        }
        _ => {}
    }
    let existing_value = match existing {
        Some(existing) => indexed_value(&index, form, existing.id)?,
        None => None,
    };
    let holder = form_object(transaction, budget)?;
    if shows_text(&spec.kind) || spec.kind == "pushbutton" {
        let resource = register_standard_font(
            transaction,
            holder,
            spec.font_resource()?,
            spec.font_name.as_deref().unwrap_or("Helvetica"),
            budget,
        )?;
        let (red, green, blue) = spec.text_color.unwrap_or((0.0, 0.0, 0.0));
        let size = pdf_font_size(spec.font_size.unwrap_or(0.0), annots.unit)?;
        let appearance = format!("/{resource} {size} Tf {red} {green} {blue} rg");
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /DA key"
        )]
        widget.insert(
            Name::from("DA"),
            Value::String(PdfString::literal(appearance.into_bytes())),
        );
    }
    let reference = transaction.allocate()?;
    let buttons = is_button(&spec.kind);
    let field_id = match existing {
        Some(existing) => {
            let parent = join(transaction, holder, existing.id, budget)?;
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Parent key"
            )]
            widget.insert(Name::from("Parent"), Value::Reference(parent));
            edit_array(transaction, parent, "Kids", budget, true, |kids| {
                kids.push(Value::Reference(reference));
            })?;
            id(parent)
        }
        None if spec.kind == "radio" => {
            let parent = transaction.allocate()?;
            let kids = Array::from(vec![Value::Reference(reference)]);
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Kids key"
            )]
            field.insert(Name::from("Kids"), Value::Array(kids));
            transaction.write(parent.number(), &Value::Dict(field), budget)?;
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Parent key"
            )]
            widget.insert(Name::from("Parent"), Value::Reference(parent));
            edit_array(transaction, holder, "Fields", budget, true, |fields| {
                fields.push(Value::Reference(parent));
            })?;
            id(parent)
        }
        None => {
            for (key, value) in field.iter_mut() {
                widget.insert(key.clone(), std::mem::replace(value, Value::Null));
            }
            edit_array(transaction, holder, "Fields", budget, true, |fields| {
                fields.push(Value::Reference(reference));
            })?;
            id(reference)
        }
    };
    if buttons {
        // Settling draws the `Off` state and the state that `/AS` names.
        let state = Value::Name(Name::from(spec.on_state()));
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /AS key"
        )]
        widget.insert(Name::from("AS"), state);
    }
    transaction.write(reference.number(), &Value::Dict(widget), budget)?;
    let mut items = copy_array(&annots.items, budget)?;
    items.push(Value::Reference(reference));
    annots.write_items(transaction, items, budget)?;
    if buttons {
        transaction.mark(Structure::Appearance, reference.number(), budget)?;
        transaction.settle(Scope::One(Structure::Appearance), budget)?;
    }
    let value = if buttons {
        let selected = spec.value.as_deref() == Some(spec.on_state());
        Some(match existing_value {
            _ if selected => spec.on_state(),
            Some(value) => value,
            None => widgets::OFF,
        })
    } else if shows_text(&spec.kind) {
        spec.value.as_deref()
    } else {
        None
    };
    let value = match value {
        Some(value) => {
            budget.charge(LimitCode::MemorySize, value.len() as u64)?;
            After::Fill(Cow::Owned(FillValue::Text(value.to_owned())))
        }
        None => After::Draw,
    };
    let after = transaction.snapshot();
    change(transaction, field_id, budget, cache, after, |_, _, _| {
        Ok(value)
    })?;
    Ok(id(reference))
}

/// Makes Standard 14 font `base` resolvable in the `/DR` of form `holder`
/// under `preferred`, its `/DA` abbreviation, or a free `preferred_N` when
/// another font holds that name; returns the resource name. Existing
/// resources are copied, not edited through shared references.
#[expect(
    clippy::disallowed_methods,
    reason = "adds one font resource entry to the copied dictionary"
)]
pub(super) fn register_standard_font(
    transaction: &mut Transaction,
    holder: Reference,
    preferred: &str,
    base: &str,
    budget: &Budget,
) -> Result<String> {
    let mut form = dict_at(transaction, holder, budget)?
        .ok_or_else(|| Error::Pdf("the interactive form is not a dictionary".into()))?;
    let snapshot = transaction.snapshot();
    let mut reads = Reads::new(&snapshot, budget);
    let mut resources = match read(&mut reads, form.get("DR"))? {
        Some(Value::Dict(dict)) => dict,
        None => Dict::new(),
        _ => return Err(Error::Pdf("the form's /DR is not a dictionary".into())),
    };
    let mut fonts = match read(&mut reads, resources.get("Font"))? {
        Some(Value::Dict(dict)) => dict,
        None => Dict::new(),
        _ => {
            return Err(Error::Pdf(
                "the form's /DR /Font is not a dictionary".into(),
            ));
        }
    };
    if standard_font_matches(read(&mut reads, fonts.get(preferred))?.as_ref(), base) {
        return Ok(preferred.to_owned());
    }
    let resource = if fonts.get(preferred).is_none() {
        preferred.to_owned()
    } else {
        // Track suffixes in one pass: Dict lookup is linear, so repeatedly
        // searching it for candidate names would take quadratic work.
        let mut occupied = BudgetVec::new(budget);
        occupied.try_reserve_exact(fonts.len().saturating_add(1))?;
        for _ in 0..=fonts.len() {
            occupied.try_push(false)?;
        }
        let prefix = format!("{preferred}_");
        for (name, value) in fonts.iter() {
            budget.poll()?;
            let Some(suffix) = name.as_bytes().strip_prefix(prefix.as_bytes()) else {
                continue;
            };
            // A usize decimal representation is at most 20 ASCII bytes.
            if suffix.len() > 20 {
                continue;
            }
            let Ok(suffix) = std::str::from_utf8(suffix) else {
                continue;
            };
            let Ok(number) = suffix.parse::<usize>() else {
                continue;
            };
            if number == 0 || number.to_string() != suffix {
                continue;
            }
            if standard_font_matches(read(&mut reads, Some(value))?.as_ref(), base) {
                return Ok(format!("{prefix}{suffix}"));
            }
            if number <= fonts.len() {
                occupied[number] = true;
            }
        }
        let suffix = occupied
            .iter()
            .enumerate()
            .skip(1)
            .find(|(_, used)| !**used)
            .map_or(fonts.len().saturating_add(1), |(index, _)| index);
        format!("{preferred}_{suffix}")
    };
    let mut font = Dict::new();
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /Type key"
    )]
    font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /Subtype key"
    )]
    font.insert(Name::from("Subtype"), Value::Name(Name::from("Type1")));
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /BaseFont key"
    )]
    font.insert(Name::from("BaseFont"), Value::Name(Name::from(base)));
    if !matches!(base, "Symbol" | "ZapfDingbats") {
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Encoding key"
        )]
        font.insert(
            Name::from("Encoding"),
            Value::Name(Name::from("WinAnsiEncoding")),
        );
    }
    let reference = transaction.allocate()?;
    transaction.write(reference.number(), &Value::Dict(font), budget)?;
    fonts.insert(Name::from(resource.as_str()), Value::Reference(reference));
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /Font key"
    )]
    resources.insert(Name::from("Font"), Value::Dict(fonts));
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /DR key"
    )]
    form.insert(Name::from("DR"), Value::Dict(resources));
    transaction.write(holder.number(), &Value::Dict(form), budget)?;
    Ok(resource)
}

fn standard_font_matches(value: Option<&Value>, base: &str) -> bool {
    let Some(Value::Dict(font)) = value else {
        return false;
    };
    let symbolic = matches!(base, "Symbol" | "ZapfDingbats");
    font.len() == if symbolic { 3 } else { 4 }
        && font.get("Type") == Some(&Value::Name(Name::from("Font")))
        && font.get("Subtype") == Some(&Value::Name(Name::from("Type1")))
        && font.get("BaseFont") == Some(&Value::Name(Name::from(base)))
        && if symbolic {
            font.get("Encoding").is_none()
        } else {
            font.get("Encoding") == Some(&Value::Name(Name::from("WinAnsiEncoding")))
        }
}

/// Returns the form dictionary's object, after moving one that the catalog
/// holds into an object of its own, or creating an empty one that names
/// Helvetica as `/Helv`.
#[expect(
    clippy::disallowed_methods,
    reason = "only fixed-size literal dictionaries and the constant default-appearance bytes are constructed"
)]
pub(super) fn form_object(transaction: &mut Transaction, budget: &Budget) -> Result<Reference> {
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let Some(Value::Reference(root)) = reads.trailer().get("Root").cloned() else {
        return Err(Error::Pdf("the document has no catalog".into()));
    };
    let Some(catalog) = object(&mut reads, id(root))? else {
        return Err(Error::Pdf("the document has no catalog".into()));
    };
    let Value::Dict(catalog_dict) = &*catalog else {
        return Err(Error::Pdf("the document has no catalog".into()));
    };
    let form = match catalog_dict.get("AcroForm") {
        Some(Value::Reference(reference)) => {
            if let Some(Value::Dict(_)) = object(&mut reads, id(*reference))?.as_deref() {
                return Ok(*reference);
            }
            None
        }
        Some(form @ Value::Dict(_)) => Some(copy_for_edit(form, budget)?),
        _ => None,
    };
    let form = match form {
        Some(form) => form,
        None => {
            let font = transaction.allocate()?;
            let helvetica: Dict = [
                ("Type", "Font"),
                ("Subtype", "Type1"),
                ("BaseFont", "Helvetica"),
                ("Encoding", "WinAnsiEncoding"),
            ]
            .into_iter()
            .map(|(key, value)| (Name::from(key), Value::Name(Name::from(value))))
            .collect();
            transaction.write(font.number(), &Value::Dict(helvetica), budget)?;
            let fonts: Dict = [(Name::from("Helv"), Value::Reference(font))]
                .into_iter()
                .collect();
            let resources: Dict = [(Name::from("Font"), Value::Dict(fonts))]
                .into_iter()
                .collect();
            let appearance = PdfString::literal(b"/Helv 0 Tf 0 g".to_vec());
            Value::Dict(
                [
                    (Name::from("Fields"), Value::Array(Array::new())),
                    (Name::from("DA"), Value::String(appearance)),
                    (Name::from("DR"), Value::Dict(resources)),
                ]
                .into_iter()
                .collect(),
            )
        }
    };
    let Value::Dict(mut catalog) = copy_for_edit(&catalog, budget)? else {
        unreachable!("a copied dictionary remains a dictionary");
    };
    let reference = transaction.allocate()?;
    transaction.write(reference.number(), &form, budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /AcroForm key"
    )]
    catalog.insert(Name::from("AcroForm"), Value::Reference(reference));
    transaction.write(root.number(), &Value::Dict(catalog), budget)?;
    Ok(reference)
}

/// Returns a copy of the dictionary of object `reference` in the
/// transaction's version, charged to `budget`, or `None` when that object,
/// with that generation, is no dictionary.
pub(super) fn dict_at(
    transaction: &Transaction,
    reference: Reference,
    budget: &Budget,
) -> Result<Option<Dict>> {
    let object = transaction.version().get(reference.number(), budget)?;
    match object.as_ref() {
        Some(object)
            if object.generation() == reference.generation()
                && matches!(**object.value(), Value::Dict(_)) =>
        {
            match copy_for_edit(object.value(), budget)? {
                Value::Dict(dict) => Ok(Some(dict)),
                _ => unreachable!("a copied dictionary remains a dictionary"),
            }
        }
        _ => Ok(None),
    }
}
