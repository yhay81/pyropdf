//! Popup creation, resizing, removal, open state, and `/IRT` reply wiring.

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Memo;
use pyropdf_store::pages::{PageIndex, PageTree};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::Transaction;

use super::edit::with_page_edit;
use super::geometry::Rect;
use super::read::PageAnnots;
use super::{AnnotationError, AnnotationResult as Result};

impl PageAnnots<'_> {
    /// Sets, moves, or removes (`None`) the `/Popup` annotation of handle
    /// `(index, id)`.
    pub fn set_popup(
        &self,
        transaction: &mut Transaction,
        index: u32,
        id: (u32, u16),
        area: Option<Rect>,
    ) -> Result<(u32, u16)> {
        let (position, dict) = self.find(index, id)?;
        if let Some((x0, y0, x1, y1)) = area
            && !([x0, y0, x1, y1].iter().all(|value| value.is_finite()) && x0 < x1 && y0 < y1)
        {
            return Err(AnnotationError::Pdf(
                "a popup rectangle is finite and not empty".to_owned(),
            ));
        }
        let existing = match dict.get("Popup") {
            Some(Value::Reference(popup)) => {
                match self.resolved(Some(&Value::Reference(*popup)))? {
                    Some(Value::Dict(popup_dict)) => Some((*popup, popup_dict)),
                    _ => None,
                }
            }
            _ => None,
        };
        if area.is_some() && existing.is_none() {
            self.budget
                .check(LimitCode::AnnotationCount, (self.items.len() + 1) as u64)?;
        }
        let area = area.map(|area| self.to_pdf(area));
        let page = self.page().object();
        self.write(
            transaction,
            position,
            false,
            dict,
            |transaction, reference, dict, items, budget| match (area, existing) {
                (Some(area), Some((popup, mut popup_dict))) => {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /Rect key"
                    )]
                    popup_dict.insert(Name::from("Rect"), area);
                    transaction.write(popup.number(), &Value::Dict(popup_dict), budget)?;
                    Ok(false)
                }
                (Some(area), None) => {
                    let popup = transaction.allocate()?;
                    let mut popup_dict = Dict::new();
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "inserts only fixed /Popup dictionary keys"
                    )]
                    {
                        popup_dict.insert(Name::from("Type"), Value::Name(Name::from("Annot")));
                        popup_dict.insert(Name::from("Subtype"), Value::Name(Name::from("Popup")));
                        popup_dict.insert(Name::from("Rect"), area);
                        popup_dict.insert(Name::from("Parent"), Value::Reference(reference));
                        if let Some(page) = page {
                            popup_dict.insert(Name::from("P"), Value::Reference(page));
                        }
                        popup_dict.insert(Name::from("Open"), Value::Boolean(false));
                    }
                    transaction.write(popup.number(), &Value::Dict(popup_dict), budget)?;
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /Popup key"
                    )]
                    dict.insert(Name::from("Popup"), Value::Reference(popup));
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "write_with reserved self.items.len() + 1 slots via BudgetVec"
                    )]
                    items.push(Value::Reference(popup));
                    Ok(true)
                }
                (None, existing) => {
                    dict.remove("Popup");
                    let Some((popup, _)) = existing else {
                        return Ok(false);
                    };
                    let mut kept = BudgetVec::new(budget);
                    kept.try_reserve_exact(items.len())?;
                    for item in items.iter() {
                        if !matches!(item, Value::Reference(listed) if *listed == popup) {
                            kept.try_push(copy_for_edit(item, budget)?)?;
                        }
                    }
                    let changed = kept.len() != items.len();
                    *items = Array::from(kept.into_vec());
                    Ok(changed)
                }
            },
        )
    }

    /// Opens or closes the popup of handle `(index, id)`, and `/Open` on a
    /// `/Text` note itself.
    pub fn set_open(
        &self,
        transaction: &mut Transaction,
        index: u32,
        id: (u32, u16),
        open: bool,
    ) -> Result<(u32, u16)> {
        let (position, dict) = self.find(index, id)?;
        let popup = match dict.get("Popup") {
            Some(Value::Reference(popup)) => {
                match self.resolved(Some(&Value::Reference(*popup)))? {
                    Some(Value::Dict(popup_dict)) => Some((*popup, popup_dict)),
                    _ => None,
                }
            }
            _ => None,
        };
        let is_note = self.name(&dict, "Subtype")?.as_deref() == Some(b"Text");
        if popup.is_none() && !is_note {
            return Err(AnnotationError::Pdf(
                "the annotation has no popup to open; set popup_rect first".to_owned(),
            ));
        }
        self.write(
            transaction,
            position,
            false,
            dict,
            |transaction, _, dict, _, budget| {
                if let Some((popup, mut popup_dict)) = popup {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /Open key"
                    )]
                    popup_dict.insert(Name::from("Open"), Value::Boolean(open));
                    transaction.write(popup.number(), &Value::Dict(popup_dict), budget)?;
                }
                if is_note {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /Open key"
                    )]
                    dict.insert(Name::from("Open"), Value::Boolean(open));
                }
                Ok(false)
            },
        )
    }

    /// Sets `/IRT` of handle `(index, id)` to `target` (another annotation on
    /// the same page), or clears `/IRT` and `/RT` when `target` is `None`.
    pub fn set_reply(
        &self,
        transaction: &mut Transaction,
        index: u32,
        id: (u32, u16),
        target: Option<(u32, u16)>,
    ) -> Result<(u32, u16)> {
        let (position, dict) = self.find(index, id)?;
        let target = match target {
            Some(target) => {
                let target = Reference::new(target.0, target.1);
                let listed = self
                    .items
                    .iter()
                    .any(|item| matches!(item, Value::Reference(listed) if *listed == target));
                if !listed || (target.number(), target.generation()) == id {
                    return Err(AnnotationError::Pdf(
                        "an annotation replies to another annotation of its page".to_owned(),
                    ));
                }
                Some(target)
            }
            None => None,
        };
        self.write(transaction, position, false, dict, |_, _, dict, _, _| {
            match target {
                Some(target) => {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /IRT key"
                    )]
                    dict.insert(Name::from("IRT"), Value::Reference(target));
                }
                None => {
                    dict.remove("IRT");
                    dict.remove("RT");
                }
            }
            Ok(false)
        })
    }
}

