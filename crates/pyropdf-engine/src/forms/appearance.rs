//! Synthesizing normal widget appearances for text, choice, button, and
//! signature fields.

use std::sync::Arc;

use pyropdf_store::Result;
use pyropdf_store::budget::{BudgetVec, LimitCode};
use pyropdf_store::value::{Dict, Name, Value};

use crate::annotations::Reader;
use crate::standard::{self, Align};

use super::face::{self, FaceText};
use super::form_font::{
    FormFont, default_appearance, form_standard_font, interactive_form, owned, parent,
};
use super::settle::{
    Drawing, FontEntry, WidgetAppearance, has_normal_stream, has_state, with_normal_states,
};
use super::style::{TextStyle, WidgetStyle, comb_ops, fmt, text_ops};
use super::text;
use super::{WidgetError, malformed};

/// The field flags that a text field's layout depends on (ISO 32000-2
/// Table 229).
const MULTILINE: i64 = 1 << 12;
const PASSWORD: i64 = 1 << 13;
const FILE_SELECT: i64 = 1 << 20;
const COMB: i64 = 1 << 24;
/// The flags of a button field that make it a radio button or a
/// pushbutton (ISO 32000-2 Table 226).
pub(super) const RADIO: i64 = 1 << 15;
const PUSHBUTTON: i64 = 1 << 16;
/// The appearance state of a button that is not selected.
pub const OFF: &str = "Off";

/// The field entries that a widget's appearance reads, in [`Entries`]
/// order.
const KEYS: [&str; 5] = ["FT", "Ff", "V", "Q", "MaxLen"];

/// The entries of [`KEYS`] that a widget's field holds or inherits.
type Entries = [Option<Arc<Value>>; 5];

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
    draw(reader, widget, false, None)
}

/// Draws the normal appearance of a text or choice `widget` from the value
/// of its field in `face`, as [`appearance`] draws it in a Standard 14
/// font; returns `None` for a widget of another field. Refuses a value
/// with a character that the face has no glyph for.
pub fn face_appearance(
    reader: &Reader<'_>,
    widget: &Dict,
    face: &mut FaceText<'_>,
) -> Result<Option<Drawing>, WidgetError> {
    draw(reader, widget, false, Some(face))
}

/// Draws every normal appearance of `widget` again after its style
/// changed, as [`appearance`] draws what it lacks: a check box or radio
/// button widget gets each state that its normal appearance names, `Off`,
/// and the state that its `/AS` names, and a pushbutton widget its caption
/// in place of the appearance it has. A signature widget keeps an
/// appearance that it has.
pub fn restyled(reader: &Reader<'_>, widget: &Dict) -> Result<Option<Drawing>, WidgetError> {
    draw(reader, widget, true, None)
}

