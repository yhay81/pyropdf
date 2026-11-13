//! PyO3 bindings for interactive form fields and widgets.

use std::collections::HashMap;
use std::sync::Arc;

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::PyBool;
use pyropdf_engine::annotations::PageAnnots;
use pyropdf_engine::forms::field_edit::{self, FieldEdit, FieldSpec};
use pyropdf_engine::forms::fill::{self, FillValue};
use pyropdf_engine::forms::flatten::Selection;
use pyropdf_engine::forms::widget_edit::{self as widget_edits, WidgetEdit, WidgetStyle};
use pyropdf_engine::forms::{self as widgets, FillCache, Form, Id, MAX_VALUE_ITEMS, remove};
use pyropdf_store::budget::{Budget, LimitCode, SharedBudget};
use pyropdf_store::version::Version;

use crate::annotations::{
    annotation_error, editing,
    input::{self, Fields},
};
use crate::document::_Document;
use crate::errors::{PdfError, limit_error, store_error};
use crate::fonts::_Font;
use crate::pages::index;

fn fill_cache(document: &_Document) -> (&FillCache, bool) {
    if document.in_transaction() {
        (&document.draft_fill_index, true)
    } else {
        (&document.fill_index, false)
    }
}

/// Returns the version that reads see.
fn view(document: &_Document) -> PyResult<(Arc<Version>, SharedBudget)> {
    Ok((document.reading_parent()?.0, document.budget()))
}

/// Returns the form of the version that reads see, memoized.
fn memoized_at(
    document: &_Document,
    version: &Arc<Version>,
    budget: &Budget,
) -> PyResult<Arc<Form>> {
    budget.poll().map_err(|e| limit_error(&e))?;
    let parent = if document.in_transaction() {
        Some(document.document()?.current())
    } else {
        None
    };
    let (memo, version) = match parent.as_ref() {
        Some(parent)
            if Arc::ptr_eq(parent, version)
                || parent
                    .changes(version, budget)
                    .map_err(|e| store_error(&e))?
                    .is_some_and(|changes| changes.is_empty()) =>
        {
            (&document.form, parent)
        }
        Some(_) => (&document.draft_form, version),
        None => (&document.form, version),
    };
    memo.get_or_compute(&(), version, budget, widgets::walk)
        .map_err(annotation_error)
}

fn memoized(document: &_Document) -> PyResult<Arc<Form>> {
    let (version, budget) = view(document)?;
    memoized_at(document, &version, &budget)
}

/// Returns the full name and object of every terminal field, by name and
/// then object.
#[pyfunction]
pub fn form_fields(py: Python<'_>, document: &_Document) -> PyResult<Vec<(String, Id)>> {
    crate::errors::boundary(|| {
        py.detach(|| {
            let (version, budget) = view(document)?;
            let form = memoized_at(document, &version, &budget)?;
            widgets::form_fields(&form, &budget).map_err(annotation_error)
        })
    })?
}

/// Returns the object of the first terminal field named `name`, or `None`.
#[pyfunction]
pub fn form_field(py: Python<'_>, document: &_Document, name: &str) -> PyResult<Option<Id>> {
    crate::errors::boundary(|| {
        let form = py.detach(|| memoized(document))?;
        Ok(widgets::form_field(&form, name))
    })?
}

/// Reads field `id` as a handle does: its full name, kind, inherited `/Ff`,
/// and value. Raises `StaleObjectError` once it is no terminal field.
#[pyfunction]
pub fn form_field_read(
    py: Python<'_>,
    document: &_Document,
    id: Id,
) -> PyResult<(String, &'static str, i64, Option<String>)> {
    crate::errors::boundary(|| {
        py.detach(|| {
            let (version, budget) = view(document)?;
            let form = memoized_at(document, &version, &budget)?;
            widgets::form_field_read(&form, id, &budget).map_err(annotation_error)
        })
    })?
}

/// Returns the options of choice field `id` in `/Opt` order: each export
/// value and label. An option is a text string, both its export value and
/// its label, or an array of the two; other entries are skipped.
#[pyfunction]
pub fn form_field_choices(
    py: Python<'_>,
    document: &_Document,
    id: Id,
) -> PyResult<Vec<(String, String)>> {
    crate::errors::boundary(|| {
        py.detach(|| {
            let (version, budget) = view(document)?;
            let form = memoized_at(document, &version, &budget)?;
            widgets::form_field_choices(&form, &version, id, &budget).map_err(annotation_error)
        })
    })?
}

