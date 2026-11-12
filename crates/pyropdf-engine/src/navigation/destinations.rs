//! Named destinations, destination arrays, and action/destination target
//! reading and encoding.

use std::collections::HashSet;
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::{Memo, Reads};
use pyropdf_store::pages::{MAX_CHAIN, PageIndex};
use pyropdf_store::refs;
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::Version;

use crate::display::{display_box, encode_text, number, rotation, to_display, to_pdf};

use super::{Error, Named, Place, Result, Target, none, visit};

/// The largest magnitude of a PDF real number, which readers hold as `f32`.
const MAX_REAL: f64 = f32::MAX as f64;

/// The page of a destination before it is resolved.
#[derive(Clone, Copy, Debug)]
enum DestinationPage {
    Reference(Reference),
    /// Some producers write a zero-based page index instead of a reference.
    Index(i64),
    Missing,
}

/// The fits of a destination that carry a point.
#[derive(Clone, Copy, Debug)]
enum Fit {
    Xyz,
    /// `/FitH` or `/FitBH`, with a top coordinate.
    Horizontal,
    /// `/FitV` or `/FitBV`, with a left coordinate.
    Vertical,
    Other,
}

/// The fits that a destination array names (ISO 32000-2 table 149).
const FITS: [&str; 8] = [
    "XYZ", "Fit", "FitB", "FitH", "FitBH", "FitV", "FitBV", "FitR",
];

/// A destination array, reduced to what a target reports and what
/// [`Destination::array`] writes again.
#[derive(Clone, Copy, Debug)]
pub struct Destination {
    page: DestinationPage,
    fit: Fit,
    /// The fit's name, if it is one of [`FITS`].
    name: Option<&'static str>,
    /// Array elements 2 to 5: the fit's coordinates and the zoom.
    numbers: [Option<f64>; 4],
}

impl Destination {
    pub const UNRESOLVED: Self = Self {
        page: DestinationPage::Missing,
        fit: Fit::Other,
        name: None,
        numbers: [None; 4],
    };

    /// Reduces a destination array, or a dictionary whose `/D` is one,
    /// resolving its elements with `resolve`.
    pub fn read(
        value: Option<Value>,
        resolve: &mut dyn FnMut(Option<&Value>) -> Result<Option<Value>>,
    ) -> Result<Self> {
        let value = match value {
            Some(Value::Dict(dict)) => resolve(dict.get("D"))?,
            value => value,
        };
        let Some(Value::Array(array)) = value else {
            return Ok(Self::UNRESOLVED);
        };
        let page = match array.get(0) {
            Some(Value::Reference(reference)) => DestinationPage::Reference(*reference),
            Some(Value::Integer(index)) => DestinationPage::Index(*index),
            _ => DestinationPage::Missing,
        };
        let name = match resolve(array.get(1))? {
            Some(Value::Name(name)) => FITS
                .into_iter()
                .find(|fit| fit.as_bytes() == name.as_bytes()),
            _ => None,
        };
        let fit = match name {
            Some("XYZ") => Fit::Xyz,
            Some("FitH" | "FitBH") => Fit::Horizontal,
            Some("FitV" | "FitBV") => Fit::Vertical,
            _ => Fit::Other,
        };
        let mut numbers = [None; 4];
        for (slot, at) in numbers.iter_mut().zip(2..) {
            *slot = match resolve(array.get(at))? {
                Some(Value::Integer(value)) => Some(value as f64),
                Some(Value::Real(value)) => Some(value.value()),
                _ => None,
            };
        }
        Ok(Self {
            page,
            fit,
            name,
            numbers,
        })
    }

    /// Returns the page that the destination names in `index`.
    #[must_use]
    pub fn page_in(&self, index: &PageIndex) -> Option<Reference> {
        let at = match self.page {
            DestinationPage::Reference(reference) => index.index_of(reference)?,
            DestinationPage::Index(at) => usize::try_from(at).ok()?,
            DestinationPage::Missing => return None,
        };
        index.get(at)?.object()
    }

