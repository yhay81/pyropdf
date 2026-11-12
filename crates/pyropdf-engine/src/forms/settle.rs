//! Appearance settling, stream-state queries, and Form XObject writing for
//! interactive form widgets.

use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::Transaction;
use pyropdf_store::{Error, Result};

use crate::annotations::{PageUnits, Reader, numbers_array};

use super::WidgetError;
use super::appearance::appearance;

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
/// the form's size and the matrix that rotates it, and the font that its
/// text names, by resource.
pub struct WidgetAppearance {
    pub(super) content: Vec<u8>,
    pub(super) size: (f64, f64),
    pub(super) matrix: Option<[f64; 6]>,
    pub(super) font: Option<(String, FontEntry)>,
}

/// The font of an appearance's text.
pub(super) enum FontEntry {
    /// A Standard 14 font by PostScript name, written with the appearance.
    Standard(&'static str),
    /// An embedded face's Type0 font that the document has.
    Embedded(Reference),
}

impl WidgetAppearance {
    /// Writes the font and the form XObject of the appearance as new
    /// objects, and returns the form's reference.
    #[expect(
        clippy::disallowed_methods,
        reason = "each key is a constant or the one font resource, so each dictionary holds six entries at most"
    )]
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
        if let Some((resource, entry)) = &self.font {
            let reference = match entry {
                FontEntry::Standard(base_font) => standard_font(transaction, base_font, budget)?,
                FontEntry::Embedded(reference) => *reference,
            };
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

/// Writes the font dictionary of Standard 14 font `base_font` as a new
/// object and returns its reference.
#[expect(
    clippy::disallowed_methods,
    reason = "each key is a constant, so the font holds four entries at most"
)]
fn standard_font(
    transaction: &mut Transaction,
    base_font: &str,
    budget: &Budget,
) -> Result<Reference> {
    let mut font = Dict::new();
    font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
    font.insert(Name::from("Subtype"), Value::Name(Name::from("Type1")));
    font.insert(Name::from("BaseFont"), Value::Name(Name::from(base_font)));
    if !matches!(base_font, "Symbol" | "ZapfDingbats") {
        font.insert(
            Name::from("Encoding"),
            Value::Name(Name::from("WinAnsiEncoding")),
        );
    }
    let reference = transaction.allocate()?;
    transaction.write(reference.number(), &Value::Dict(font), budget)?;
    Ok(reference)
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
    let generation = transaction
        .version()
        .get(number, budget)?
        .map_or(0, |object| object.generation());
    let unit = PageUnits::new(transaction.version(), budget)
        .unit_of(Some(Reference::new(number, generation)), widget)?;
    settle_in_unit(transaction, number, widget, unit, budget)
}

/// Draws the appearance of widget `number`, `widget`, on a page whose
/// `/UserUnit` is `unit`.
pub fn settle_in_unit(
    transaction: &mut Transaction,
    number: u32,
    widget: &Dict,
    unit: f64,
    budget: &Budget,
) -> Result<()> {
    let reader = Reader::on_page(transaction.version(), budget, unit);
    match appearance(&reader, widget) {
        Ok(Some(drawing)) => write(transaction, number, widget, drawing, budget),
        Ok(None) | Err(WidgetError::Malformed(_) | WidgetError::Lines) => Ok(()),
        Err(WidgetError::Store(error)) => Err(error),
    }
}

/// Writes `drawing` into the appearances of widget `number`, `widget`.
pub fn write(
    transaction: &mut Transaction,
    number: u32,
    widget: &Dict,
    drawing: Drawing,
    budget: &Budget,
) -> Result<()> {
    let reader = Reader::new(transaction.version(), budget);
    let mut appearances = match reader.entry(widget, "AP")?.as_deref() {
        Some(value @ Value::Dict(_)) => match copy_for_edit(value, budget)? {
            Value::Dict(appearances) => appearances,
            _ => unreachable!("a copied dictionary remains a dictionary"),
        },
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
            #[expect(
                clippy::disallowed_methods,
                reason = "the appearances grow by one entry at most"
            )]
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
                #[expect(
                    clippy::disallowed_methods,
                    reason = "one entry per state drawn, and `button_states` grew the states through the budget"
                )]
                normal.insert(state, Value::Reference(reference));
            }
            #[expect(
                clippy::disallowed_methods,
                reason = "the appearances grow by one entry at most"
            )]
            appearances.insert(Name::from("N"), Value::Dict(normal));
        }
    }
    let Some(original) = transaction.version().get(number, budget)? else {
        return Err(Error::malformed(
            "a widget disappeared while settling its appearance",
        ));
    };
    let Value::Dict(mut widget) = copy_for_edit(original.value(), budget)? else {
        return Err(Error::malformed("a widget is no longer a dictionary"));
    };
    if appearances.is_empty() {
        widget.remove("AP");
    } else {
        #[expect(
            clippy::disallowed_methods,
            reason = "the widget grows by one entry at most"
        )]
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
    let found = with_normal_states(reader, widget, |states| {
        states
            .get(state)
            .map(|normal| reader.resolve(normal).map(|normal| has_content(&normal)))
            .transpose()
    })?;
    Ok(matches!(found, Some(Ok(Some(true)))))
}

/// Calls `read` with the normal appearance dictionary of a check box or
/// radio button widget, whose keys are its states, and returns its result,
/// or `None` when the widget has no such dictionary.
pub fn with_normal_states<R>(
    reader: &Reader<'_>,
    widget: &Dict,
    read: impl FnOnce(&Dict) -> R,
) -> Result<Option<R>> {
    let appearances = reader.entry(widget, "AP")?;
    let Some(Value::Dict(appearances)) = appearances.as_deref() else {
        return Ok(None);
    };
    let normal = reader.entry(appearances, "N")?;
    let Some(Value::Dict(normal)) = normal.as_deref() else {
        return Ok(None);
    };
    Ok(Some(read(normal)))
}

fn has_content(value: &Value) -> bool {
    matches!(value, Value::Stream(stream) if !stream.data().is_empty())
}
