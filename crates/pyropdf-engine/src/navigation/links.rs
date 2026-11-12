//! Link annotations: listing, reading, adding, and editing `/Link`
//! annotations on a page.

use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Memo;
use pyropdf_store::pages::{MAX_CHAIN, Page, PageIndex, PageTree};
use pyropdf_store::refs;
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::display::{display_box, number, rotation, to_display, to_pdf};
use crate::forms::text_string;

use super::destinations::{Destination, LeadReader, NamedDestinations, lead, settle, target_entry};
use super::{Error, LinkRecord, Rect, Result, Target, with_savepoint};

/// Resolves at most [`MAX_CHAIN`] references, charging the owned result
/// before copying it.
fn resolve(version: &Version, value: Option<&Value>, budget: &Budget) -> Result<Option<Value>> {
    let mut current = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(value) => return Ok(Some(refs::copy_for_edit(value, budget)?)),
    };
    for _ in 0..MAX_CHAIN {
        let Some(object) = version
            .get(current.number(), budget)?
            .filter(|object| object.generation() == current.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Null => return Ok(None),
            Value::Reference(next) => current = *next,
            value => return Ok(Some(refs::copy_for_edit(value, budget)?)),
        }
    }
    Err(Error::Malformed(
        "a reference chain is longer than 16 references".to_owned(),
    ))
}

/// The `/Annots` array and display space of one page.
struct PageAnnots<'v, 'b> {
    version: &'v Arc<Version>,
    index: &'v PageIndex,
    at: usize,
    budget: &'b Budget,
    crop: [f64; 4],
    rotation: i64,
    unit: f64,
    array: Option<Reference>,
    items: Array,
}

impl<'v, 'b> PageAnnots<'v, 'b> {
    fn read(
        version: &'v Arc<Version>,
        index: &'v PageIndex,
        budget: &'b Budget,
        number: u32,
        generation: u16,
    ) -> Result<Option<Self>> {
        let Some(at) = index.index_of(Reference::new(number, generation)) else {
            return Ok(None);
        };
        let page = &index.pages()[at];
        let crop = display_box(page);
        let rotation = rotation(page.rotate());
        let unit = page.user_unit();
        let annots = page.dict().get("Annots");
        let array = match annots {
            Some(Value::Reference(reference)) => Some(*reference),
            _ => None,
        };
        let items = match resolve(version, annots, budget)? {
            Some(Value::Array(items)) => items,
            _ => Array::new(),
        };
        budget.check(LimitCode::AnnotationCount, items.len() as u64)?;
        Ok(Some(Self {
            version,
            index,
            at,
            budget,
            crop,
            rotation,
            unit,
            array,
            items,
        }))
    }

    fn page(&self) -> &Page {
        &self.index.pages()[self.at]
    }

    fn resolved(&self, value: Option<&Value>) -> Result<Option<Value>> {
        resolve(self.version, value, self.budget)
    }

    fn entry(&self, item: &Value) -> Result<Option<((u32, u16), Dict)>> {
        let id = match item {
            Value::Reference(reference) => (reference.number(), reference.generation()),
            Value::Dict(_) => {
                let Value::Dict(dict) = refs::copy_for_edit(item, self.budget)? else {
                    unreachable!("checked dictionary variant");
                };
                return Ok(Some(((0, 0), dict)));
            }
            _ => return Ok(None),
        };
        Ok(match self.resolved(Some(item))? {
            Some(Value::Dict(dict)) => Some((id, dict)),
            _ => None,
        })
    }

    fn position(&self, index: u32, id: (u32, u16)) -> Result<usize> {
        let names = |item: &Value| {
            matches!(item, Value::Reference(reference)
                if (reference.number(), reference.generation()) == id)
        };
        let index = index as usize;
        let position = match self.items.get(index) {
            Some(item) if id != (0, 0) && names(item) => Some(index),
            Some(Value::Dict(_)) if id == (0, 0) => Some(index),
            _ if id != (0, 0) => self.items.iter().position(names),
            _ => None,
        };
        position.ok_or_else(|| {
            Error::Stale(format!(
                "annotation {} is no longer in the page's /Annots",
                id.0
            ))
        })
    }