/// Moves, adds, or removes (`None`) the popup of handle `(index, id)` on page
/// `(number, generation)`.
#[expect(
    clippy::too_many_arguments,
    reason = "page identity, annotation handle identity, and optional popup rectangle"
)]
pub fn annotation_popup(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    area: Option<Rect>,
) -> Result<Option<(u32, u16)>> {
    with_page_edit(
        transaction,
        draft_pages,
        budget,
        number,
        generation,
        |annots, transaction, _| annots.set_popup(transaction, index, id, area),
    )
}

/// Opens or closes the popup (and `/Text` `/Open`) of handle `(index, id)` on
/// page `(number, generation)`.
#[expect(
    clippy::too_many_arguments,
    reason = "page identity, annotation handle identity, and open flag"
)]
pub fn annotation_open(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    open: bool,
) -> Result<Option<(u32, u16)>> {
    with_page_edit(
        transaction,
        draft_pages,
        budget,
        number,
        generation,
        |annots, transaction, _| annots.set_open(transaction, index, id, open),
    )
}

/// Sets or clears `/IRT` on handle `(index, id)` of page `(number,
/// generation)`.
#[expect(
    clippy::too_many_arguments,
    reason = "page identity, annotation handle identity, and optional reply target"
)]
pub fn annotation_reply(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    target: Option<(u32, u16)>,
) -> Result<Option<(u32, u16)>> {
    with_page_edit(
        transaction,
        draft_pages,
        budget,
        number,
        generation,
        |annots, transaction, _| annots.set_reply(transaction, index, id, target),
    )
}