/// Returns where the widgets of field `id` appear, in page order: the page
/// object, the widget's object, and its position in the page's `/Annots`.
#[pyfunction]
pub fn form_field_widgets(
    py: Python<'_>,
    document: &_Document,
    id: Id,
) -> PyResult<Vec<(Id, Id, u32)>> {
    crate::errors::boundary(|| {
        py.detach(|| {
            let (version, index, budget) = index(document)?;
            let form = memoized_at(document, &version, &budget)?;
            widgets::form_field_widgets(&form, &version, &index, id, &budget)
                .map_err(annotation_error)
        })
    })?
}

/// Lists the widgets of page object `(number, generation)` in `/Annots`
/// order, or returns `None` for a page that is gone.
#[pyfunction]
pub fn widget_list(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<Vec<widgets::WidgetRecord>>> {
    crate::errors::boundary(|| {
        py.detach(|| {
            let (version, page_index, budget) = crate::pages::index(document)?;
            let Some(annots) = PageAnnots::read(
                Arc::clone(&version),
                page_index,
                &budget,
                number,
                generation,
            )
            .map_err(annotation_error)?
            else {
                return Ok(None);
            };
            let form = memoized_at(document, &version, &budget)?;
            widgets::widget_list(&annots, &form)
                .map(Some)
                .map_err(annotation_error)
        })
    })?
}

/// Reads the widget that a handle names. Returns `None` for a page that is
/// gone, and raises `StaleObjectError` for a widget that is.
#[pyfunction]
pub fn widget_read(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: Id,
) -> PyResult<Option<widgets::WidgetRecord>> {
    crate::errors::boundary(|| {
        py.detach(|| {
            let (version, page_index, budget) = crate::pages::index(document)?;
            let Some(annots) = PageAnnots::read(
                Arc::clone(&version),
                page_index,
                &budget,
                number,
                generation,
            )
            .map_err(annotation_error)?
            else {
                return Ok(None);
            };
            let (position, dict) =
                widgets::find_widget(&annots, index, id).map_err(annotation_error)?;
            let form = memoized_at(document, &version, &budget)?;
            widgets::widget_read(&annots, &form, position, id, &dict)
                .map(Some)
                .map_err(annotation_error)
        })
    })?
}

/// Gathers the top-level radio buttons that share a name, each a field
/// merged with its widget, under one new parent field of that name, which
/// takes the place of the first in `/Fields`; returns how many groups it
/// made. Viewers and the walk otherwise see only one of them.
#[pyfunction]
pub fn form_repair_radio_groups(py: Python<'_>, document: &_Document) -> PyResult<u32> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            widgets::repair_radio_groups(transaction, budget).map_err(annotation_error)
        })
    })?
}

/// Reads a value, admitting a text's length against `budget` before
/// copying it.
fn read_fill_value(value: &Bound<'_, PyAny>, budget: &Budget) -> PyResult<FillValue> {
    if let Ok(flag) = value.cast::<PyBool>() {
        return Ok(FillValue::Flag(flag.is_true()));
    }
    let text = input::text(
        budget,
        value,
        "a field value",
        LimitCode::FormFieldInputSize,
        &mut 0,
    )?;
    Ok(FillValue::Text(text))
}

/// Sets the value of field `id` to `value`, and draws its widgets through
/// the appearance settler, with those of the other fields that have no
/// appearance yet: a text or choice field shows its text, and a check box
/// or radio button selects the state that `value` names in each widget
/// that has it, or else in its first widget. `True` names a button's first
/// state that is not `Off`, or `Yes`, and `False` names `Off`. Then sets
/// the form's `/NeedAppearances` when a widget is left without an
/// appearance. Refuses read-only or incompatible fields and invalid or
/// oversized values, leaving the document as it was.
///
/// Without `draw`, the field's own widgets are left as they are, for
/// [`form_field_draw_face`] to draw.
#[pyfunction]
#[pyo3(signature = (document, id, value, draw=true))]
pub fn form_field_fill(
    py: Python<'_>,
    document: &_Document,
    id: Id,
    value: &Bound<'_, PyAny>,
    draw: bool,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        let (cache, draft) = fill_cache(document);
        document.edit(py, |transaction, budget| {
            let value = read_fill_value(value, budget)?;
            fill::fill(transaction, id, &value, budget, cache, draft, draw)
                .map_err(annotation_error)
        })
    })?
}