/// Implements [`appearance`], with `every` [`restyled`], and with `face`
/// [`face_appearance`].
fn draw(
    reader: &Reader<'_>,
    widget: &Dict,
    every: bool,
    face: Option<&mut FaceText<'_>>,
) -> Result<Option<Drawing>, WidgetError> {
    let entries = entries(reader, widget)?;
    let [field_type, flags, value, quadding, max_len] = &entries;
    let flags = integer(flags.as_deref()).unwrap_or(0);
    let text_field = match field_type.as_deref() {
        Some(Value::Name(name)) if name.as_bytes() == b"Tx" => true,
        Some(Value::Name(name)) if name.as_bytes() == b"Ch" => false,
        _ if face.is_some() => return Ok(None),
        Some(Value::Name(name)) if name.as_bytes() == b"Btn" && flags & PUSHBUTTON == 0 => {
            return button_states(reader, widget, flags & RADIO != 0, every);
        }
        Some(Value::Name(name)) if matches!(name.as_bytes(), b"Btn" | b"Sig") => {
            let keep = !every || name.as_bytes() == b"Sig";
            if keep && has_normal_stream(reader, widget)? {
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
                None,
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
    let form_quadding = match form {
        Some(form) if quadding.is_none() => reader.entry(form, "Q")?,
        _ => None,
    };
    let quadding = quadding.as_deref().or(form_quadding.as_deref());
    let align = Align::from_quadding(integer(quadding).unwrap_or(0));
    let value = value_text(reader, value.as_deref())?;
    let style = WidgetStyle::read(reader, widget)?;
    if let TextLayout::Comb(max_len) = layout {
        let count = standard::grapheme_count(
            &standard::single_line(&value, reader.budget)?,
            reader.budget,
        )?;
        if count > max_len {
            return Err(WidgetError::Malformed(
                format!("comb field value has {count} characters, exceeding MaxLen {max_len}")
                    .into(),
            ));
        }
    }
    if let Some(face) = &face {
        face.check(&value)?;
    } else {
        for (index, character) in value.chars().enumerate() {
            if index % 1024 == 0 {
                reader.budget.poll()?;
            }
            if standard::winansi_byte(character).is_none() {
                return Ok(Some(Drawing::Removed));
            }
        }
    }
    drawn(reader, widget, form, &style, &value, layout, align, face).map(Some)
}

/// Draws `widget`'s border and background with `value` laid out inside
/// them in the font of its `/DA`, or in `face` at the size and colour of
/// its `/DA`.
#[expect(
    clippy::too_many_arguments,
    reason = "the parts of an appearance that `draw` reads"
)]
fn drawn(
    reader: &Reader<'_>,
    widget: &Dict,
    form: Option<&Dict>,
    style: &WidgetStyle,
    value: &str,
    layout: TextLayout,
    align: Align,
    face: Option<&mut FaceText<'_>>,
) -> Result<Drawing, WidgetError> {
    let appearance = default_appearance(reader, widget, form)?;
    let form_font = match (&appearance, form) {
        (Some(appearance), Some(form)) => form_standard_font(reader, form, &appearance.font)?,
        _ => FormFont::Absent,
    };
    let base_font = match form_font {
        FormFont::Standard(base_font) => Some(base_font),
        // Drawing in another font's encoding would show other glyphs.
        FormFont::Unsupported if face.is_none() => {
            return Err(malformed(
                "the form font that the widget's /DA names has an /Encoding \
                 that a Standard 14 appearance cannot reproduce",
            ));
        }
        FormFont::Absent | FormFont::Unsupported => None,
    };
    let mut text = TextStyle::new(appearance, base_font);
    let embedded = face.as_ref().map(|face| face.font());
    if face.is_some() {
        face::RESOURCE.clone_into(&mut text.resource);
    }
    let mut ops = BudgetVec::new(reader.budget);
    match layout {
        TextLayout::Comb(max_len) => {
            comb_ops(&mut ops, style, &text, value, max_len, align, face)?;
        }
        TextLayout::SingleLine | TextLayout::Multiline => {
            let multiline = matches!(layout, TextLayout::Multiline);
            text_ops(&mut ops, style, &text, value, multiline, align, face)?;
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
        let entry = match embedded {
            None => FontEntry::Standard(text.base_font),
            Some(Some(reference)) => FontEntry::Embedded(reference),
            // Collecting glyphs writes nothing.
            Some(None) => FontEntry::Standard(text.base_font),
        };
        Some((text.resource, entry))
    };
    Ok(Drawing::Drawn(WidgetAppearance {
        content: content.into_vec(),
        size: (style.width, style.height),
        matrix: style.matrix,
        font,
    }))
}

/// Draws the states that a check box or radio button widget lacks: `Off`,
/// and the state that its `/AS` names, as a check mark or a dot. With
/// `every`, it draws these and each state of its normal appearance anew.
fn button_states(
    reader: &Reader<'_>,
    widget: &Dict,
    radio: bool,
    every: bool,
) -> Result<Option<Drawing>, WidgetError> {
    let style = WidgetStyle::read(reader, widget)?;
    let current = match reader.entry(widget, "AS")?.as_deref() {
        Some(Value::Name(state)) => state.clone(),
        _ => Name::from(OFF),
    };
    let mut named = BudgetVec::new(reader.budget);
    if every {
        with_normal_states(reader, widget, |normal| -> Result<()> {
            for state in normal.keys() {
                let size = state.as_bytes().len() as u64;
                reader.budget.charge(LimitCode::MemorySize, size)?;
                named.try_push(state.clone())?;
            }
            Ok(())
        })?
        .transpose()?;
    }
    let mut states = BudgetVec::new(reader.budget);
    for state in named
        .into_vec()
        .into_iter()
        .chain([Name::from(OFF), current])
    {
        if states.iter().any(|(drawn, _)| *drawn == state) {
            continue;
        }
        if !every && has_state(reader, widget, state.as_bytes())? {
            continue;
        }
        let on = state.as_bytes() != OFF.as_bytes();
        let content = style.button(on, radio, reader.budget)?;
        states.try_push((
            state,
            WidgetAppearance {
                content,
                size: (style.width, style.height),
                matrix: style.matrix,
                font: None,
            },
        ))?;
    }
    Ok((!states.is_empty()).then(|| Drawing::States(states.into_vec())))
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
                *slot = reader
                    .entry(dict, key)?
                    .map(|value| owned(value, reader.budget))
                    .transpose()?;
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

/// Returns a field value as text: a text string, a name, or the text
/// strings of an array joined by commas, as `Field.value` reads it; else
/// empty. Charges the text to the budget's memory.
fn value_text(reader: &Reader<'_>, value: Option<&Value>) -> Result<String> {
    let mut text = BudgetVec::new(reader.budget);
    match value {
        Some(Value::String(encoded)) => text::append_text(&mut text, encoded.as_bytes())?,
        Some(Value::Name(name)) => text::append_lossy(&mut text, name.as_bytes())?,
        Some(Value::Array(items)) => {
            for item in items.iter() {
                reader.budget.poll()?;
                if let Value::String(encoded) = &*reader.resolve(item)? {
                    if !text.is_empty() {
                        text.try_extend_from_slice(b", ")?;
                    }
                    text::append_text(&mut text, encoded.as_bytes())?;
                }
            }
        }
        _ => {}
    }
    Ok(String::from_utf8(text.into_vec()).expect("the decoder writes valid UTF-8"))
}

/// Returns an integer value.
pub(super) fn integer(value: Option<&Value>) -> Option<i64> {
    match value {
        Some(&Value::Integer(value)) => Some(value),
        _ => None,
    }
}