    /// Returns the destination as an array that names `page`, with the
    /// operands that its fit takes; an unknown fit becomes `/Fit`.
    #[must_use]
    #[expect(
        clippy::disallowed_methods,
        reason = "a destination has two fixed entries and at most four operands from its fixed-size numbers array"
    )]
    pub fn array(&self, page: Reference) -> Array {
        let (name, operands) = match self.name {
            Some(name @ ("XYZ" | "FitR")) => (name, if name == "XYZ" { 3 } else { 4 }),
            Some(name @ ("FitH" | "FitBH" | "FitV" | "FitBV")) => (name, 1),
            Some(name) => (name, 0),
            None => ("Fit", 0),
        };
        let mut array = Array::from(vec![Value::Reference(page), Value::Name(Name::from(name))]);
        for operand in &self.numbers[..operands] {
            array.push(operand.map_or(Value::Null, number));
        }
        array
    }

    /// Returns the zero-based page, display point, and zoom that the
    /// destination names in `index`, or `None` when it names no page. A
    /// destination that leaves its coordinates null keeps the viewer's
    /// position, so it has no point.
    #[must_use]
    pub fn place(&self, index: &PageIndex) -> Option<Place> {
        let pages = index.pages();
        let at = match self.page {
            DestinationPage::Reference(reference) => index.index_of(reference)?,
            DestinationPage::Index(at) => {
                usize::try_from(at).ok().filter(|at| *at < pages.len())?
            }
            DestinationPage::Missing => return None,
        };
        let page = &pages[at];
        let crop = display_box(page);
        let turn = rotation(page.rotate());
        let unit = page.user_unit();
        let [first, second, third, _] = self.numbers;
        let (point, zoom) = match self.fit {
            Fit::Xyz => (
                (first.is_some() || second.is_some()).then(|| {
                    to_display(
                        crop,
                        turn,
                        unit,
                        first.unwrap_or(crop[0]),
                        second.unwrap_or(crop[3]),
                    )
                }),
                third.filter(|zoom| *zoom != 0.0),
            ),
            Fit::Horizontal => (
                first.map(|top| to_display(crop, turn, unit, crop[0], top)),
                None,
            ),
            Fit::Vertical => (
                first.map(|left| to_display(crop, turn, unit, left, crop[3])),
                None,
            ),
            Fit::Other => (None, None),
        };
        Some((u32::try_from(at).ok()?, point, zoom))
    }
}

/// The named destinations of a version: the names of the `/Names /Dests`
/// tree, the first entry of a repeated name winning, then those of the
/// legacy `/Dests` dictionary that the tree lacks.
pub struct NamedDestinations {
    names: Vec<(Box<[u8]>, Destination)>,
    /// Positions in `names`, sorted by name.
    sorted: Vec<usize>,
}

impl NamedDestinations {
    #[must_use]
    pub fn get(&self, name: &[u8]) -> Option<Destination> {
        let at = self
            .sorted
            .binary_search_by(|at| (*self.names[*at].0).cmp(name))
            .ok()?;
        Some(self.names[self.sorted[at]].1)
    }

    /// Returns the memoized named destinations of `version`.
    pub fn memoized(
        memo: &Memo<(), Self>,
        version: &Arc<Version>,
        budget: &Budget,
    ) -> Result<Arc<Self>> {
        memo.get_or_compute(&(), version, budget, Self::build)
    }