/// Draws the widgets of text or choice field `id` in embedded `face`, as
/// [`form_field_fill`] draws them in a Standard 14 font, and returns the
/// glyphs that they show with their texts.
///
/// Without `font`, it only collects the glyphs and writes nothing. With
/// `font`, the Type0 font of the face that the appearances name and the
/// text that its ToUnicode CMap maps each glyph to, it writes the
/// appearances and then sets the form's `/NeedAppearances` while a widget
/// is left without an appearance. Refuses a character that the face has no
/// glyph for and what the fill refuses, leaving the document as it was.
#[pyfunction]
#[pyo3(signature = (document, id, face, font=None))]
pub fn form_field_draw_face(
    py: Python<'_>,
    document: &_Document,
    id: Id,
    face: &_Font,
    font: Option<(u32, HashMap<u32, String>)>,
) -> PyResult<Vec<(u32, String)>> {
    crate::errors::boundary(|| {
        let Some(face) = face.embedded() else {
            return Err(PdfError::new_err(
                "form appearances draw an OpenType face, not a Standard 14 font",
            ));
        };
        let (cache, draft) = fill_cache(document);
        document.edit(py, |transaction, budget| {
            fill::draw_face(transaction, id, face, font.as_ref(), budget, cache, draft)
                .map_err(annotation_error)
        })
    })?
}

/// Sets the form's `/NeedAppearances`, which asks viewers to draw the
/// widgets again; a document without a form dictionary refuses.
#[pyfunction]
pub fn form_set_need_appearances(
    py: Python<'_>,
    document: &_Document,
    value: bool,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            fill::set_need_appearances(transaction, value, budget).map_err(annotation_error)
        })
    })?
}

/// Reads field `id`'s label (`/TU`), maximum length (inherited `/MaxLen`,
/// unless negative), and default value (inherited `/DV`, a name or a text
/// string), all from one version.
#[pyfunction]
pub fn form_field_details(
    py: Python<'_>,
    document: &_Document,
    id: Id,
) -> PyResult<(Option<String>, Option<i64>, Option<String>)> {
    crate::errors::boundary(|| {
        py.detach(|| {
            let (version, budget) = view(document)?;
            let form = memoized_at(document, &version, &budget)?;
            field_edit::field_details(&version, &budget, &form, id).map_err(annotation_error)
        })
    })?
}

/// The most properties that a field edit removes.
const FIELD_CLEARABLE: usize = 3;

/// Refuses more choices than a choice field has.
fn too_many_choices() -> PyErr {
    PdfError::new_err(format!(
        "a choice field has at most {MAX_VALUE_ITEMS} choices"
    ))
}

/// Reads an edit from the mapping that Python passes, checking each
/// input-derived size against `budget` before copying it.
fn read_field_edit(map: &Bound<'_, PyAny>, budget: &Budget) -> PyResult<FieldEdit> {
    let fields = Fields::new(map, budget);
    let code = LimitCode::FormFieldInputSize;
    let mut total = 0;
    Ok(FieldEdit {
        label: fields.text("label", code, &mut total)?,
        max_length: fields.value("max_length")?,
        flags: fields.value("flags")?,
        choices: fields.pairs(
            "choices",
            MAX_VALUE_ITEMS,
            too_many_choices,
            code,
            &mut total,
        )?,
        default_value: fields.text("default_value", code, &mut total)?,
        reset: fields.value("reset")?.unwrap_or(false),
        clear: fields.names("clear", FIELD_CLEARABLE, cannot_clear)?,
    })
}

/// Refuses a name longer than any field property.
fn cannot_clear() -> PyErr {
    PdfError::new_err("a field property that cannot be removed")
}

/// Edits field `id`: sets or removes its label, maximum length, and
/// default value, sets its flags and choices, or resets its value to the
/// default value, and draws its widgets again as setting its value does.
/// A reset without a default value removes the value of a text or choice
/// field and turns a button off. Refuses flags that change the field's
/// kind, a maximum length outside a text field, and choices outside a
/// choice field, leaving the document as it was.
#[pyfunction]
pub fn form_field_edit(
    py: Python<'_>,
    document: &_Document,
    id: Id,
    edit: &Bound<'_, PyAny>,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        let (cache, draft) = fill_cache(document);
        document.edit(py, |transaction, budget| {
            let edit = read_field_edit(edit, budget)?;
            field_edit::edit_field(transaction, id, &edit, budget, cache, draft)
                .map_err(annotation_error)
        })
    })?
}

