//! The appearance streams of form widgets: the value of a text or choice
//! field in a Standard 14 font, and the states of a check box or radio
//! button, over the widget's background and border (DESIGN.md section 10).
//!
//! Setting a field's value marks its widgets' appearances stale, and
//! [`AppearanceSettler`](crate::annotations::AppearanceSettler) draws each
//! with [`settle`] once per transaction. A widget reads its field's entries
//! along the `/Parent` chain, where a null entry, or a reference to a null
//! or missing object, inherits as an absent one does; the interactive
//! form's `/DA` and `/Q` are the defaults. Ported from pylopdf's
//! `rust/src/form.rs` and the appearances of `rust/src/document/forms.rs`
//! at ebb01d3c4c742046e8ce7fd72957cd15a2933321.

mod text;

pub use text::text_string;

use std::borrow::Cow;
use std::fmt;
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::{Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::Transaction;
use pyropdf_store::{Error, LimitExceeded, Result};

use crate::annotations::{Reader, numbers_array};
use crate::standard::{
    self, Align, Base14Font, DefaultAppearance, DeviceColor, FIT_STEP, LayoutError, MAX_FIT_SIZE,
    MIN_FIT_SIZE, Style,
};

/// The most fields of a `/Parent` chain that a widget reads entries from.
pub const MAX_PARENTS: usize = 64;

/// The field flags that a text field's layout depends on (ISO 32000-2
/// Table 229).
const MULTILINE: i64 = 1 << 12;
const PASSWORD: i64 = 1 << 13;
const FILE_SELECT: i64 = 1 << 20;
const COMB: i64 = 1 << 24;
/// The flags of a button field that make it a radio button or a
/// pushbutton (ISO 32000-2 Table 226).
const RADIO: i64 = 1 << 15;
const PUSHBUTTON: i64 = 1 << 16;
/// The appearance state of a button that is not selected.
pub const OFF: &str = "Off";

/// The field entries that a widget's appearance reads, in [`Entries`]
/// order.
const KEYS: [&str; 5] = ["FT", "Ff", "V", "Q", "MaxLen"];

/// The entries of [`KEYS`] that a widget's field holds or inherits.
type Entries = [Option<Arc<Value>>; 5];

/// Why a widget's appearance cannot be drawn.
#[derive(Debug)]
pub enum WidgetError {
    /// The widget or its field holds what the appearance cannot show.
    Malformed(Cow<'static, str>),
    /// The value lays out in more lines than [`standard::MAX_LINES`].
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

impl fmt::Display for WidgetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
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

/// Returns whether the appearance settler draws `dict` as a widget: its
/// `/Subtype` is `Widget`, or it has none, as a field merged with its
/// widget may.
#[must_use]
pub fn is_widget(dict: &Dict) -> bool {
    match dict.get("Subtype") {
        Some(Value::Name(name)) => name.as_bytes() == b"Widget",
        None => true,
        Some(_) => false,
    }
}

/// What the settler writes for a widget.
pub enum Drawing {
    /// The value holds a character outside WinAnsi, which no Standard 14
    /// font draws: the widget's normal appearance is removed, so that none
    /// shows a stale value.
    Removed,
    /// The widget's new normal appearance.
    Drawn(WidgetAppearance),
    /// The normal appearances that a check box or radio button widget
    /// lacks, by state: `Off` and the state that its `/AS` names. A state
    /// that holds content is kept.
    States(Vec<(Name, WidgetAppearance)>),
}

/// A drawn normal appearance of a widget: its content in the form's space,
/// the form's size and the matrix that rotates it, and the Standard 14 font
/// that its text names, by resource and PostScript name.
pub struct WidgetAppearance {
    content: Vec<u8>,
    size: (f64, f64),
    matrix: Option<[f64; 6]>,
    font: Option<(String, &'static str)>,
}

impl WidgetAppearance {
    /// Writes the font and the form XObject of the appearance as new
    /// objects, and returns the form's reference.
    fn write(self, transaction: &mut Transaction, budget: &Budget) -> Result<Reference> {
        let mut dict = Dict::new();
        dict.insert(Name::from("Type"), Value::Name(Name::from("XObject")));
        dict.insert(Name::from("Subtype"), Value::Name(Name::from("Form")));
        dict.insert(Name::from("FormType"), Value::Integer(1));
        dict.insert(
            Name::from("BBox"),
            numbers_array(&[0.0, 0.0, self.size.0, self.size.1]),
        );
        if let Some(matrix) = self.matrix {
            dict.insert(Name::from("Matrix"), numbers_array(&matrix));
        }
        if let Some((resource, base_font)) = &self.font {
            let mut font = Dict::new();
            font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
            font.insert(Name::from("Subtype"), Value::Name(Name::from("Type1")));
            font.insert(Name::from("BaseFont"), Value::Name(Name::from(*base_font)));
            if !matches!(*base_font, "Symbol" | "ZapfDingbats") {
                font.insert(
                    Name::from("Encoding"),
                    Value::Name(Name::from("WinAnsiEncoding")),
                );
            }
            let reference = transaction.allocate()?;
            transaction.write(reference.number(), &Value::Dict(font), budget)?;
            let mut fonts = Dict::new();
            fonts.insert(Name::from(resource.as_str()), Value::Reference(reference));
            let mut resources = Dict::new();
            resources.insert(Name::from("Font"), Value::Dict(fonts));
            dict.insert(Name::from("Resources"), Value::Dict(resources));
        }
        budget.charge(LimitCode::MemorySize, self.content.len() as u64)?;
        let form = Stream::new(dict, Bytes::new(self.content));
        let reference = transaction.allocate()?;
        transaction.write(reference.number(), &Value::from(form), budget)?;
        Ok(reference)
    }
}

/// Draws the appearance of widget `number`, `widget`, for the appearance
/// settler: a widget of a text or choice field gets a new normal appearance,
/// keeping its down and rollover ones, a check box or radio button gets the
/// states that it lacks, and a widget that cannot be drawn keeps what it
/// has. Refuses only with what a read or the budget refuses.
pub fn settle(
    transaction: &mut Transaction,
    number: u32,
    widget: &Dict,
    budget: &Budget,
) -> Result<()> {
    let reader = Reader {
        version: transaction.version(),
        budget,
    };
    let drawing = match appearance(&reader, widget) {
        Ok(Some(drawing)) => drawing,
        Ok(None) | Err(WidgetError::Malformed(_) | WidgetError::Lines) => return Ok(()),
        Err(WidgetError::Store(error)) => return Err(error),
    };
    let mut appearances = match reader.entry(widget, "AP")?.as_deref() {
        Some(Value::Dict(appearances)) => appearances.clone(),
        _ => Dict::new(),
    };
    match drawing {
        Drawing::Removed => {
            if appearances.remove("N").is_none() {
                return Ok(());
            }
        }
        Drawing::Drawn(drawn) => {
            let reference = drawn.write(transaction, budget)?;
            appearances.insert(Name::from("N"), Value::Reference(reference));
        }
        Drawing::States(states) => {
            let mut normal = match appearances.get("N") {
                Some(normal) => match &*reader.resolve(normal)? {
                    Value::Dict(normal) => normal.clone(),
                    _ => Dict::new(),
                },
                None => Dict::new(),
            };
            for (state, drawn) in states {
                let reference = drawn.write(transaction, budget)?;
                normal.insert(state, Value::Reference(reference));
            }
            appearances.insert(Name::from("N"), Value::Dict(normal));
        }
    }
    let mut widget = widget.clone();
    if appearances.is_empty() {
        widget.remove("AP");
    } else {
        widget.insert(Name::from("AP"), Value::Dict(appearances));
    }
    transaction.write(number, &Value::Dict(widget), budget)?;
    Ok(())
}

/// Returns whether `widget` has a normal appearance stream with content, as
/// a widget of a text or choice field needs to show its value.
pub fn has_normal_stream(reader: &Reader<'_>, widget: &Dict) -> Result<bool> {
    let appearances = reader.entry(widget, "AP")?;
    let Some(Value::Dict(appearances)) = appearances.as_deref() else {
        return Ok(false);
    };
    match appearances.get("N") {
        Some(normal) => Ok(has_content(&*reader.resolve(normal)?)),
        None => Ok(false),
    }
}

/// Returns whether the normal appearance of the state that a check box or
/// radio button widget's `/AS` names is a stream with content.
pub fn has_state_stream(reader: &Reader<'_>, widget: &Dict) -> Result<bool> {
    let Some(Value::Name(state)) = widget.get("AS") else {
        return Ok(false);
    };
    has_state(reader, widget, state.as_bytes())
}

/// Returns whether the normal appearance of `state` of a check box or radio
/// button widget is a stream with content.
pub fn has_state(reader: &Reader<'_>, widget: &Dict, state: &[u8]) -> Result<bool> {
    let normal = normal_states(reader, widget)?;
    let Some(Value::Dict(states)) = normal.as_deref() else {
        return Ok(false);
    };
    match states.get(state) {
        Some(normal) => Ok(has_content(&*reader.resolve(normal)?)),
        None => Ok(false),
    }
}

/// Returns the normal appearance dictionary of a check box or radio button
/// widget, whose keys are its states, or `None` when it has none.
pub fn normal_states(reader: &Reader<'_>, widget: &Dict) -> Result<Option<Arc<Value>>> {
    let appearances = reader.entry(widget, "AP")?;
    let Some(Value::Dict(appearances)) = appearances.as_deref() else {
        return Ok(None);
    };
    let normal = reader.entry(appearances, "N")?;
    Ok(normal.filter(|normal| matches!(**normal, Value::Dict(_))))
}

fn has_content(value: &Value) -> bool {
    matches!(value, Value::Stream(stream) if !stream.data().is_empty())
}

/// How a field's value is laid out.
#[derive(Clone, Copy)]
enum TextLayout {
    SingleLine,
    Multiline,
    /// A comb of this many cells.
    Comb(usize),
}

/// Draws the normal appearance of `widget` from the value of its field,
/// the states that a check box or radio button widget lacks, or the border
/// and `/MK` caption of a pushbutton or signature widget that lacks a
/// normal appearance; returns `None` for a widget of another field, or one
/// that lacks nothing.
///
/// Refuses a widget without a usable `/Rect`, a malformed comb field, a
/// comb value with more graphemes than `/MaxLen`, and text that does not
/// fit, as pylopdf does when it fills the field.
pub fn appearance(reader: &Reader<'_>, widget: &Dict) -> Result<Option<Drawing>, WidgetError> {
    let entries = entries(reader, widget)?;
    let [field_type, flags, value, quadding, max_len] = &entries;
    let flags = integer(flags.as_deref()).unwrap_or(0);
    let text_field = match field_type.as_deref() {
        Some(Value::Name(name)) if name.as_bytes() == b"Tx" => true,
        Some(Value::Name(name)) if name.as_bytes() == b"Ch" => false,
        Some(Value::Name(name)) if name.as_bytes() == b"Btn" && flags & PUSHBUTTON == 0 => {
            return button_states(reader, widget, flags & RADIO != 0);
        }
        Some(Value::Name(name)) if matches!(name.as_bytes(), b"Btn" | b"Sig") => {
            if has_normal_stream(reader, widget)? {
                return Ok(None);
            }
            let style = WidgetStyle::read(reader, widget)?;
            let caption = match reader.entry(widget, "MK")?.as_deref() {
                Some(Value::Dict(mark)) if name.as_bytes() == b"Btn" => {
                    value_text(reader, reader.entry(mark, "CA")?.as_deref())?
                }
                _ => String::new(),
            };
            let caption = if standard::is_winansi(&caption) {
                caption
            } else {
                String::new()
            };
            let form = interactive_form(reader)?;
            let form = match form.as_deref() {
                Some(Value::Dict(form)) => Some(form),
                _ => None,
            };
            return drawn(
                reader,
                widget,
                form,
                &style,
                &caption,
                TextLayout::SingleLine,
                Align::Center,
            )
            .map(Some);
        }
        _ => return Ok(None),
    };
    let layout = if !text_field {
        TextLayout::SingleLine
    } else if flags & COMB != 0 {
        if flags & (MULTILINE | PASSWORD | FILE_SELECT) != 0 {
            return Err(malformed(
                "comb fields cannot also be multiline, password, or file-select fields",
            ));
        }
        let max_len = integer(max_len.as_deref())
            .and_then(|max_len| usize::try_from(max_len).ok())
            .filter(|&max_len| max_len > 0)
            .ok_or_else(|| malformed("comb field requires a positive MaxLen"))?;
        TextLayout::Comb(max_len)
    } else if flags & MULTILINE != 0 {
        TextLayout::Multiline
    } else {
        TextLayout::SingleLine
    };
    let form = interactive_form(reader)?;
    let form = match form.as_deref() {
        Some(Value::Dict(form)) => Some(form),
        _ => None,
    };
    // A `/Q` on the chain wins even when it is unusable, and the form's
    // `/Q` is the default (ISO 32000-2 Table 225).
    let quadding = match quadding {
        Some(quadding) => Some(Arc::clone(quadding)),
        None => match form {
            Some(form) => reader.entry(form, "Q")?,
            None => None,
        },
    };
    let align = Align::from_quadding(integer(quadding.as_deref()).unwrap_or(0));
    let value = value_text(reader, value.as_deref())?;
    let style = WidgetStyle::read(reader, widget)?;
    if let TextLayout::Comb(max_len) = layout {
        let count = standard::grapheme_count(&standard::single_line(&value, reader.budget)?);
        if count > max_len {
            return Err(WidgetError::Malformed(
                format!("comb field value has {count} characters, exceeding MaxLen {max_len}")
                    .into(),
            ));
        }
    }
    if !standard::is_winansi(&value) {
        return Ok(Some(Drawing::Removed));
    }
    drawn(reader, widget, form, &style, &value, layout, align).map(Some)
}

/// Draws `widget`'s border and background with `value` laid out inside
/// them in the font of its `/DA`.
fn drawn(
    reader: &Reader<'_>,
    widget: &Dict,
    form: Option<&Dict>,
    style: &WidgetStyle,
    value: &str,
    layout: TextLayout,
    align: Align,
) -> Result<Drawing, WidgetError> {
    let text = TextStyle::new(default_appearance(reader, widget, form)?);
    let mut ops = BudgetVec::new(reader.budget);
    match layout {
        TextLayout::Comb(max_len) => {
            comb_ops(&mut ops, style, &text, value, max_len, align)?;
        }
        TextLayout::SingleLine | TextLayout::Multiline => {
            let multiline = matches!(layout, TextLayout::Multiline);
            text_ops(&mut ops, style, &text, value, multiline, align)?;
        }
    }
    let mut content = BudgetVec::new(reader.budget);
    style.decoration(&mut content)?;
    let font = if ops.is_empty() {
        None
    } else {
        let rect = style.content_rect();
        let clip = format!(
            "q\n{} {} {} {} re W n\n",
            fmt(rect[0]),
            fmt(rect[1]),
            fmt((rect[2] - rect[0]).max(0.0)),
            fmt((rect[3] - rect[1]).max(0.0)),
        );
        content.try_extend_from_slice(clip.as_bytes())?;
        content.try_extend_from_slice(&ops)?;
        content.try_extend_from_slice(b"Q\n")?;
        Some((text.resource, text.base_font))
    };
    Ok(Drawing::Drawn(WidgetAppearance {
        content: content.into_vec(),
        size: (style.width, style.height),
        matrix: style.matrix,
        font,
    }))
}

/// Draws the states that a check box or radio button widget lacks: `Off`,
/// and the state that its `/AS` names, as a check mark or a dot.
fn button_states(
    reader: &Reader<'_>,
    widget: &Dict,
    radio: bool,
) -> Result<Option<Drawing>, WidgetError> {
    let style = WidgetStyle::read(reader, widget)?;
    let current = match reader.entry(widget, "AS")?.as_deref() {
        Some(Value::Name(state)) => state.clone(),
        _ => Name::from(OFF),
    };
    let normal = normal_states(reader, widget)?;
    let normal = match normal.as_deref() {
        Some(Value::Dict(normal)) => Some(normal),
        _ => None,
    };
    let mut states = Vec::new();
    for state in [Name::from(OFF), current] {
        if states.iter().any(|(drawn, _)| *drawn == state) {
            continue;
        }
        if let Some(normal) = normal
            && let Some(stream) = normal.get(state.as_bytes())
            && has_content(&*reader.resolve(stream)?)
        {
            continue;
        }
        let on = state.as_bytes() != OFF.as_bytes();
        let content = style.button(on, radio, reader.budget)?;
        states.push((
            state,
            WidgetAppearance {
                content,
                size: (style.width, style.height),
                matrix: style.matrix,
                font: None,
            },
        ));
    }
    Ok((!states.is_empty()).then_some(Drawing::States(states)))
}

/// Reads the entries of [`KEYS`] for a widget's field: each from the
/// nearest field of the `/Parent` chain that holds it, from the widget
/// when it is a field itself, with a `/T` or without a parent, and else
/// from its parent.
fn entries(reader: &Reader<'_>, widget: &Dict) -> Result<Entries> {
    let mut found = Entries::default();
    let mut visited = Vec::new();
    let mut held = None;
    if widget.get("T").is_none() {
        held = parent(reader, widget, &mut visited)?;
    }
    loop {
        let dict = match held.as_deref() {
            None => widget,
            Some(Value::Dict(dict)) => dict,
            Some(_) => break,
        };
        for (slot, key) in found.iter_mut().zip(KEYS) {
            if slot.is_none() {
                *slot = reader.entry(dict, key)?;
            }
        }
        if found.iter().all(Option::is_some) {
            break;
        }
        match parent(reader, dict, &mut visited)? {
            Some(parent) => held = Some(parent),
            None => break,
        }
    }
    Ok(found)
}

/// Returns the dictionary of the field that `dict`'s `/Parent` refers to,
/// or `None` past [`MAX_PARENTS`] fields or for a field visited before.
fn parent(reader: &Reader<'_>, dict: &Dict, visited: &mut Vec<u32>) -> Result<Option<Arc<Value>>> {
    let Some(parent @ Value::Reference(reference)) = dict.get("Parent") else {
        return Ok(None);
    };
    if visited.len() >= MAX_PARENTS || visited.contains(&reference.number()) {
        return Ok(None);
    }
    visited.push(reference.number());
    let parent = reader.resolve(parent)?;
    Ok(matches!(*parent, Value::Dict(_)).then_some(parent))
}

/// Returns the document's interactive form dictionary.
fn interactive_form(reader: &Reader<'_>) -> Result<Option<Arc<Value>>> {
    let Some(root) = reader.version.trailer().get("Root") else {
        return Ok(None);
    };
    let catalog = reader.resolve(root)?;
    let Value::Dict(catalog) = &*catalog else {
        return Ok(None);
    };
    reader.entry(catalog, "AcroForm")
}

/// Returns the `/DA` of a widget: its own, else the nearest of its
/// `/Parent` chain, else the interactive form's.
fn default_appearance(
    reader: &Reader<'_>,
    widget: &Dict,
    form: Option<&Dict>,
) -> Result<Option<DefaultAppearance>> {
    let mut visited = Vec::new();
    let mut held = None;
    loop {
        let dict = match held.as_deref() {
            None => widget,
            Some(Value::Dict(dict)) => dict,
            Some(_) => break,
        };
        if let Some(Value::String(da)) = reader.entry(dict, "DA")?.as_deref() {
            return Ok(Some(DefaultAppearance::parse(da.as_bytes())));
        }
        match parent(reader, dict, &mut visited)? {
            Some(parent) => held = Some(parent),
            None => break,
        }
    }
    let Some(form) = form else {
        return Ok(None);
    };
    Ok(match reader.entry(form, "DA")?.as_deref() {
        Some(Value::String(da)) => Some(DefaultAppearance::parse(da.as_bytes())),
        _ => None,
    })
}

/// Returns a field value as text: a text string, a name, or the text
/// strings of an array joined by commas, as `Field.value` reads it; else
/// empty. Charges the text to the budget's memory.
fn value_text(reader: &Reader<'_>, value: Option<&Value>) -> Result<String> {
    let text = match value {
        Some(Value::String(encoded)) => text_string(encoded.as_bytes()),
        Some(Value::Name(name)) => String::from_utf8_lossy(name.as_bytes()).into_owned(),
        Some(Value::Array(items)) => {
            let mut text = String::new();
            for item in items.iter() {
                if let Value::String(encoded) = &*reader.resolve(item)? {
                    let item = text_string(encoded.as_bytes());
                    reader
                        .budget
                        .charge(LimitCode::MemorySize, item.len() as u64 + 2)?;
                    if !text.is_empty() {
                        text.push_str(", ");
                    }
                    text.push_str(&item);
                }
            }
            return Ok(text);
        }
        _ => String::new(),
    };
    reader
        .budget
        .charge(LimitCode::MemorySize, text.len() as u64)?;
    Ok(text)
}

/// Returns an integer value.
fn integer(value: Option<&Value>) -> Option<i64> {
    match value {
        Some(&Value::Integer(value)) => Some(value),
        _ => None,
    }
}

/// A device colour of `/MK`, with channels from 0 to 1.
#[derive(Clone, Copy)]
enum Color {
    Gray(f64),
    Rgb(f64, f64, f64),
    Cmyk(f64, f64, f64, f64),
}

impl Color {
    /// Reads a colour array of one, three, or four numbers.
    fn read(reader: &Reader<'_>, value: Option<&Value>) -> Result<Option<Self>> {
        let Some(value) = value else {
            return Ok(None);
        };
        let Some(values) = reader.numbers_of(value, 4)? else {
            return Ok(None);
        };
        let values: Vec<f64> = values.into_iter().map(|v| v.clamp(0.0, 1.0)).collect();
        Ok(match values[..] {
            [gray] => Some(Self::Gray(gray)),
            [red, green, blue] => Some(Self::Rgb(red, green, blue)),
            [cyan, magenta, yellow, black] => Some(Self::Cmyk(cyan, magenta, yellow, black)),
            _ => None,
        })
    }

    /// Returns the operator that selects the colour for filling or
    /// stroking.
    fn operator(self, stroke: bool) -> String {
        let (values, operator) = match self {
            Self::Gray(gray) => (vec![gray], "g"),
            Self::Rgb(red, green, blue) => (vec![red, green, blue], "rg"),
            Self::Cmyk(cyan, magenta, yellow, black) => (vec![cyan, magenta, yellow, black], "k"),
        };
        let mut out: Vec<String> = values.into_iter().map(fmt).collect();
        out.push(if stroke {
            operator.to_uppercase()
        } else {
            operator.to_owned()
        });
        out.join(" ") + "\n"
    }
}

/// The size and rotation of a widget, as its text is laid out, and its
/// background and border.
struct WidgetStyle {
    width: f64,
    height: f64,
    matrix: Option<[f64; 6]>,
    background: Option<Color>,
    border: Option<Color>,
    border_width: f64,
}

impl WidgetStyle {
    /// Reads a widget's `/Rect`, its `/MK` rotation and colours, and its
    /// border width from `/BS` or the legacy `/Border`.
    fn read(reader: &Reader<'_>, widget: &Dict) -> Result<Self, WidgetError> {
        let Some(rect) = widget.get("Rect") else {
            return Err(malformed("widget has no Rect"));
        };
        let resolved = reader.resolve(rect)?;
        let Value::Array(items) = &*resolved else {
            return Err(malformed("widget Rect is invalid"));
        };
        let values = if items.len() == 4 {
            reader.numbers_of(&resolved, 4)?
        } else {
            None
        };
        let Some(&[x0, y0, x1, y1]) = values.as_deref() else {
            return Err(malformed("widget Rect must contain four numbers"));
        };
        let (width, height) = ((x1 - x0).abs(), (y1 - y0).abs());
        if !(width > 0.0 && height > 0.0 && width.is_finite() && height.is_finite()) {
            return Err(malformed(
                "widget Rect must have positive finite dimensions",
            ));
        }
        let mark = reader.entry(widget, "MK")?;
        let mark = match mark.as_deref() {
            Some(Value::Dict(mark)) => Some(mark),
            _ => None,
        };
        let entry = |key| match mark {
            Some(mark) => reader.entry(mark, key),
            None => Ok(None),
        };
        let rotation = integer(entry("R")?.as_deref()).unwrap_or(0).rem_euclid(360);
        let background = Color::read(reader, entry("BG")?.as_deref())?;
        let border = Color::read(reader, entry("BC")?.as_deref())?.or(Some(Color::Gray(0.0)));
        let mut border_width = None;
        if let Some(Value::Dict(style)) = reader.entry(widget, "BS")?.as_deref()
            && let Some(width) = style.get("W")
        {
            border_width = reader.number(width)?;
        }
        if border_width.is_none()
            && let Some(Value::Array(border)) = reader.entry(widget, "Border")?.as_deref()
            && let Some(width) = border.get(2)
        {
            border_width = reader.number(width)?;
        }
        let border_width = border_width
            .unwrap_or(1.0)
            .clamp(0.0, width.min(height) / 2.0);
        let (layout_width, layout_height, matrix) = match rotation {
            90 => (height, width, Some([0.0, 1.0, -1.0, 0.0, width, 0.0])),
            180 => (width, height, Some([-1.0, 0.0, 0.0, -1.0, width, height])),
            270 => (height, width, Some([0.0, -1.0, 1.0, 0.0, 0.0, height])),
            _ => (width, height, None),
        };
        Ok(Self {
            width: layout_width,
            height: layout_height,
            matrix,
            background,
            border,
            border_width,
        })
    }

    /// Returns the box that the text is drawn in, inside the border, with
    /// `y` downward from the top.
    fn content_rect(&self) -> [f64; 4] {
        let inset = (self.border_width + 1.0)
            .max(2.0)
            .min(self.width.min(self.height) / 3.0);
        [
            inset,
            inset,
            (self.width - inset).max(inset),
            (self.height - inset).max(inset),
        ]
    }

    /// Draws a check box, with a check mark when `on`, or a radio button,
    /// with a dot when `on`.
    fn button(&self, on: bool, radio: bool, budget: &Budget) -> Result<Vec<u8>, LimitExceeded> {
        let mut out = BudgetVec::new(budget);
        if radio {
            self.round_decoration(&mut out)?;
        } else {
            self.decoration(&mut out)?;
        }
        let (width, height) = (self.width, self.height);
        if on && radio {
            let mut ops = circle(width / 2.0, height / 2.0, width.min(height) * 0.22);
            ops.push_str("0 g\nf\n");
            out.try_extend_from_slice(ops.as_bytes())?;
        } else if on {
            let ops = format!(
                "q\n0 G\n{} w\n{} {} m\n{} {} l\n{} {} l\nS\nQ\n",
                fmt((width.min(height) * 0.10).max(1.0)),
                fmt(width * 0.20),
                fmt(height * 0.52),
                fmt(width * 0.42),
                fmt(height * 0.27),
                fmt(width * 0.82),
                fmt(height * 0.76),
            );
            out.try_extend_from_slice(ops.as_bytes())?;
        }
        Ok(out.into_vec())
    }

    /// Draws the background and the border of a radio button as circles.
    fn round_decoration(&self, out: &mut BudgetVec<'_, u8>) -> Result<(), LimitExceeded> {
        let radius = ((self.width.min(self.height) - self.border_width) / 2.0).max(0.0);
        let (x, y) = (self.width / 2.0, self.height / 2.0);
        let mut ops = String::from("q\n");
        if let Some(color) = self.background {
            ops.push_str(&color.operator(false));
            ops.push_str(&circle(x, y, radius));
            ops.push_str("f\n");
        }
        if self.border_width > 0.0
            && let Some(color) = self.border
        {
            ops.push_str(&color.operator(true));
            ops.push_str(&format!("{} w\n", fmt(self.border_width)));
            ops.push_str(&circle(x, y, radius));
            ops.push_str("S\n");
        }
        ops.push_str("Q\n");
        out.try_extend_from_slice(ops.as_bytes())
    }

    /// Draws the background and the rectangular border.
    fn decoration(&self, out: &mut BudgetVec<'_, u8>) -> Result<(), LimitExceeded> {
        let mut ops = String::from("q\n");
        if let Some(color) = self.background {
            ops.push_str(&color.operator(false));
            ops.push_str(&format!(
                "0 0 {} {} re f\n",
                fmt(self.width),
                fmt(self.height)
            ));
        }
        if self.border_width > 0.0
            && let Some(color) = self.border
        {
            ops.push_str(&color.operator(true));
            let inset = self.border_width / 2.0;
            ops.push_str(&format!(
                "{} w\n{} {} {} {} re S\n",
                fmt(self.border_width),
                fmt(inset),
                fmt(inset),
                fmt((self.width - self.border_width).max(0.0)),
                fmt((self.height - self.border_width).max(0.0)),
            ));
        }
        ops.push_str("Q\n");
        out.try_extend_from_slice(ops.as_bytes())
    }
}

/// The text style of a widget's `/DA`.
struct TextStyle {
    font: Base14Font,
    /// The font's resource name.
    resource: String,
    base_font: &'static str,
    /// The size in points; 0 fits the text to the widget.
    size: f64,
    color: (f64, f64, f64),
}

impl TextStyle {
    /// Returns the style of a `/DA`: Helvetica, as `Helv`, for a font that
    /// is no Standard 14 abbreviation, and Helvetica fitted in black without
    /// one.
    fn new(da: Option<DefaultAppearance>) -> Self {
        let (resource, size, color) = da.map_or_else(
            || ("Helv".to_owned(), 0.0, (0.0, 0.0, 0.0)),
            |da| (da.font, da.size, da.color),
        );
        let base_font = standard::da_font(&resource);
        let resource = if base_font == "Helvetica" && resource != "Helv" {
            "Helv".to_owned()
        } else {
            resource
        };
        Self {
            font: standard::font(base_font).unwrap_or(Base14Font::Helvetica),
            resource,
            base_font,
            size: if size.is_finite() && size > 0.0 {
                size
            } else {
                0.0
            },
            color,
        }
    }

    fn style(&self, size: f64) -> Style<'_> {
        let (red, green, blue) = self.color;
        Style {
            resource: &self.resource,
            size,
            color: DeviceColor::Rgb(red, green, blue),
        }
    }
}

/// Draws a text or choice value, fitted to the widget unless the `/DA`
/// sets a size.
fn text_ops(
    out: &mut BudgetVec<'_, u8>,
    style: &WidgetStyle,
    text: &TextStyle,
    value: &str,
    multiline: bool,
    align: Align,
) -> Result<(), WidgetError> {
    if value.is_empty() {
        return Ok(());
    }
    let budget = out.budget();
    let value = if multiline {
        Cow::Borrowed(value)
    } else {
        standard::single_line(value, budget)?
    };
    let rect = style.content_rect();
    let size = (rect[2] - rect[0], rect[3] - rect[1]);
    if size.0 <= 0.0 || size.1 <= 0.0 {
        return Ok(());
    }
    let (layout, font_size) = standard::fit(&value, size, text.font, text.size, multiline, budget)?;
    standard::text_ops(
        out,
        style.height,
        rect,
        &layout,
        align,
        text.style(font_size),
    )?;
    Ok(())
}

/// Draws a comb value one grapheme to a cell, from the cell that `align`
/// starts it at, at the largest size that fits every cell.
fn comb_ops(
    out: &mut BudgetVec<'_, u8>,
    style: &WidgetStyle,
    text: &TextStyle,
    value: &str,
    max_len: usize,
    align: Align,
) -> Result<(), WidgetError> {
    let budget = out.budget();
    let value = standard::single_line(value, budget)?;
    let graphemes = standard::graphemes(&value, budget)?;
    if graphemes.is_empty() {
        return Ok(());
    }
    let rect = style.content_rect();
    let size = (rect[2] - rect[0], rect[3] - rect[1]);
    if size.0 <= 0.0 || size.1 <= 0.0 {
        return Ok(());
    }
    #[expect(
        clippy::cast_precision_loss,
        reason = "a comb has fewer cells than f64 counts exactly"
    )]
    let cell = size.0 / max_len as f64;
    let mut font_size = size.1.min(MAX_FIT_SIZE);
    let layouts = loop {
        let mut layouts = BudgetVec::new(budget);
        layouts.try_reserve(graphemes.len())?;
        let mut fits = true;
        for grapheme in graphemes.iter() {
            let layout =
                standard::form_layout(grapheme, (cell, size.1), text.font, font_size, 1.0, budget)?;
            fits &= layout.fits() && layout.lines.len() == 1;
            layouts.try_push(layout)?;
        }
        if fits {
            break layouts;
        }
        font_size *= FIT_STEP;
        if font_size < MIN_FIT_SIZE {
            return Err(malformed("comb field text cannot fit inside the widget"));
        }
    };
    let unused = max_len - graphemes.len();
    let start = match align {
        Align::Left => 0,
        Align::Center => unused / 2,
        Align::Right => unused,
    };
    for (index, layout) in layouts.iter().enumerate() {
        #[expect(
            clippy::cast_precision_loss,
            reason = "a comb has fewer cells than f64 counts exactly"
        )]
        let x0 = rect[0] + (start + index) as f64 * cell;
        standard::text_ops(
            out,
            style.height,
            [x0, rect[1], x0 + cell, rect[3]],
            layout,
            Align::Center,
            text.style(font_size),
        )?;
    }
    Ok(())
}