    /// Indexes the named destinations that `reads` reads.
    pub fn build(reads: &mut Reads<'_>) -> Result<Self> {
        let budget = reads.budget();
        let root = reads.trailer().get("Root");
        let catalog = match read(reads, root)? {
            Some(Value::Dict(catalog)) => catalog,
            _ => Dict::new(),
        };
        let mut found = BudgetVec::new(budget);
        let tree = match read(reads, catalog.get("Names"))? {
            Some(Value::Dict(mut names)) => names.remove("Dests"),
            _ => None,
        };
        if let Some(tree) = tree {
            walk(reads, tree, &mut found)?;
        }
        if let Some(Value::Dict(legacy)) = read(reads, catalog.get("Dests"))? {
            for (key, value) in legacy.iter() {
                budget.charge(LimitCode::NamedDestinationCount, 1)?;
                charge_names(key.as_bytes().len(), budget)?;
                let value = read(reads, Some(value))?;
                let destination = Destination::read(value, &mut |value| read(reads, value))?;
                found.try_push((Box::from(key.as_bytes()), destination))?;
            }
        }
        // Break name ties by input position, so the first occurrence wins
        // without the extra allocation of a stable sort.
        let mut sorted = BudgetVec::new(budget);
        for at in 0..found.len() {
            sorted.try_push(at)?;
        }
        let mut sorted = sorted.into_vec();
        sorted.sort_unstable_by(|a, b| found[*a].0.cmp(&found[*b].0).then(a.cmp(b)));
        sorted.dedup_by(|later, earlier| found[*later].0 == found[*earlier].0);
        let mut kept = BudgetVec::new(budget);
        for _ in 0..found.len() {
            kept.try_push(false)?;
        }
        for at in &sorted {
            kept[*at] = true;
        }
        let mut names = BudgetVec::new(budget);
        for (entry, kept) in found.into_vec().into_iter().zip(kept.into_vec()) {
            if kept {
                names.try_push(entry)?;
            }
        }
        let mut sorted = BudgetVec::new(budget);
        for at in 0..names.len() {
            sorted.try_push(at)?;
        }
        sorted.sort_unstable_by(|a, b| names[*a].0.cmp(&names[*b].0));
        budget.poll()?;
        Ok(Self {
            names: names.into_vec(),
            sorted: sorted.into_vec(),
        })
    }
}

/// Returns the named destinations of `version` in the order that
/// `doc.destinations` lists them.
pub fn named_destinations(
    memo: &Memo<(), NamedDestinations>,
    version: &Arc<Version>,
    index: &PageIndex,
    budget: &Budget,
) -> Result<Vec<Named>> {
    let named = NamedDestinations::memoized(memo, version, budget)?;
    let mut out = BudgetVec::new(budget);
    for (name, destination) in &named.names {
        budget.charge(LimitCode::MemorySize, name.len().saturating_mul(3) as u64)?;
        out.try_push((
            String::from_utf8_lossy(name).into_owned(),
            destination.place(index),
        ))?;
    }
    Ok(out.into_vec())
}

/// Follows references from `value` through `reads`, at most [`MAX_CHAIN`];
/// a null, an absent object, or a missing entry is `None`.
pub fn read(reads: &mut Reads<'_>, value: Option<&Value>) -> Result<Option<Value>> {
    let mut current = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(value) => return Ok(Some(refs::copy_for_edit(value, reads.budget())?)),
    };
    for _ in 0..MAX_CHAIN {
        let Some(object) = reads
            .get(current.number())?
            .filter(|object| object.generation() == current.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Null => return Ok(None),
            Value::Reference(next) => current = *next,
            value => return Ok(Some(refs::copy_for_edit(value, reads.budget())?)),
        }
    }
    Err(Error::Pdf(format!(
        "a reference cycle or a chain of more than {MAX_CHAIN} references"
    )))
}