/// Refuses a field kind that no field has.
fn unknown_kind() -> PyErr {
    PdfError::new_err("unknown field kind")
}

/// Refuses a font name longer than any Standard 14 font's.
fn unknown_font() -> PyErr {
    PdfError::new_err("unknown Standard 14 font")
}

/// Reads a specification from the mapping that Python passes, checking
/// each input-derived size against `budget` before copying it.
fn read_field_spec(map: &Bound<'_, PyAny>, budget: &Budget) -> PyResult<FieldSpec> {
    let fields = Fields::new(map, budget);
    let code = LimitCode::FormFieldInputSize;
    let mut total = 0;
    Ok(FieldSpec {
        kind: fields
            .short("kind", input::MAX_NAME, unknown_kind)?
            .ok_or_else(|| PyTypeError::new_err("missing kind"))?,
        name: input::text(budget, &fields.required("name")?, "name", code, &mut total)?,
        rect: fields.required("rect")?.extract()?,
        value: fields.text("value", code, &mut total)?,
        default_value: fields.text("default_value", code, &mut total)?,
        flags: fields.value("flags")?.unwrap_or(0),
        label: fields.text("label", code, &mut total)?,
        max_length: fields.value("max_length")?,
        choices: fields.pairs(
            "choices",
            MAX_VALUE_ITEMS,
            too_many_choices,
            code,
            &mut total,
        )?,
        on_state: fields.text("on_state", code, &mut total)?,
        caption: fields.text("caption", code, &mut total)?,
        border: fields.border("border")?,
        border_color: fields.value("border_color")?,
        background_color: fields.value("background_color")?,
        text_color: fields.value("text_color")?,
        font_name: fields.short("font_name", input::MAX_NAME, unknown_font)?,
        font_size: fields.value("font_size")?,
        align: fields.value("align")?,
    })
}

/// Adds a widget that `spec` describes to page object `(number,
/// generation)`: the first widget of a new field, or another widget of the
/// field of that name and kind. Draws the widget through the appearance
/// settler, as setting the field's value does, and returns the widget's
/// object and position in the page's `/Annots`; or `None` for a page that
/// is gone. The page and the form are read after the edit takes the
/// document's writer lock. Refuses a name with dots or one of a field that
/// has fields below it, a kind other than the existing field's, and an
/// invalid or oversized field specification, leaving the document as it was.
#[pyfunction]
pub fn widget_add(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    spec: &Bound<'_, PyAny>,
) -> PyResult<Option<(Id, u32)>> {
    crate::errors::boundary(|| {
        let (cache, draft) = fill_cache(document);
        editing(
            py,
            document,
            number,
            generation,
            |annots, transaction, budget| {
                let spec = read_field_spec(spec, budget)?;
                field_edit::add_widget(transaction, annots, &spec, budget, cache, draft)
                    .map_err(annotation_error)
            },
        )
    })?
}

/// Reads the style of the widget that a handle names. Returns `None` for a
/// page that is gone, and raises `StaleObjectError` for a widget that is.
#[pyfunction]
pub fn widget_style(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: Id,
) -> PyResult<Option<WidgetStyle>> {
    crate::errors::boundary(|| {
        let (version, pages, budget) = crate::pages::index(document)?;
        py.detach(|| {
            widget_edits::widget_style(version, pages, &budget, number, generation, index, id)
        })
        .map_err(annotation_error)
    })?
}

/// The most properties that a widget edit removes.
const WIDGET_CLEARABLE: usize = 8;

/// Reads an edit from the mapping that Python passes, checking each
/// input-derived size against `budget` before copying it.
fn read_widget_edit(map: &Bound<'_, PyAny>, budget: &Budget) -> PyResult<WidgetEdit> {
    let fields = Fields::new(map, budget);
    Ok(WidgetEdit {
        rect: fields.value("rect")?,
        flags: fields.value("flags")?,
        border: fields.border("border")?,
        border_color: fields.value("border_color")?,
        background_color: fields.value("background_color")?,
        caption: fields.text("caption", LimitCode::AnnotationInputSize, &mut 0)?,
        rotation: fields.value("rotation")?,
        text_color: fields.value("text_color")?,
        font_name: fields.short("font_name", input::MAX_NAME, unknown_font)?,
        font_size: fields.value("font_size")?,
        clear: fields.names("clear", WIDGET_CLEARABLE, || {
            PdfError::new_err("a widget property that cannot be removed")
        })?,
    })
}