/// Formats a number for content with at most six decimals, as pylopdf's
/// widget appearances do.
/// Returns a closed path of four Bézier curves around a circle.
fn circle(x: f64, y: f64, radius: f64) -> String {
    const KAPPA: f64 = 0.552_284_749_830_793_6;
    let control = radius * KAPPA;
    let points = [
        [
            x + radius,
            y + control,
            x + control,
            y + radius,
            x,
            y + radius,
        ],
        [
            x - control,
            y + radius,
            x - radius,
            y + control,
            x - radius,
            y,
        ],
        [
            x - radius,
            y - control,
            x - control,
            y - radius,
            x,
            y - radius,
        ],
        [
            x + control,
            y - radius,
            x + radius,
            y - control,
            x + radius,
            y,
        ],
    ];
    let mut ops = format!("{} {} m\n", fmt(x + radius), fmt(y));
    for curve in points {
        let numbers: Vec<String> = curve.into_iter().map(fmt).collect();
        ops.push_str(&numbers.join(" "));
        ops.push_str(" c\n");
    }
    ops.push_str("h\n");
    ops
}

fn fmt(value: f64) -> String {
    let value = if value.abs() < 1.0e-9 { 0.0 } else { value };
    let text = format!("{value:.6}");
    text.trim_end_matches('0').trim_end_matches('.').to_owned()
}

#[cfg(test)]
mod tests;