    fn find(&self, index: u32, id: (u32, u16)) -> Result<(usize, Dict)> {
        let position = self.position(index, id)?;
        match self.entry(&self.items.as_slice()[position])? {
            Some((_, dict)) => Ok((position, dict)),
            None => Err(Error::Stale(format!(
                "annotation {} is no longer a dictionary",
                id.0
            ))),
        }
    }

    fn is_link(&self, dict: &Dict) -> Result<bool> {
        let Some(Value::Name(name)) = self.resolved(dict.get("Subtype"))? else {
            return Ok(false);
        };
        self.budget
            .check(LimitCode::AnnotationTextSize, name.as_bytes().len() as u64)?;
        self.budget
            .charge(LimitCode::MemorySize, name.as_bytes().len() as u64)?;
        Ok(name.as_bytes() == b"Link")
    }

    fn rect(&self, value: Option<&Value>) -> Result<Option<Rect>> {
        let Some(Value::Array(items)) = self.resolved(value)? else {
            return Ok(None);
        };
        if items.len() != 4 {
            return Ok(None);
        }
        let mut numbers = [0.0; 4];
        for (slot, item) in numbers.iter_mut().zip(items.iter()) {
            *slot = match self.resolved(Some(item))? {
                Some(Value::Integer(value)) => value as f64,
                Some(Value::Real(value)) => value.value(),
                _ => return Ok(None),
            };
        }
        let [a, b, c, d] = numbers;
        Ok(Some((a.min(c), b.min(d), a.max(c), b.max(d))))
    }

    fn to_display(&self, (x0, y0, x1, y1): Rect) -> Rect {
        let (a, b) = to_display(self.crop, self.rotation, self.unit, x0, y0);
        let (c, d) = to_display(self.crop, self.rotation, self.unit, x1, y1);
        (a.min(c), b.min(d), a.max(c), b.max(d))
    }

    fn to_pdf(&self, (x0, y0, x1, y1): Rect) -> Value {
        let (a, b) = to_pdf(self.crop, self.rotation, self.unit, x0, y0);
        let (c, d) = to_pdf(self.crop, self.rotation, self.unit, x1, y1);
        Value::Array(Array::from(vec![
            number(a.min(c)),
            number(b.min(d)),
            number(a.max(c)),
            number(b.max(d)),
        ]))
    }

    fn shared(&self) -> bool {
        let Some(array) = self.array else {
            return false;
        };
        self.index.pages().iter().enumerate().any(|(at, page)| {
            at != self.at
                && matches!(page.dict().get("Annots"), Some(Value::Reference(other)) if *other == array)
        })
    }