/// Walks the `/Names /Dests` tree from `root` and adds its entries to
/// `found`, under the limits of its entries, nodes, edges, levels, and
/// name bytes.
fn walk(
    reads: &mut Reads<'_>,
    root: Value,
    found: &mut BudgetVec<'_, (Box<[u8]>, Destination)>,
) -> Result<()> {
    let budget = reads.budget();
    let mut stack = BudgetVec::new(budget);
    stack.try_push((root, 1_usize))?;
    let mut visited = HashSet::new();
    let (mut nodes, mut edges) = (0_usize, 0_usize);
    while let Some((node, depth)) = stack.pop() {
        budget.poll()?;
        if let Value::Reference(reference) = node
            && !visit(&mut visited, reference, budget)?
        {
            continue;
        }
        let Some(Value::Dict(node)) = read(reads, Some(&node))? else {
            continue;
        };
        nodes += 1;
        budget.check(LimitCode::NamedDestinationCount, nodes as u64)?;
        if let Some(Value::Array(names)) = read(reads, node.get("Names"))? {
            budget.charge(
                LimitCode::NamedDestinationCount,
                names.len().div_ceil(2) as u64,
            )?;
            for pair in names.as_slice().chunks(2) {
                let [key, value] = pair else { continue };
                let Some(Value::String(key)) = read(reads, Some(key))? else {
                    continue;
                };
                charge_names(key.as_bytes().len(), budget)?;
                let value = read(reads, Some(value))?;
                let destination = Destination::read(value, &mut |value| read(reads, value))?;
                found.try_push((Box::from(key.as_bytes()), destination))?;
            }
        }
        if let Some(Value::Array(kids)) = read(reads, node.get("Kids"))? {
            if !kids.is_empty() {
                budget.check(LimitCode::NamedDestinationDepth, (depth + 1) as u64)?;
            }
            edges += kids.len();
            budget.check(LimitCode::NamedDestinationEdgeCount, edges as u64)?;
            for kid in kids.iter().rev() {
                stack.try_push((refs::copy_for_edit(kid, budget)?, depth + 1))?;
            }
        }
    }
    Ok(())
}

fn charge_names(amount: usize, budget: &Budget) -> Result<()> {
    budget.charge(LimitCode::NamedDestinationTextSize, amount as u64)?;
    budget.charge(LimitCode::MemorySize, amount as u64)?;
    Ok(())
}

/// Where a link or an outline item leads before its destination resolves:
/// a target, a destination, or the name of a named destination.
#[derive(Clone, Debug)]
pub enum Lead {
    Target(Target),
    Destination(Destination),
    Name(Box<[u8]>),
}

/// What reading a [`Lead`] needs: the values that references name, and
/// text that a budget charges.
pub trait LeadReader {
    /// The shared operation budget for owned target buffers.
    fn budget(&self) -> &Budget;

    /// Copies a named destination only after charging its owned bytes.
    fn name(&self, bytes: &[u8]) -> Result<Box<[u8]>> {
        self.budget()
            .charge(LimitCode::MemorySize, bytes.len() as u64)?;
        Ok(Box::from(bytes))
    }

    /// Returns `value`, or what it refers to; `None` for a null.
    fn resolved(&mut self, value: Option<&Value>) -> Result<Option<Value>>;

    /// Returns `bytes` as text, decoded as a PDF text string when
    /// `decode`, after charging them as `label`.
    fn text(&mut self, bytes: &[u8], label: &str, decode: bool) -> Result<String>;
}

