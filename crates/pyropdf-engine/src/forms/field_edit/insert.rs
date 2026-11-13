//! Inserting widgets onto a page and registering or joining their AcroForm
//! fields.

use std::borrow::Cow;

use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::refs::copy_array_for_edit;
use pyropdf_store::settle::{Scope, Structure};
use pyropdf_store::value::{Array, Dict, Name, PdfString, Value};
use pyropdf_store::version::Transaction;

use crate::annotations::{AnnotationError as Error, AnnotationResult as Result, PageAnnots};
use crate::forms as widgets;
use crate::forms::fill::{After, FillValue, base, change, is_button, shows_text};
use crate::forms::fill_index::{FillCache, indexed_value};
use crate::forms::tree_edit::{append_references, join};
use crate::forms::values::{Id, id};
use crate::forms::widget_edit::pdf_font_size;

use super::acroform::{form_object, register_standard_font};
use super::spec::FieldSpec;

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
            append_references(transaction, parent, "Kids", &[reference], budget)?;
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
            append_references(transaction, holder, "Fields", &[parent], budget)?;
            id(parent)
        }
        None => {
            for (key, value) in field.iter_mut() {
                widget.insert(key.clone(), std::mem::replace(value, Value::Null));
            }
            append_references(transaction, holder, "Fields", &[reference], budget)?;
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
    let mut items = copy_array_for_edit(&annots.items, budget)?;
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