    fn write_items(&self, transaction: &mut Transaction, items: Array) -> Result<()> {
        if let Some(array) = self.array
            && !self.shared()
        {
            transaction.write(array.number(), &Value::Array(items), self.budget)?;
            return Ok(());
        }
        let page = self.page();
        let Some(object) = page.object() else {
            return Err(Error::Pdf("the page is not an indirect object".to_owned()));
        };
        let mut entries = BudgetVec::new(self.budget);
        entries.try_reserve_exact(page.dict().len() + 1)?;
        let mut items = Some(items);
        for (key, value) in page.dict().iter() {
            self.budget
                .charge(LimitCode::MemorySize, key.as_bytes().len() as u64)?;
            let copied = if key.as_bytes() == b"Annots" {
                Value::Array(items.take().unwrap_or_default())
            } else {
                refs::copy_for_edit(value, self.budget)?
            };
            entries.try_push((Name::from(key.as_bytes()), copied))?;
        }
        if let Some(items) = items {
            entries.try_push((Name::from("Annots"), Value::Array(items)))?;
        }
        let mut dict = Dict::new();
        for (key, value) in entries.into_vec() {
            #[expect(
                clippy::disallowed_methods,
                reason = "each entry was copied and charged via BudgetVec above"
            )]
            dict.insert(key, value);
        }
        transaction.write(object.number(), &Value::Dict(dict), self.budget)?;
        Ok(())
    }

    fn write(
        mut self,
        transaction: &mut Transaction,
        position: usize,
        mut dict: Dict,
        change: impl FnOnce(&mut Dict),
    ) -> Result<(u32, u16)> {
        let mut listed = false;
        if matches!(self.items.get(position), Some(Value::Dict(_))) {
            for item in self.items.iter_mut() {
                if let Value::Dict(_) = item {
                    let entry = refs::copy_for_edit(item, self.budget)?;
                    let reference = transaction.allocate()?;
                    transaction.write(reference.number(), &entry, self.budget)?;
                    *item = Value::Reference(reference);
                    listed = true;
                }
            }
        }
        let Some(&Value::Reference(reference)) = self.items.get(position) else {
            return Err(Error::Pdf(
                "the annotation changed while it was edited".to_owned(),
            ));
        };
        change(&mut dict);
        transaction.write(reference.number(), &Value::Dict(dict), self.budget)?;
        if listed {
            let items = std::mem::take(&mut self.items);
            self.write_items(transaction, items)?;
        }
        Ok((reference.number(), reference.generation()))
    }
}

fn charge(total: &mut usize, amount: usize, budget: &Budget) -> Result<()> {
    *total = total.saturating_add(amount);
    budget.check(LimitCode::AnnotationTextSize, *total as u64)?;
    Ok(())
}

fn charge_text(
    bytes: &[u8],
    (encoded, returned): &mut (usize, usize),
    budget: &Budget,
) -> Result<String> {
    charge(encoded, bytes.len(), budget)?;
    budget.charge(LimitCode::MemorySize, bytes.len().saturating_mul(3) as u64)?;
    let text = String::from_utf8_lossy(bytes).into_owned();
    charge(returned, text.len(), budget)?;
    Ok(text)
}

fn charge_decoded(
    bytes: &[u8],
    (encoded, returned): &mut (usize, usize),
    budget: &Budget,
) -> Result<String> {
    charge(encoded, bytes.len(), budget)?;
    budget.charge(LimitCode::MemorySize, bytes.len().saturating_mul(3) as u64)?;
    let text = text_string(bytes);
    charge(returned, text.len(), budget)?;
    Ok(text)
}

/// Reads the links of one page, looking named destinations up once per
/// call. `budget` holds the text bytes that the call encoded and returned.
struct Links<'a, 'v, 'b> {
    destinations: &'a Memo<(), NamedDestinations>,
    annots: &'a PageAnnots<'v, 'b>,
    named: Option<Arc<NamedDestinations>>,
    budget: (usize, usize),
}

impl<'a, 'v, 'b> Links<'a, 'v, 'b> {
    const fn new(
        destinations: &'a Memo<(), NamedDestinations>,
        annots: &'a PageAnnots<'v, 'b>,
    ) -> Self {
        Self {
            destinations,
            annots,
            named: None,
            budget: (0, 0),
        }
    }

    fn link(&mut self, dict: &Dict) -> Result<Option<(Rect, Target)>> {
        if !self.annots.is_link(dict)? {
            return Ok(None);
        }
        let Some(area) = self.annots.rect(dict.get("Rect"))? else {
            return Ok(None);
        };
        Ok(Some((self.annots.to_display(area), self.target(dict)?)))
    }

    fn target(&mut self, link: &Dict) -> Result<Target> {
        let lead = lead(self, link)?;
        let annots = self.annots;
        settle(&lead, annots.index, annots.budget, &mut |name| {
            self.lookup(name)
        })
    }