/// Edits the widget that a handle names and draws it again for its
/// field's value: a text or choice value, every state of a check box or
/// radio button, and a pushbutton's caption; a signature widget keeps an
/// appearance that it has. The page and the widget are read after the edit
/// takes the writer lock. Returns the widget's object, which a direct
/// dictionary gains here, or `None` for a page that is gone.
#[pyfunction]
pub fn widget_edit(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: Id,
    edit: &Bound<'_, PyAny>,
) -> PyResult<Option<Id>> {
    crate::errors::boundary(|| {
        let (cache, draft) = fill_cache(document);
        editing(
            py,
            document,
            number,
            generation,
            |annots, transaction, budget| {
                let edit = read_widget_edit(edit, budget)?;
                widget_edits::edit_widget(
                    transaction,
                    annots,
                    index,
                    id,
                    &edit,
                    budget,
                    (cache, draft),
                )
                .map_err(annotation_error)
            },
        )
    })?
}

/// Takes the widgets that `handles`, each `(index, object)`, name off page
/// object `(number, generation)` and out of the form, in one edit that
/// reads the page and the form after taking the writer lock. A widget that
/// the form knows leaves every page that lists it and its field's `/Kids`;
/// a field leaves with its last widget, as [`form_field_remove`] removes
/// it. Returns `false` for a page that is gone.
#[pyfunction]
pub fn widget_remove(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    handles: Vec<(u32, Id)>,
) -> PyResult<bool> {
    crate::errors::boundary(|| {
        let cache = fill_cache(document);
        editing(
            py,
            document,
            number,
            generation,
            |annots, transaction, budget| {
                remove::widget_remove(annots, transaction, &handles, budget, cache)
                    .map_err(annotation_error)
            },
        )
        .map(|removed| removed.is_some())
    })?
}

/// Draws the appearances of the annotations and widgets that `annotations`
/// and `widgets` select into page content and takes them off, in one edit,
/// on page object `page`, or on every page for `None`; returns `false` when
/// `page` is no longer a page. The edit reads the pages after it takes the
/// writer lock, and charges every page to its one budget; a refusal on any
/// page changes nothing. Taking the widgets off every page removes the
/// interactive form; taking them off one page takes them out of their
/// fields, with each field that loses its last widget.
#[pyfunction]
#[pyo3(signature = (document, page, annotations, widgets, /))]
pub fn flatten(
    py: Python<'_>,
    document: &_Document,
    page: Option<Id>,
    annotations: bool,
    widgets: bool,
) -> PyResult<bool> {
    crate::errors::boundary(|| {
        let cache = fill_cache(document);
        document.edit(py, |transaction, budget| {
            remove::flatten(
                transaction,
                &document.draft_pages,
                page,
                Selection {
                    annotations,
                    widgets,
                },
                cache,
                budget,
            )
            .map_err(annotation_error)
        })
    })?
}

/// Takes field `field` out of the form with its widgets, in one edit that
/// reads the pages and the form after taking the writer lock: its widgets
/// leave every page that lists them, and the field leaves the field tree
/// and the calculation order with each named parent that it leaves without
/// kids.
#[pyfunction]
pub fn form_field_remove(py: Python<'_>, document: &_Document, field: Id) -> PyResult<()> {
    crate::errors::boundary(|| {
        let cache = fill_cache(document);
        document.edit(py, |transaction, budget| {
            remove::form_field_remove(transaction, &document.draft_pages, field, cache, budget)
                .map_err(annotation_error)
        })
    })?
}

/// Returns how many form walks the document's memos reused and how many
/// they computed, for tests.
#[pyfunction]
pub fn form_walk_counts(document: &_Document) -> PyResult<(u64, u64)> {
    crate::errors::boundary(|| {
        let (form, drafts) = (document.form.stats(), document.draft_form.stats());
        (
            form.hits + drafts.hits,
            form.computations + drafts.computations,
        )
    })
}