/// Returns where `dict`, a link or an outline item, leads: its `/A`
/// action, else its `/Dest`.
pub fn lead(reader: &mut impl LeadReader, dict: &Dict) -> Result<Lead> {
    let Some(Value::Dict(action)) = reader.resolved(dict.get("A"))? else {
        return match dict.get("Dest") {
            Some(destination) => destination_lead(reader, destination),
            None => Ok(Lead::Target(none())),
        };
    };
    let kind = match reader.resolved(action.get("S"))? {
        Some(Value::Name(kind)) => kind,
        _ => return Ok(Lead::Target(none())),
    };
    Ok(Lead::Target(match kind.as_bytes() {
        b"URI" => {
            let uri = match reader.resolved(action.get("URI"))? {
                Some(Value::String(uri)) => Some(reader.text(uri.as_bytes(), "URI text", false)?),
                _ => None,
            };
            ("uri".to_owned(), None, None, None, uri, None)
        }
        b"GoTo" => match action.get("D") {
            Some(destination) => return destination_lead(reader, destination),
            None => none(),
        },
        b"GoToR" => {
            let file = file(reader, action.get("F"))?;
            let (page, name) = match reader.resolved(action.get("D"))? {
                Some(Value::String(name)) => (
                    None,
                    Some(reader.text(name.as_bytes(), "named destination", false)?),
                ),
                Some(Value::Name(name)) => (
                    None,
                    Some(reader.text(name.as_bytes(), "named destination", false)?),
                ),
                Some(Value::Array(items)) => (
                    match items.get(0) {
                        Some(Value::Integer(page)) => u32::try_from(*page).ok(),
                        _ => None,
                    },
                    None,
                ),
                _ => (None, None),
            };
            ("gotor".to_owned(), page, None, None, file, name)
        }
        b"Launch" => {
            let file = file(reader, action.get("F"))?;
            ("launch".to_owned(), None, None, None, file, None)
        }
        b"Named" => {
            let name = match reader.resolved(action.get("N"))? {
                Some(Value::Name(name)) => {
                    Some(reader.text(name.as_bytes(), "named action", false)?)
                }
                _ => None,
            };
            ("named".to_owned(), None, None, None, None, name)
        }
        _ => none(),
    }))
}

/// Returns the name of a file specification: its `/UF`, else its `/F`,
/// or the string itself.
fn file(reader: &mut impl LeadReader, value: Option<&Value>) -> Result<Option<String>> {
    let name = match reader.resolved(value)? {
        Some(Value::Dict(spec)) => match reader.resolved(spec.get("UF"))? {
            Some(name) => Some(name),
            None => reader.resolved(spec.get("F"))?,
        },
        value => value,
    };
    let Some(Value::String(name)) = name else {
        return Ok(None);
    };
    reader.text(name.as_bytes(), "file name", true).map(Some)
}

/// Returns a destination of this document, or the name of one.
fn destination_lead(reader: &mut impl LeadReader, value: &Value) -> Result<Lead> {
    Ok(match reader.resolved(Some(value))? {
        Some(Value::Name(name)) => Lead::Name(reader.name(name.as_bytes())?),
        Some(Value::String(name)) => Lead::Name(reader.name(name.as_bytes())?),
        value => Lead::Destination(Destination::read(value, &mut |value| {
            reader.resolved(value)
        })?),
    })
}

/// Returns the target of `lead` in `index`, with `lookup` giving the
/// destination of a name: `"goto"` for a destination that names a page,
/// `"none"` for one that does not.
pub fn settle(
    lead: &Lead,
    index: &PageIndex,
    budget: &Budget,
    lookup: &mut dyn FnMut(&[u8]) -> Result<Destination>,
) -> Result<Target> {
    let destination = match lead {
        Lead::Target(target) => {
            let bytes = target
                .0
                .len()
                .saturating_add(target.4.as_deref().map_or(0, str::len))
                .saturating_add(target.5.as_deref().map_or(0, str::len));
            budget.charge(LimitCode::MemorySize, bytes as u64)?;
            return Ok(target.clone());
        }
        Lead::Destination(destination) => *destination,
        Lead::Name(name) => lookup(name)?,
    };
    Ok(match destination.place(index) {
        Some((page, point, zoom)) => ("goto".to_owned(), Some(page), point, zoom, None, None),
        None => none(),
    })
}

/// Returns a number that a destination writes, refusing one outside the
/// range of PDF real numbers.
fn real(value: f64, label: &str) -> Result<Value> {
    if !value.is_finite() || value.abs() > MAX_REAL {
        return Err(Error::Pdf(format!(
            "a destination's {label} {value} is outside the PDF real-number range"
        )));
    }
    Ok(number(value))
}

