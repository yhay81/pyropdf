//! Compare the embedded programs of painted fonts across the two store versions.
use super::profiles::{PathReads, named};
use super::resources::Use;
use super::*;
use crate::fonts::validity::ProgramFormat;
use pyropdf_store::budget::try_insert_map;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::{Name, Stream};
use std::cell::RefCell;
use std::collections::HashMap;

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct Binding {
    stream: Reference,
    format: ProgramFormat,
}

#[derive(Clone, Copy)]
pub(super) struct Pair {
    before: Binding,
    after: Binding,
    owners: (usize, usize),
}

fn name(value: Option<&Value>) -> Option<&[u8]> {
    match value? {
        Value::Name(n) => Some(n.as_bytes()),
        _ => None,
    }
}

fn binding(path: &mut PathReads<'_>, font: &Value) -> Result<Option<Binding>> {
    let face = path.resolve(Some(font))?;
    let Some(mut dict) = face.as_deref().and_then(value_dict) else {
        return Ok(None);
    };
    let (array, descendant);
    if named(dict, "Subtype", b"Type0") {
        array = path.resolve(dict.get("DescendantFonts"))?;
        let Some(Value::Array(children)) = array.as_deref() else {
            return Ok(None);
        };
        descendant = path.resolve(children.get(0))?;
        let Some(child) = descendant.as_deref().and_then(value_dict) else {
            return Ok(None);
        };
        dict = child;
    }
    let Some(subtype) = name(dict.get("Subtype")) else {
        return Ok(None);
    };
    if subtype == b"Type3" {
        return Ok(None);
    }
    let descriptor = path.resolve(dict.get("FontDescriptor"))?;
    let Some(desc) = descriptor.as_deref().and_then(value_dict) else {
        return Ok(None);
    };
    for key in ["FontFile", "FontFile2", "FontFile3"] {
        let Some(value @ Value::Reference(id)) = desc.get(key) else {
            continue;
        };
        let stream = path.resolve(Some(value))?;
        if let Some(Value::Stream(stream)) = stream.as_deref() {
            let format = ProgramFormat::declared(subtype, key, name(stream.dict().get("Subtype")));
            return Ok(Some(Binding {
                stream: *id,
                format,
            }));
        }
    }
    Ok(None)
}

fn program_changed(a: &Stream, b: &Stream) -> bool {
    a.data() != b.data()
        || [
            "Filter",
            "DecodeParms",
            "F",
            "FFilter",
            "FDecodeParms",
            "Subtype",
        ]
        .iter()
        .any(|k| a.dict().get(k) != b.dict().get(k))
}

fn relevant(before: Option<&Value>, after: Option<&Value>) -> bool {
    match (before, after) {
        (Some(Value::Stream(a)), Some(Value::Stream(b))) => {
            a.dict().get("Resources") != b.dict().get("Resources")
                || program_changed(a, b)
                    && ![b"Metadata".as_slice(), b"XObject", b"XRef", b"ObjStm"]
                        .contains(&name(b.dict().get("Type")).unwrap_or_default())
        }
        (Some(Value::Dict(a)), Some(Value::Dict(b))) => {
            let keys: &[&str] = match name(a.get("Type")) {
                Some(b"FontDescriptor") => &["FontFile", "FontFile2", "FontFile3"],
                Some(b"Font") => &["Subtype", "DescendantFonts", "FontDescriptor"],
                None if b.iter().any(|(n, v)| {
                    a.get(n.as_bytes()) != Some(v)
                        && matches!(v, Value::Dict(_) | Value::Reference(_))
                }) =>
                {
                    return true;
                }
                Some(b"Page" | b"Pages") | None => &["Resources", "Font", "ExtGState"],
                _ => return false,
            };
            keys.iter().any(|key| a.get(key) != b.get(key))
        }
        (Some(Value::Array(a)), Some(Value::Array(b))) => a != b,
        _ => false,
    }
}

/// A font dictionary can itself be direct, including a direct descriptor.
/// Resolve its original selection in the same resource owner in that case.
#[derive(Clone, Copy)]
pub(super) enum Selector<'a> {
    Font(&'a Name),
    State(&'a Name),
}

impl Selector<'_> {
    pub(super) fn name(&self) -> &Name {
        match self {
            Self::Font(n) | Self::State(n) => n,
        }
    }

    fn in_owner(self, reads: &mut PathReads<'_>, owner: u32) -> Result<Option<Binding>> {
        let root = object(reads.version, owner, reads.budget)?;
        if let Some(Value::Array(pair)) = root.as_deref()
            && let Self::State(_) = self
        {
            return match pair.get(0) {
                Some(v) => binding(reads, v),
                None => Ok(None),
            };
        }
        let Some(container) = root.as_deref().and_then(value_dict) else {
            return Ok(None);
        };
        let local = reads.resolve(container.get("Resources"))?;
        let resources = local.as_deref().and_then(value_dict).unwrap_or(container);
        let category = match self {
            Self::Font(_) => "Font",
            Self::State(_) => "ExtGState",
        };
        let group = reads.resolve(resources.get(category))?;
        let entries = group.as_deref().and_then(value_dict).unwrap_or(resources);
        let selected = entries.get(self.name().as_bytes());
        match self {
            Self::Font(_) => match selected {
                Some(v) => binding(reads, v),
                None => Ok(None),
            },
            Self::State(_) => {
                let state = reads.resolve(selected)?;
                let gs = state.as_deref().and_then(value_dict).unwrap_or(resources);
                let font = reads.resolve(gs.get("Font"))?;
                match font.as_deref() {
                    Some(Value::Array(pair)) if let Some(v) = pair.get(0) => binding(reads, v),
                    _ => Ok(None),
                }
            }
        }
    }
}