    fn lookup(&mut self, name: &[u8]) -> Result<Destination> {
        charge(&mut self.budget.0, name.len(), self.annots.budget)?;
        if self.named.is_none() {
            self.named = Some(NamedDestinations::memoized(
                self.destinations,
                self.annots.version,
                self.annots.budget,
            )?);
        }
        Ok(self
            .named
            .as_ref()
            .and_then(|named| named.get(name))
            .unwrap_or(Destination::UNRESOLVED))
    }
}

impl LeadReader for Links<'_, '_, '_> {
    fn budget(&self) -> &Budget {
        self.annots.budget
    }

    fn resolved(&mut self, value: Option<&Value>) -> Result<Option<Value>> {
        self.annots.resolved(value)
    }

    fn text(&mut self, bytes: &[u8], _label: &str, decode: bool) -> Result<String> {
        if decode {
            charge_decoded(bytes, &mut self.budget, self.annots.budget)
        } else {
            charge_text(bytes, &mut self.budget, self.annots.budget)
        }
    }
}

/// Returns the links of page object `(number, generation)` in `/Annots`
/// order, or `None` for a page that is gone.
pub fn link_list(
    version: &Arc<Version>,
    pages: &PageIndex,
    destinations: &Memo<(), NamedDestinations>,
    budget: &Budget,
    number: u32,
    generation: u16,
) -> Result<Option<Vec<LinkRecord>>> {
    let Some(annots) = PageAnnots::read(version, pages, budget, number, generation)? else {
        return Ok(None);
    };
    let mut links = Links::new(destinations, &annots);
    let mut out = BudgetVec::new(budget);
    for (position, item) in annots.items.iter().enumerate() {
        let Some((id, dict)) = annots.entry(item)? else {
            continue;
        };
        if let Some((area, target)) = links.link(&dict)? {
            out.try_push((
                u32::try_from(position).unwrap_or(u32::MAX),
                id,
                area,
                target,
            ))?;
        }
    }
    Ok(Some(out.into_vec()))
}

/// Returns the link that a handle names, or `None` for a page that is gone.
#[expect(
    clippy::too_many_arguments,
    reason = "reads a link handle from its version, page index, destination memo, budget, page identity, and annotation identity"
)]
pub fn link_read(
    version: &Arc<Version>,
    pages: &PageIndex,
    destinations: &Memo<(), NamedDestinations>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> Result<Option<LinkRecord>> {
    let Some(annots) = PageAnnots::read(version, pages, budget, number, generation)? else {
        return Ok(None);
    };
    let (position, dict) = annots.find(index, id)?;
    match Links::new(destinations, &annots).link(&dict)? {
        Some((area, target)) => Ok(Some((
            u32::try_from(position).unwrap_or(u32::MAX),
            id,
            area,
            target,
        ))),
        None => Err(Error::Stale(format!(
            "annotation {} at index {position} is no longer a link",
            id.0
        ))),
    }
}

/// Adds a link over a display rectangle of page object `(number,
/// generation)` that leads to `target`, and returns its object, or `None`
/// for a page that is gone.
pub fn link_add(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    area: Rect,
    target: &Target,
) -> Result<Option<(u32, u16)>> {
    let version = transaction.snapshot();
    let pages = PageIndex::memoized(draft_pages, &version, budget)?;
    let Some(annots) = PageAnnots::read(&version, &pages, budget, number, generation)? else {
        return Ok(None);
    };
    with_savepoint(transaction, |transaction| {
        budget.check(LimitCode::AnnotationCount, (annots.items.len() + 1) as u64)?;
        if !matches!(
            annots.resolved(annots.page().dict().get("Annots"))?,
            None | Some(Value::Array(_))
        ) {
            return Err(Error::Pdf("the page's /Annots is not an array".to_owned()));
        }
        let rect = link_rect(&annots, area)?;
        let entry = entry(&annots, target)?;
        let page = annots.page().object();

        let reference = transaction.allocate()?;
        let mut dict = Dict::new();
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Type key"
        )]
        dict.insert(Name::from("Type"), Value::Name(Name::from("Annot")));
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Subtype key"
        )]
        dict.insert(Name::from("Subtype"), Value::Name(Name::from("Link")));
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Rect key"
        )]
        dict.insert(Name::from("Rect"), rect);
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Border key with a fixed 3-element array"
        )]
        dict.insert(
            Name::from("Border"),
            Value::Array(Array::from(vec![Value::Integer(0); 3])),
        );
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /F key"
        )]
        dict.insert(Name::from("F"), Value::Integer(4));
        if let Some(page) = page {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /P key"
            )]
            dict.insert(Name::from("P"), Value::Reference(page));
        }
        if let Some((key, value)) = entry {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the single target key produced by entry"
            )]
            dict.insert(Name::from(key), value);
        }
        transaction.write(reference.number(), &Value::Dict(dict), budget)?;
        let mut items = BudgetVec::new(budget);
        items.try_reserve_exact(annots.items.len() + 1)?;
        for item in annots.items.iter() {
            items.try_push(refs::copy_for_edit(item, budget)?)?;
        }
        items.try_push(Value::Reference(reference))?;
        annots.write_items(transaction, Array::from(items.into_vec()))?;
        Ok(Some((reference.number(), reference.generation())))
    })
}