/// Returns the key and value that make a link or an outline item lead to
/// `target`, a page of `index` for `"goto"`: `/Dest` for a page of this
/// document, `/A` for an action, or nothing.
#[expect(
    clippy::disallowed_methods,
    reason = "the action entries come from a fixed-size literal array"
)]
pub fn target_entry(index: &PageIndex, target: &Target) -> Result<Option<(&'static str, Value)>> {
    let (kind, page, point, zoom, text, name) = target;
    let text = |label: &str| {
        text.as_deref()
            .filter(|text| !text.is_empty())
            .ok_or_else(|| Error::Pdf(format!("a {kind} target needs a {label}")))
    };
    let action = |entries: Vec<(&str, Value)>| {
        Some((
            "A",
            Value::Dict(
                entries
                    .into_iter()
                    .map(|(key, value)| (Name::from(key), value))
                    .collect(),
            ),
        ))
    };
    Ok(match kind.as_str() {
        "none" => None,
        "goto" => {
            let at = page.ok_or_else(|| Error::Pdf("a destination needs a page".to_owned()))?;
            let pages = index.pages();
            let Some(target) = pages.get(at as usize) else {
                return Err(Error::Pdf(format!(
                    "destination page {at} is out of range (0..{})",
                    pages.len().saturating_sub(1)
                )));
            };
            let reference = target.object().ok_or_else(|| {
                Error::Pdf("the destination page is not an indirect object".to_owned())
            })?;
            let zoom = match zoom {
                Some(zoom) if *zoom < 0.0 => {
                    return Err(Error::Pdf(
                        "a destination's zoom must not be negative".to_owned(),
                    ));
                }
                Some(zoom) => Some(real(*zoom, "zoom")?),
                None => None,
            };
            let array = match (point, zoom) {
                (None, None) => vec![Value::Reference(reference), Value::Name(Name::from("Fit"))],
                (point, zoom) => {
                    let (left, top) = match point {
                        Some((x, y)) => {
                            let (left, top) = to_pdf(
                                display_box(target),
                                rotation(target.rotate()),
                                target.user_unit(),
                                *x,
                                *y,
                            );
                            (real(left, "left")?, real(top, "top")?)
                        }
                        None => (Value::Null, Value::Null),
                    };
                    vec![
                        Value::Reference(reference),
                        Value::Name(Name::from("XYZ")),
                        left,
                        top,
                        zoom.unwrap_or(Value::Null),
                    ]
                }
            };
            Some(("Dest", Value::Array(Array::from(array))))
        }
        "uri" => action(vec![
            ("S", Value::Name(Name::from("URI"))),
            (
                "URI",
                Value::String(PdfString::literal(text("URI")?.as_bytes())),
            ),
        ]),
        "gotor" => {
            let destination = match name.as_deref().filter(|name| !name.is_empty()) {
                Some(name) => Value::String(PdfString::literal(name.as_bytes())),
                None => Value::Array(Array::from(vec![
                    Value::Integer(i64::from(page.unwrap_or(0))),
                    Value::Name(Name::from("Fit")),
                ])),
            };
            action(vec![
                ("S", Value::Name(Name::from("GoToR"))),
                ("F", encode_text(text("file")?)),
                ("D", destination),
            ])
        }
        "launch" => action(vec![
            ("S", Value::Name(Name::from("Launch"))),
            ("F", encode_text(text("file")?)),
        ]),
        "named" => {
            let name = name
                .as_deref()
                .filter(|name| !name.is_empty())
                .ok_or_else(|| Error::Pdf("a named action needs a name".to_owned()))?;
            action(vec![
                ("S", Value::Name(Name::from("Named"))),
                ("N", Value::Name(Name::from(name))),
            ])
        }
        other => return Err(Error::Pdf(format!("unknown target kind {other:?}"))),
    })
}