pub(super) struct Check<'a> {
    opened: &'a Version,
    version: &'a Version,
    edited: BudgetVec<'a, u32>,
    before: RefCell<HashMap<Binding, bool>>,
    after: RefCell<HashMap<Binding, bool>>,
}

impl Check<'_> {
    pub(super) fn select(
        &self,
        path: &mut PathReads<'_>,
        font: &Value,
        selector: Selector<'_>,
    ) -> Result<Option<Pair>> {
        let start = path.owners.len();
        let Some(after) = binding(path, font)? else {
            return Ok(None);
        };
        let owners = path.font_owners.len();
        for &number in &path.owners[start..] {
            if self.edited.binary_search(&number).is_ok() {
                path.font_owners.try_push(number)?;
            }
        }
        let mut old = PathReads::new(self.opened, path.budget);
        let mut previous = None;
        for &owner in path.owners[..start].iter().rev() {
            if self.edited.binary_search(&owner).is_ok() {
                let mut now = PathReads::new(self.version, path.budget);
                if selector.in_owner(&mut now, owner)? == Some(after)
                    && let Some(prior) = selector.in_owner(&mut old, owner)?
                    && prior != after
                {
                    previous = Some(prior);
                    path.font_owners.try_push(owner)?;
                    break;
                }
            }
        }
        let end = path.font_owners.len();
        if owners == end {
            return Ok(None);
        }
        let Some(before) = (match previous {
            Some(p) => Some(p),
            None => binding(&mut old, font)?,
        }) else {
            return Ok(None);
        };
        if before == after && self.edited.binary_search(&after.stream.number()).is_err() {
            return Ok(None);
        }
        Ok(Some(Pair {
            before,
            after,
            owners: (owners, end),
        }))
    }

    fn readable(&self, current: bool, key: Binding, budget: &Budget) -> Result<bool> {
        let (version, cache) = if current {
            (self.version, &self.after)
        } else {
            (self.opened, &self.before)
        };
        if let Some(&known) = cache.borrow().get(&key) {
            return Ok(known);
        }
        let value = object(version, key.stream.number(), budget)?;
        let valid = match value.as_deref() {
            Some(Value::Stream(stream))
                if stream.dict().get("Filter").is_none() && stream.dict().get("F").is_none() =>
            {
                let bytes = stream.data();
                budget
                    .decode(pyropdf_store::budget::StreamKind::Any)
                    .charge(bytes.len() as u64)?;
                key.format.readable(bytes, budget)?
            }
            Some(Value::Stream(stream)) => match stream.decoded(budget) {
                Ok(data) => {
                    let size = data.len() as u64;
                    budget.charge(LimitCode::MemorySize, size)?;
                    let result = key.format.readable(&data, budget);
                    budget.release(LimitCode::MemorySize, size);
                    result?
                }
                Err(error) if error.limit().is_none() => false,
                Err(error) => return Err(error.into()),
            },
            _ => false,
        };
        try_insert_map(&mut cache.borrow_mut(), key, valid, budget)?;
        Ok(valid)
    }

    pub(super) fn shown(&self, path: &mut PathReads<'_>, selected: usize) -> Result<()> {
        let pair = path.embedded_fonts[selected];
        if !self.readable(true, pair.after, path.budget)?
            && self.readable(false, pair.before, path.budget)?
        {
            let (first, last) = pair.owners;
            for &number in &path.font_owners[first..last] {
                if path.changed_programs.len() < MAX_NAMED
                    && !path.changed_programs.contains(&number)
                {
                    path.changed_programs.try_push(number)?;
                }
            }
        }
        Ok(())
    }
}

pub(super) fn changes<'b>(
    opened: &Version,
    version: &Version,
    written: &[u32],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u32>> {
    let mut edited = BudgetVec::new(budget);
    for &id in written {
        let before = object(opened, id, budget)?;
        let after = object(version, id, budget)?;
        if relevant(before.as_deref(), after.as_deref()) {
            edited.try_push(id)?;
        }
    }
    let mut path = PathReads::new(version, budget);
    if !edited.is_empty() {
        let check = Check {
            opened,
            version,
            edited,
            before: RefCell::default(),
            after: RefCell::default(),
        };
        let use_ = Use::DamagedProgram(&check);
        let pages = PageIndex::build(&mut Reads::new(version, budget))?;
        for page in pages.pages() {
            use_.page(&mut path, page)?;
            let annots = path.resolve(page.dict().get("Annots"))?;
            if let Some(Value::Array(items)) = annots.as_deref() {
                for item in items.iter() {
                    budget.poll()?;
                    path.owners.clear();
                    let annotation = path.resolve(Some(item))?;
                    if let Some(dict) = annotation.as_deref().and_then(value_dict) {
                        use_.appearance(&mut path, dict, page)?;
                    }
                }
            }
            path.owners.truncate(0);
            path.font_owners.truncate(0);
            path.embedded_fonts.truncate(0);
        }
    }
    let mut found = BudgetVec::new(budget);
    found.try_extend_from_slice(&path.changed_programs)?;
    Ok(found)
}