/// Moves the link that a handle names to a display rectangle, makes it lead
/// to `target`, or both, and returns its object, which a direct dictionary
/// gains here; or `None` for a page that is gone.
#[expect(
    clippy::too_many_arguments,
    reason = "edits a link handle in a transaction using its page identity, annotation identity, and optional geometry and target updates"
)]
pub fn link_edit(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    area: Option<Rect>,
    target: Option<&Target>,
) -> Result<Option<(u32, u16)>> {
    let version = transaction.snapshot();
    let pages = PageIndex::memoized(draft_pages, &version, budget)?;
    let Some(annots) = PageAnnots::read(&version, &pages, budget, number, generation)? else {
        return Ok(None);
    };
    with_savepoint(transaction, |transaction| {
        let (position, dict) = annots.find(index, id)?;
        if !annots.is_link(&dict)? {
            return Err(Error::Stale(format!(
                "annotation {} at index {position} is no longer a link",
                id.0
            )));
        }
        let rect = area.map(|area| link_rect(&annots, area)).transpose()?;
        let entry = target.map(|target| entry(&annots, target)).transpose()?;
        annots
            .write(transaction, position, dict, |dict| {
                if let Some(rect) = rect {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /Rect key"
                    )]
                    dict.insert(Name::from("Rect"), rect);
                }
                if let Some(entry) = entry {
                    dict.remove("Dest");
                    dict.remove("A");
                    if let Some((key, value)) = entry {
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "replaces or adds at most one destination or action key"
                        )]
                        dict.insert(Name::from(key), value);
                    }
                }
            })
            .map(Some)
    })
}

fn link_rect(annots: &PageAnnots<'_, '_>, (x0, y0, x1, y1): Rect) -> Result<Value> {
    if !([x0, y0, x1, y1].iter().all(|value| value.is_finite()) && x0 < x1 && y0 < y1) {
        return Err(Error::Pdf(
            "a link rectangle is finite with x0 < x1 and y0 < y1".to_owned(),
        ));
    }
    Ok(annots.to_pdf((x0, y0, x1, y1)))
}

fn entry(annots: &PageAnnots<'_, '_>, target: &Target) -> Result<Option<(&'static str, Value)>> {
    let (_, _, _, _, text, name) = target;
    let size = 4 + text.as_deref().map_or(0, str::len) + name.as_deref().map_or(0, str::len);
    annots
        .budget
        .charge(LimitCode::AnnotationInputSize, size as u64)?;
    annots
        .budget
        .charge(LimitCode::MemorySize, size.saturating_mul(3) as u64)?;
    target_entry(annots.index, target)
}
