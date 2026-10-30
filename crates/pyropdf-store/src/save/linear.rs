//! Linearized full saves (ISO 32000-2 Annex F, DESIGN.md section 4.4), and
//! the detection of a linearized file.
//!
//! The layout is qpdf's, so that a reader that computes the hint tables
//! itself, as `qpdf --check-linearization` does, finds the tables that the
//! file holds:
//!
//! 1. the header, the linearization dictionary, the first-page
//!    cross-reference section and its trailer;
//! 2. the catalog and the objects of the open document (part 4);
//! 3. the hint stream;
//! 4. the first page's object, the objects that only it uses, and the
//!    objects that it shares with others (part 6);
//! 5. each other page's object and the objects that only it uses (part 7);
//! 6. the objects that other pages share (part 8);
//! 7. the page tree, thumbnails, outlines, and every other object (part 9);
//! 8. the main cross-reference section and its trailer.
//!
//! The objects of 5 to 7 take the numbers 1, 2, 3, and so on in file
//! order, and the linearization dictionary and the objects of 2 to 4 the
//! numbers after them. The page tree's inherited attributes move into the
//! pages, which ISO 32000-2 section F.4.3 allows. The save writes the
//! objects that the trailer reaches through the pages, the trailer's
//! entries, and the catalog's; a page outside the page tree is not
//! written, and references to it become null.
//!
//! The save writes the file twice: once to count, without the hint
//! stream, whose offsets disregard it (section F.3.2), and once to the
//! target. Every field that the counts fill has a fixed width.

use std::fmt;
use std::io;
use std::sync::Arc;

use super::{Error, Mode, Output, Result, Row, Streams, identifier, recode, renews, table};
use crate::budget::{Budget, BudgetVec};
use crate::memo::Reads;
use crate::pages::PageIndex;
use crate::security;
use crate::value::{Bytes, Dict, Name, PdfString, Reference, Stream, Value};
use crate::version::Version;
use crate::{Error as StoreError, write};

/// The attributes that pages inherit (ISO 32000-2 table 31).
const INHERITABLE: [&[u8]; 4] = [b"Resources", b"MediaBox", b"CropBox", b"Rotate"];

/// The catalog entries whose objects a viewer needs to open the document
/// (ISO 32000-2 section F.4.2).
const OPEN: [&[u8]; 5] = [
    b"ViewerPreferences",
    b"PageMode",
    b"Threads",
    b"OpenAction",
    b"AcroForm",
];

/// How far the bytes of the linearization dictionary may look for its
/// object (ISO 32000-2 section F.3.3).
const FIRST_OBJECT_WINDOW: usize = 1024;

/// Returns whether `version`'s file is linearized: its first object, in
/// its first 1024 bytes, is a linearization dictionary whose `/L` is the
/// length of the file (ISO 32000-2 section F.3.3). An update appended to a
/// linearized file changes its length, and so makes it read as not
/// linearized.
pub fn is_linearized(version: &Version, budget: &Budget) -> crate::Result<bool> {
    let Some(file) = version.base().file() else {
        return Ok(false);
    };
    let bytes = file.bytes().get(file.origin()..).unwrap_or_default();
    let Some(reference) = first_object(&bytes[..bytes.len().min(FIRST_OBJECT_WINDOW)]) else {
        return Ok(false);
    };
    let Some(body) = version.base().object(reference, budget)? else {
        return Ok(false);
    };
    let Ok(Value::Dict(dict)) = Value::parse(&body, budget) else {
        return Ok(false);
    };
    Ok(dict.get("Linearized").is_some()
        && matches!(dict.get("L"), Some(Value::Integer(length))
            if u64::try_from(*length).ok() == Some(bytes.len() as u64)))
}

/// Returns the reference of the first `N G obj` in `bytes`.
fn first_object(bytes: &[u8]) -> Option<Reference> {
    let at = bytes.windows(3).position(|window| window == b"obj")?;
    let mut words = bytes[..at]
        .rsplit(|byte| byte.is_ascii_whitespace())
        .filter(|word| !word.is_empty());
    let generation = std::str::from_utf8(words.next()?).ok()?.parse().ok()?;
    let number = std::str::from_utf8(words.next()?).ok()?.parse().ok()?;
    Some(Reference::new(number, generation))
}

/// Who uses an object, as qpdf's linearization groups the users.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum User {
    /// Page `n`, counted from 0.
    Page(u32),
    /// The thumbnail of page `n`.
    Thumb(u32),
    /// A trailer or catalog entry that opening the document reads.
    Open,
    /// The catalog's `/Outlines`.
    Outlines,
    /// The catalog's `/Pages`.
    Tree,
    /// Any other trailer or catalog entry.
    Other,
}

/// The part of the file that an object goes in.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
enum Class {
    /// An object that the save does not write.
    #[default]
    Unused,
    Root,
    Outlines,
    Open,
    FirstPrivate,
    FirstShared,
    OtherPrivate,
    OtherShared,
    ThumbPrivate,
    ThumbShared,
    Other,
}

/// What the save knows of one object of the version, or one that it adds.
#[derive(Clone, Copy, Debug, Default)]
struct Info {
    number: u32,
    /// The last walk that reached the object.
    stamp: u32,
    /// The number of users that reach it.
    users: u32,
    root: bool,
    open: bool,
    outlines: bool,
    first: bool,
    tree: bool,
    /// The number of pages other than the first that use it, and the last.
    pages: u32,
    page: u32,
    /// The number of thumbnails that use it, and the last.
    thumbs: u32,
    thumb: u32,
    others: u32,
    class: Class,
    /// Its number in the save.
    new: u32,
    /// Its position in the file order.
    at: u32,
}

/// The objects of a linearized save, in file order.
struct Plan<'a> {
    version: &'a Version,
    budget: &'a Budget,
    /// The values that the save writes instead of the version's, and the
    /// objects that it adds, ascending by number.
    overrides: BudgetVec<'a, (u32, Arc<Value>)>,
    /// One entry per object, ascending by number.
    infos: BudgetVec<'a, Info>,
    /// The index of each page's entry in `infos`.
    pages: BudgetVec<'a, usize>,
    /// The pages other than the first, and the objects that each reaches.
    visits: BudgetVec<'a, (u32, usize)>,
    stamp: u32,
    /// The number of objects that the save adds.
    added: u32,
    /// The indices in `infos` in file order: parts 4, 6, 7, 8, and 9.
    order: BudgetVec<'a, usize>,
    part4: usize,
    part6: usize,
    /// The start of each page's objects in `order` but the first's, and
    /// of part 8.
    starts: BudgetVec<'a, usize>,
    part8: usize,
    part9: usize,
    /// The outlines' objects in `order`, when the catalog names some.
    outlines: Option<(usize, usize)>,
    /// The `/Encrypt` dictionary's entry, which is never encrypted.
    encrypt: Option<usize>,
}

/// How the save writes `/ID`.
enum Id {
    None,
    /// The trailer's own `/ID`.
    Keep(Value),
    /// `[first digest]`, with the digest of the first pass, or the digest
    /// as both.
    Renew(Option<Value>),
    /// `[first first]`, for a new encryption without a renewed digest.
    Fixed(Value),
}

/// The values that the first pass measures, which the second writes.
#[derive(Clone, Copy, Default)]
struct Fields {
    length: u64,
    hint_offset: u64,
    hint_length: u64,
    end: u64,
    main: u64,
    zero: u64,
}

/// Writes `version` linearized to `target`, encrypted as `mode` asks.
pub(super) fn write(
    version: &Version,
    target: &mut dyn io::Write,
    budget: &Budget,
    mode: &Mode<'_>,
    streams: Streams,
    renew_id: bool,
) -> Result<u64> {
    let mut plan = Plan::new(version, budget)?;
    let added = match mode {
        Mode::New { dict, .. } => Some(plan.add(Value::Dict(dict.clone()))?),
        _ => None,
    };
    plan.index(budget)?;
    let encrypt = match (added, version.trailer().get("Encrypt"), mode) {
        (Some(number), _, _) => Some(Value::Reference(Reference::new(number, 0))),
        (None, Some(value), Mode::Keep { .. }) => Some(value.clone()),
        _ => None,
    };
    plan.encrypt = match &encrypt {
        Some(Value::Reference(reference)) => plan.find(reference.number()),
        _ => None,
    };
    plan.classify(encrypt.as_ref())?;
    plan.arrange()?;
    let id = match mode {
        Mode::New { id, .. } => {
            let first = Value::String(PdfString::hexadecimal(id.as_slice()));
            if renew_id {
                Id::Renew(Some(first))
            } else {
                Id::Fixed(first)
            }
        }
        _ => {
            let trailer = version.trailer().get("ID");
            let first = match trailer {
                Some(Value::Array(id)) => id.get(0).filter(|v| matches!(v, Value::String(_))),
                _ => None,
            };
            match (renew_id, mode, trailer) {
                // The keys of the base's encryption derive from the first.
                (true, Mode::Keep { .. }, _) => Id::Renew(first.cloned()),
                (true, _, _) => Id::Renew(renews(version, true).flatten()),
                (false, _, Some(id)) => Id::Keep(id.clone()),
                (false, _, None) => Id::None,
            }
        }
    };
    let writer = Writer {
        plan: &plan,
        mode,
        streams,
        encrypt: encrypt.as_ref(),
        id: &id,
    };
    let mut sink = io::sink();
    let mut first = Output::new(&mut sink, budget, 0, true);
    first.charge = false;
    let mut positions = BudgetVec::new(budget);
    let fields = writer.emit(&mut first, None, &mut positions, None, None)?;
    let digest = first.digest().expect("the first pass keeps a digest");
    let hint = writer.hint(&positions)?;
    let hint_length = hint.len() as u64;
    let shift = |offset: u64| offset + hint_length;
    let fields = Fields {
        length: shift(fields.length),
        hint_offset: fields.hint_offset,
        hint_length,
        end: shift(fields.end),
        main: shift(fields.main),
        zero: shift(fields.zero),
    };
    let mut out = Output::new(target, budget, 0, false);
    let mut written = BudgetVec::new(budget);
    writer.emit(
        &mut out,
        Some((&hint, &positions)),
        &mut written,
        Some(fields),
        Some(digest),
    )?;
    if out.position != fields.length {
        return Err(changed());
    }
    Ok(out.position)
}

fn changed() -> Error {
    StoreError::malformed("an object's size changed between the passes of a linearized save").into()
}

impl<'a> Plan<'a> {
    fn new(version: &'a Version, budget: &'a Budget) -> Result<Self> {
        let mut numbers = BudgetVec::new(budget);
        version.base().numbers(&mut numbers)?;
        for (number, _) in version.slots() {
            budget.poll()?;
            numbers.try_push(number)?;
        }
        numbers.sort_unstable();
        let mut infos: BudgetVec<'_, Info> = BudgetVec::new(budget);
        for &number in numbers.iter() {
            budget.poll()?;
            if number == 0
                || infos.last().is_some_and(|info| info.number == number)
                || version.generation(number).is_none()
            {
                continue;
            }
            infos.try_push(Info {
                number,
                ..Info::default()
            })?;
        }
        Ok(Self {
            version,
            budget,
            overrides: BudgetVec::new(budget),
            infos,
            pages: BudgetVec::new(budget),
            visits: BudgetVec::new(budget),
            stamp: 0,
            added: 0,
            order: BudgetVec::new(budget),
            part4: 0,
            part6: 0,
            starts: BudgetVec::new(budget),
            part8: 0,
            part9: 0,
            outlines: None,
            encrypt: None,
        })
    }

    /// Adds `value` as a new object, and returns its number.
    fn add(&mut self, value: Value) -> Result<u32> {
        let number = self
            .version
            .next_number()
            .checked_add(u64::from(self.added))
            .and_then(|number| u32::try_from(number).ok())
            .ok_or_else(|| StoreError::malformed("no object number is left for the save"))?;
        self.added += 1;
        self.overrides.try_push((number, Arc::new(value)))?;
        self.infos.try_push(Info {
            number,
            ..Info::default()
        })?;
        Ok(number)
    }

    fn find(&self, number: u32) -> Option<usize> {
        self.infos
            .binary_search_by_key(&number, |info| info.number)
            .ok()
    }

    /// Returns the value that the save writes for object `number`.
    fn value(&self, number: u32) -> Result<Option<Arc<Value>>> {
        if let Ok(at) = self
            .overrides
            .binary_search_by_key(&number, |&(number, _)| number)
        {
            return Ok(Some(Arc::clone(&self.overrides[at].1)));
        }
        Ok(self
            .version
            .get(number, self.budget)
            .map_err(|error| error.context(format!("linearizing object {number}")))?
            .map(|object| Arc::clone(object.value())))
    }

    /// Finds the pages in order, and moves the attributes that the page
    /// tree's nodes hold into the pages that inherit them.
    fn index(&mut self, budget: &'a Budget) -> Result<()> {
        let index = PageIndex::build(&mut Reads::new(self.version, budget))?;
        if index.is_empty() {
            return Err(refused("a document without pages"));
        }
        let catalog = match self.version.trailer().get("Root") {
            Some(Value::Reference(root)) => self.value(root.number())?,
            _ => None,
        };
        let tree = match catalog.as_deref() {
            Some(Value::Dict(catalog)) => catalog.get("Pages"),
            _ => None,
        };
        let Some(Value::Reference(tree)) = tree else {
            return Err(refused("a page tree that is not an indirect object"));
        };
        let mut stack: BudgetVec<'_, (bool, u32, [Option<Value>; 4])> = BudgetVec::new(budget);
        stack.try_push((true, tree.number(), Default::default()))?;
        let mut pages = BudgetVec::new(budget);
        let mut overrides = BudgetVec::new(budget);
        while let Some((node, number, inherited)) = stack.pop() {
            budget.poll()?;
            let Some(value) = self.value(number)? else {
                continue;
            };
            let Value::Dict(dict) = &*value else {
                continue;
            };
            if !node {
                pages.try_push(number)?;
                let mut page = None::<Dict>;
                for (key, value) in INHERITABLE.iter().zip(&inherited) {
                    if let Some(value) = value
                        && dict.get(key).is_none()
                    {
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "a page takes at most four inherited attributes"
                        )]
                        page.get_or_insert_with(|| dict.clone())
                            .insert(Name::new(*key), value.clone());
                    }
                }
                if !is_type(dict, b"Page") {
                    #[expect(clippy::disallowed_methods, reason = "one /Type entry")]
                    page.get_or_insert_with(|| dict.clone()).insert(
                        Name::new(&b"Type"[..]),
                        Value::Name(Name::new(&b"Page"[..])),
                    );
                }
                if let Some(page) = page {
                    overrides.try_push((number, Arc::new(Value::Dict(page))))?;
                }
                continue;
            }
            let mut inherited = inherited;
            let mut own = None::<Dict>;
            for (key, slot) in INHERITABLE.iter().zip(inherited.iter_mut()) {
                let Some(value) = dict.get(key) else {
                    continue;
                };
                // A direct array or dictionary becomes one object, which
                // each page names, as qpdf moves it.
                *slot = Some(if matches!(value, Value::Array(_) | Value::Dict(_)) {
                    Value::Reference(Reference::new(self.add(value.clone())?, 0))
                } else {
                    value.clone()
                });
                own.get_or_insert_with(|| dict.clone()).remove(key);
            }
            if let Some(own) = own {
                overrides.try_push((number, Arc::new(Value::Dict(own))))?;
            }
            let kids = match dict.get("Kids") {
                Some(Value::Reference(kids)) => self.value(kids.number())?,
                Some(kids) => Some(Arc::new(kids.clone())),
                None => None,
            };
            let Some(Value::Array(kids)) = kids.as_deref() else {
                continue;
            };
            for kid in kids.iter().rev() {
                budget.poll()?;
                let Value::Reference(kid) = kid else {
                    return Err(refused(
                        "a page or page-tree node written directly in /Kids",
                    ));
                };
                let node = match self.value(kid.number())?.as_deref() {
                    Some(Value::Dict(kid)) => is_type(kid, b"Pages"),
                    Some(Value::Reference(_)) => {
                        return Err(refused("a /Kids entry that names a reference"));
                    }
                    _ => continue,
                };
                stack.try_push((node, kid.number(), inherited.clone()))?;
            }
        }
        let same = pages.len() == index.len()
            && pages
                .iter()
                .zip(index.pages())
                .all(|(&number, page)| page.object().map(|o| o.number()) == Some(number));
        if !same {
            return Err(refused(
                "a page tree that its pages do not read as one tree",
            ));
        }
        self.overrides.try_extend(overrides.into_vec())?;
        self.overrides.sort_unstable_by_key(|&(number, _)| number);
        for &number in pages.iter() {
            let at = self
                .find(number)
                .ok_or_else(|| StoreError::malformed("a page has no object"))?;
            self.pages.try_push(at)?;
        }
        Ok(())
    }

    /// Walks the users of the objects, and gives each object its part.
    fn classify(&mut self, encrypt: Option<&Value>) -> Result<()> {
        let version = self.version;
        for page in 0..self.pages.len() {
            let number = self.infos[self.pages[page]].number;
            let user = User::Page(u32::try_from(page).map_err(|_| refused("too many pages"))?);
            self.walk(&Value::Reference(Reference::new(number, 0)), user, true)?;
        }
        for (name, value) in version.trailer().iter() {
            if super::trailer_key(name.as_bytes())
                && !matches!(name.as_bytes(), b"Root" | b"Encrypt" | b"ID")
            {
                self.walk(value, User::Other, false)?;
            }
        }
        if let Some(encrypt) = encrypt {
            self.walk(encrypt, User::Open, false)?;
        }
        let root = match version.trailer().get("Root") {
            Some(Value::Reference(root)) => root.number(),
            _ => return Err(refused("a trailer without a catalog")),
        };
        let catalog = self.value(root)?;
        let Some(Value::Dict(catalog)) = catalog.as_deref() else {
            return Err(refused("a trailer without a catalog"));
        };
        for (name, value) in catalog.iter() {
            let user = match name.as_bytes() {
                b"Pages" => User::Tree,
                b"Outlines" => User::Outlines,
                key if OPEN.contains(&key) => User::Open,
                _ => User::Other,
            };
            self.walk(value, user, false)?;
        }
        let at = self
            .find(root)
            .ok_or_else(|| refused("a trailer without a catalog"))?;
        self.infos[at].root = true;
        self.infos[at].users += 1;
        for info in self.infos.iter_mut() {
            info.class = class(info);
        }
        Ok(())
    }

    /// Walks the objects that `start` reaches for `user`, as qpdf does:
    /// depth first, the entries of a dictionary in reverse order of their
    /// keys, never into a page but the first one, `top`, nor up from it by
    /// `/Parent`. Its `/Thumb` belongs to the page's thumbnail.
    fn walk(&mut self, start: &Value, user: User, top: bool) -> Result<()> {
        self.stamp += 1;
        let budget = self.budget;
        let mut pending: BudgetVec<'_, (u32, User, bool)> = BudgetVec::new(budget);
        let mut found = BudgetVec::new(budget);
        match start {
            Value::Reference(reference) => pending.try_push((reference.number(), user, top))?,
            value => {
                direct(value, user, false, &mut found, budget)?;
                pending.try_extend(found.iter().rev().map(|&(n, u)| (n, u, false)))?;
                found.clear();
            }
        }
        while let Some((number, user, top)) = pending.pop() {
            budget.poll()?;
            let Some(at) = self.find(number) else {
                continue;
            };
            let Some(value) = self.value(number)? else {
                continue;
            };
            let page = matches!(&*value, Value::Dict(dict) if is_type(dict, b"Page"));
            if (page && !top) || self.infos[at].stamp == self.stamp {
                continue;
            }
            let info = &mut self.infos[at];
            info.stamp = self.stamp;
            info.users += 1;
            match user {
                User::Page(0) => info.first = true,
                User::Page(page) => {
                    info.pages += 1;
                    info.page = page;
                    self.visits.try_push((page, at))?;
                }
                User::Thumb(page) => {
                    info.thumbs += 1;
                    info.thumb = page;
                }
                User::Open => info.open = true,
                User::Outlines => info.outlines = true,
                User::Tree => {
                    info.tree = true;
                    info.others += 1;
                }
                User::Other => info.others += 1,
            }
            direct(&value, user, page, &mut found, budget)?;
            pending.try_extend(found.iter().rev().map(|&(n, u)| (n, u, false)))?;
            found.clear();
        }
        Ok(())
    }

    /// Puts the objects in file order, and numbers them.
    fn arrange(&mut self) -> Result<()> {
        let budget = self.budget;
        let mut order = BudgetVec::new(budget);
        let root = self
            .infos
            .iter()
            .position(|info| info.class == Class::Root)
            .ok_or_else(|| refused("a trailer without a catalog"))?;
        order.try_push(root)?;
        self.push(&mut order, |_, info| info.class == Class::Open)?;
        self.part4 = order.len();
        let first = self.pages[0];
        if self.infos[first].class != Class::FirstPrivate {
            return Err(refused("a first page that other users reach"));
        }
        order.try_push(first)?;
        self.push(&mut order, |at, info| {
            info.class == Class::FirstPrivate && at != first
        })?;
        self.push(&mut order, |_, info| info.class == Class::FirstShared)?;
        let catalog = match self.value(self.infos[root].number)?.as_deref() {
            Some(Value::Dict(catalog)) => Some(catalog.clone()),
            _ => None,
        };
        let outlines = catalog
            .as_ref()
            .and_then(|catalog| match catalog.get("Outlines") {
                Some(Value::Reference(outlines)) => self.find(outlines.number()),
                _ => None,
            });
        let in_first = catalog.as_ref().is_some_and(outlines_first);
        if in_first {
            self.outlines(&mut order, outlines)?;
        }
        self.part6 = order.len();
        let mut private = BudgetVec::new(budget);
        for (at, info) in self.infos.iter().enumerate() {
            if info.class == Class::OtherPrivate {
                private.try_push((info.page, at))?;
            }
        }
        private.sort_unstable();
        let mut next = 0;
        for page in 1..self.pages.len() {
            let own = self.pages[page];
            if self.infos[own].class != Class::OtherPrivate {
                return Err(refused("a page that other users reach"));
            }
            self.starts.try_push(order.len())?;
            order.try_push(own)?;
            while let Some(&(user, at)) = private.get(next) {
                if user as usize > page {
                    break;
                }
                next += 1;
                if at != own {
                    order.try_push(at)?;
                }
            }
        }
        self.part8 = order.len();
        self.starts.try_push(self.part8)?;
        self.push(&mut order, |_, info| info.class == Class::OtherShared)?;
        self.part9 = order.len();
        self.push(&mut order, |_, info| {
            info.class == Class::Other && info.tree
        })?;
        let mut thumbs = BudgetVec::new(budget);
        for (at, info) in self.infos.iter().enumerate() {
            if info.class == Class::ThumbPrivate {
                thumbs.try_push((info.thumb, at))?;
            }
        }
        thumbs.sort_unstable();
        let mut next = 0;
        for page in 0..self.pages.len() {
            let thumb = match self.value(self.infos[self.pages[page]].number)?.as_deref() {
                Some(Value::Dict(dict)) => match dict.get("Thumb") {
                    Some(Value::Reference(thumb)) => self.find(thumb.number()),
                    _ => None,
                },
                _ => None,
            };
            let start = next;
            while thumbs
                .get(next)
                .is_some_and(|&(user, _)| user as usize == page)
            {
                next += 1;
            }
            let group = &thumbs[start..next];
            if let Some(thumb) = thumb
                && group.iter().any(|&(_, at)| at == thumb)
            {
                order.try_push(thumb)?;
            }
            for &(_, at) in group {
                if Some(at) != thumb {
                    order.try_push(at)?;
                }
            }
        }
        self.push(&mut order, |_, info| info.class == Class::ThumbShared)?;
        if !in_first {
            self.outlines(&mut order, outlines)?;
        }
        self.push(&mut order, |_, info| {
            info.class == Class::Other && !info.tree
        })?;
        let used = self
            .infos
            .iter()
            .filter(|info| info.class != Class::Unused)
            .count();
        if used != order.len() {
            return Err(StoreError::malformed("a linearized save placed an object twice").into());
        }
        // The second half takes the numbers from 1, then the
        // linearization dictionary, the first half, and the hint stream.
        let mut number = 1u32;
        for (position, &at) in order.iter().enumerate() {
            self.infos[at].at = u32::try_from(position).map_err(|_| refused("too many objects"))?;
        }
        for &at in &order[self.part6..] {
            self.infos[at].new = number;
            number += 1;
        }
        number += 1;
        for (position, &at) in order[..self.part6].iter().enumerate() {
            if position == self.part4 {
                number += 1;
            }
            self.infos[at].new = number;
            number += 1;
        }
        self.order = order;
        Ok(())
    }

    /// Appends the indices of the objects that `keep` keeps, ascending by
    /// number.
    fn push(
        &self,
        order: &mut BudgetVec<'_, usize>,
        keep: impl Fn(usize, &Info) -> bool,
    ) -> Result<()> {
        for (at, info) in self.infos.iter().enumerate() {
            self.budget.poll()?;
            if keep(at, info) {
                order.try_push(at)?;
            }
        }
        Ok(())
    }

    /// Appends the outlines: their dictionary, then the objects that only
    /// they use.
    fn outlines(
        &mut self,
        order: &mut BudgetVec<'_, usize>,
        outlines: Option<usize>,
    ) -> Result<()> {
        let Some(outlines) = outlines.filter(|&at| self.infos[at].class == Class::Outlines) else {
            return Ok(());
        };
        let start = order.len();
        order.try_push(outlines)?;
        self.push(order, |at, info| {
            info.class == Class::Outlines && at != outlines
        })?;
        self.outlines = Some((start, order.len()));
        Ok(())
    }

    /// The number of the linearization dictionary.
    fn linearization(&self) -> u32 {
        (self.order.len() - self.part6) as u32 + 1
    }

    /// The number of the hint stream.
    fn hint(&self) -> u32 {
        self.linearization() + 1 + self.part4 as u32
    }

    /// Renumbers each reference inside `value`, and replaces one to an
    /// object that the save does not write with null.
    fn rewrite(&self, value: &mut Value) -> Result<()> {
        let mut stack = BudgetVec::new(self.budget);
        stack.try_push(value)?;
        while let Some(value) = stack.pop() {
            match value {
                Value::Reference(reference) => {
                    *value = match self.find(reference.number()) {
                        Some(at) if self.infos[at].class != Class::Unused => {
                            Value::Reference(Reference::new(self.infos[at].new, 0))
                        }
                        _ => Value::Null,
                    };
                }
                Value::Array(array) => stack.try_extend(array.iter_mut())?,
                Value::Dict(dict) => stack.try_extend(dict.iter_mut().map(|(_, item)| item))?,
                Value::Stream(stream) => {
                    stack.try_extend(stream.dict_mut().iter_mut().map(|(_, item)| item))?;
                }
                _ => {}
            }
        }
        Ok(())
    }

    /// Returns a copy of `value` with its references renumbered, read again
    /// from its bytes so that the copy grows through the budget.
    fn copy(&self, value: &Value) -> Result<Value> {
        let mut bytes = BudgetVec::new(self.budget);
        write::value(&mut bytes, value)?;
        let mut value = Value::parse(&Bytes::new(bytes.into_vec()), self.budget)?;
        self.rewrite(&mut value)?;
        Ok(value)
    }
}

/// Collects the references inside the direct structure of `value`, in the
/// order that qpdf's walk reaches them: the children of a container go on
/// a stack in order, a dictionary's sorted by key.
fn direct(
    value: &Value,
    user: User,
    page: bool,
    found: &mut BudgetVec<'_, (u32, User)>,
    budget: &Budget,
) -> Result<()> {
    let mut stack: BudgetVec<'_, (&Value, User)> = BudgetVec::new(budget);
    children(value, user, page, &mut stack, budget)?;
    while let Some((value, user)) = stack.pop() {
        match value {
            Value::Reference(reference) => found.try_push((reference.number(), user))?,
            Value::Dict(dict) if is_type(dict, b"Page") => {}
            value => children(value, user, false, &mut stack, budget)?,
        }
    }
    Ok(())
}

fn children<'v>(
    value: &'v Value,
    user: User,
    page: bool,
    stack: &mut BudgetVec<'_, (&'v Value, User)>,
    budget: &Budget,
) -> Result<()> {
    let (dict, stream) = match value {
        Value::Array(array) => return Ok(stack.try_extend(array.iter().map(|item| (item, user)))?),
        Value::Dict(dict) => (dict, false),
        Value::Stream(stream) => (stream.dict(), true),
        _ => return Ok(()),
    };
    let mut entries = BudgetVec::new(budget);
    // The writer writes a stream's `/Length` directly, so an object that
    // only a length names is not written.
    entries.try_extend(
        dict.iter()
            .filter(|(name, _)| !(stream && name.as_bytes() == b"Length")),
    )?;
    entries.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    for (name, item) in entries.iter() {
        match (page, name.as_bytes(), user) {
            (true, b"Parent", _) => {}
            (true, b"Thumb", User::Page(index)) => stack.try_push((*item, User::Thumb(index)))?,
            _ => stack.try_push((*item, user))?,
        }
    }
    Ok(())
}

fn is_type(dict: &Dict, kind: &[u8]) -> bool {
    matches!(dict.get("Type"), Some(Value::Name(name)) if name.as_bytes() == kind)
}

/// Returns whether the outlines go with the first page: the document
/// opens showing them.
fn outlines_first(catalog: &Dict) -> bool {
    matches!(catalog.get("PageMode"), Some(Value::Name(mode)) if mode.as_bytes() == b"UseOutlines")
        && catalog.get("Outlines").is_some()
}

/// Returns the part of an object, as qpdf's linearization chooses it.
fn class(info: &Info) -> Class {
    if info.users == 0 {
        Class::Unused
    } else if info.root {
        Class::Root
    } else if info.outlines {
        Class::Outlines
    } else if info.open {
        Class::Open
    } else if info.first && info.others == 0 && info.pages == 0 && info.thumbs == 0 {
        Class::FirstPrivate
    } else if info.first {
        Class::FirstShared
    } else if info.pages == 1 && info.others == 0 && info.thumbs == 0 {
        Class::OtherPrivate
    } else if info.pages > 1 {
        Class::OtherShared
    } else if info.thumbs == 1 && info.others == 0 {
        Class::ThumbPrivate
    } else if info.thumbs > 1 {
        Class::ThumbShared
    } else {
        Class::Other
    }
}

/// Returns the error of a document that a linearized save cannot write.
fn refused(what: &str) -> Error {
    StoreError::malformed(format!("cannot linearize {what}")).into()
}

/// Writes the file of a plan.
struct Writer<'a> {
    plan: &'a Plan<'a>,
    mode: &'a Mode<'a>,
    streams: Streams,
    encrypt: Option<&'a Value>,
    id: &'a Id,
}

impl Writer<'_> {
    /// Writes the file to `out`, and the position of each object in file
    /// order, then of the main cross-reference section, to `positions`.
    /// The first pass has no `hint` and no `fields`; the second has the
    /// hint stream and the first pass's positions, and checks that each
    /// object lands where the first pass, shifted by the hint stream, put
    /// it.
    fn emit(
        &self,
        out: &mut Output<'_>,
        hint: Option<(&[u8], &[u64])>,
        positions: &mut BudgetVec<'_, u64>,
        fields: Option<Fields>,
        digest: Option<Value>,
    ) -> Result<Fields> {
        let plan = self.plan;
        let fields = fields.unwrap_or_default();
        let header = plan
            .version
            .base()
            .file()
            .and_then(|file| file.bytes().get(file.origin()..file.origin() + 8))
            .filter(|bytes| bytes.starts_with(b"%PDF-"))
            .unwrap_or(b"%PDF-1.7");
        out.bytes(header)?;
        out.bytes(b"\n%\xE2\xE3\xCF\xD3\n")?;
        let linearization = plan.linearization();
        let first_page = plan.infos[plan.pages[0]].new;
        let linearization_offset = out.offset()?;
        out.display(format_args!(
            "{linearization} 0 obj\n<< /Linearized 1 /L {:10} /H [ {:10} {:10} ] /O {first_page} /E {:10} /N {} /T {:10} >>\nendobj\n",
            fields.length,
            fields.hint_offset,
            fields.hint_length,
            fields.end,
            plan.pages.len(),
            fields.zero,
        ))?;
        // The first-page section lists the linearization dictionary, part
        // 4, the hint stream, and part 6.
        let first_xref = out.offset()?;
        let at = |position: usize| -> u64 {
            hint.map_or(0, |(bytes, positions)| {
                let offset = positions[position];
                if position >= plan.part4 {
                    offset + bytes.len() as u64
                } else {
                    offset
                }
            })
        };
        let mut rows = BudgetVec::new(plan.budget);
        let row = |number: u32, offset: u64| Row {
            number,
            generation: 0,
            offset: Some(offset),
            next_free: 0,
            member: None,
        };
        rows.try_push(row(linearization, linearization_offset))?;
        for position in 0..plan.part6 {
            if position == plan.part4 {
                rows.try_push(row(plan.hint(), fields.hint_offset))?;
            }
            rows.try_push(row(plan.infos[plan.order[position]].new, at(position)))?;
        }
        table(&rows, out)?;
        out.bytes(b"trailer\n<<")?;
        self.trailer(out)?;
        self.identifier(out, digest.as_ref())?;
        out.display(format_args!(
            " /Size {} /Prev {:10} >>\nstartxref\n0\n%%EOF\n",
            plan.hint() + plan.part6 as u32 - plan.part4 as u32 + 1,
            fields.main,
        ))?;
        let mut result = Fields::default();
        for (position, &index) in plan.order.iter().enumerate() {
            if position == plan.part4 {
                result.hint_offset = out.offset()?;
                if let Some((bytes, _)) = hint {
                    out.bytes(bytes)?;
                }
            }
            if position == plan.part6 {
                result.end = out.offset()?;
            }
            let offset = out.offset()?;
            if hint.is_some() && offset != at(position) {
                return Err(changed());
            }
            positions.try_push(offset)?;
            let bytes = self.object(index)?;
            out.bytes(&bytes)?;
        }
        if plan.order.len() == plan.part6 {
            result.end = out.offset()?;
        }
        result.main = out.offset()?;
        positions.try_push(result.main)?;
        let mut rows = BudgetVec::new(plan.budget);
        rows.try_push(Row {
            number: 0,
            generation: u16::MAX,
            offset: None,
            next_free: 0,
            member: None,
        })?;
        for position in plan.part6..plan.order.len() {
            let number = plan.infos[plan.order[position]].new;
            rows.try_push(row(number, positions[position]))?;
        }
        let count = rows.len();
        result.zero = result.main + format!("xref\n0 {count}").len() as u64;
        table(&rows, out)?;
        out.display(format_args!("trailer\n<< /Size {count}"))?;
        self.identifier(out, digest.as_ref())?;
        out.display(format_args!(" >>\nstartxref\n{first_xref}\n%%EOF\n"))?;
        result.length = out.offset()?;
        Ok(result)
    }

    /// Writes the trailer's entries, renumbered, and `/Encrypt`.
    fn trailer(&self, out: &mut Output<'_>) -> Result<()> {
        for (name, value) in self.plan.version.trailer().iter() {
            if !super::trailer_key(name.as_bytes()) || matches!(name.as_bytes(), b"ID" | b"Encrypt")
            {
                continue;
            }
            self.entry(out, name, value)?;
        }
        if let Some(encrypt) = self.encrypt {
            self.entry(out, &Name::new(&b"Encrypt"[..]), encrypt)?;
        }
        Ok(())
    }

    fn entry(&self, out: &mut Output<'_>, name: &Name, value: &Value) -> Result<()> {
        out.budget.poll()?;
        let mut bytes = BudgetVec::new(out.budget);
        write::value(&mut bytes, &Value::Name(name.clone()))?;
        bytes.try_push(b' ')?;
        write::value(&mut bytes, &self.plan.copy(value)?)?;
        out.bytes(b" ")?;
        out.bytes(&bytes)
    }

    /// Writes `/ID`, with `digest` as the second element where the save
    /// renews it, and a placeholder of its width in the first pass.
    fn identifier(&self, out: &mut Output<'_>, digest: Option<&Value>) -> Result<()> {
        let placeholder = Value::String(PdfString::hexadecimal([0u8; 16]));
        let digest = digest.cloned().unwrap_or(placeholder);
        match self.id {
            Id::None => Ok(()),
            Id::Keep(id) => {
                let mut bytes = BudgetVec::new(out.budget);
                write::value(&mut bytes, id)?;
                out.bytes(b" /ID ")?;
                out.bytes(&bytes)
            }
            Id::Renew(first) => {
                identifier(out, first.clone().unwrap_or_else(|| digest.clone()), digest)
            }
            Id::Fixed(first) => identifier(out, first.clone(), first.clone()),
        }
    }

    /// Returns the bytes of the object at `index` of the plan's entries,
    /// renumbered, recoded, and encrypted.
    fn object(&self, index: usize) -> Result<BudgetVec<'_, u8>> {
        let plan = self.plan;
        let budget = plan.budget;
        let info = &plan.infos[index];
        let context = |error: Error| error.context(format!("linearizing object {}", info.number));
        let value = plan
            .value(info.number)?
            .ok_or_else(|| StoreError::malformed("a written object has no value"))?;
        let mut value = plan.copy(&value).map_err(context)?;
        if self.streams != Streams::Keep
            && let Some(recoded) = recode::recoded(&value, self.streams, budget).map_err(context)?
        {
            let mut before = BudgetVec::new(budget);
            write::value(&mut before, &value)?;
            let mut after = BudgetVec::new(budget);
            write::value(&mut after, &recoded)?;
            if self.streams == Streams::Decompress || after.len() < before.len() {
                value = recoded;
            }
        }
        let reference = Reference::new(info.new, 0);
        if let Some(handler) = self.mode.handler()
            && plan.encrypt != Some(index)
        {
            value = security::encrypt_value(handler, reference, &value, budget)
                .map_err(|e| context(e.into()))?;
        }
        let mut bytes = BudgetVec::new(budget);
        object(&mut bytes, reference.number(), &value)?;
        Ok(bytes)
    }

    /// Returns the hint stream object of the first pass's `positions`.
    fn hint(&self, positions: &[u64]) -> Result<BudgetVec<'_, u8>> {
        let plan = self.plan;
        let budget = plan.budget;
        let wide = |value: u64| {
            u32::try_from(value)
                .map(u64::from)
                .map_err(|_| refused("a file too large for hint tables"))
        };
        let length = |start: usize, end: usize| positions[end] - positions[start];
        let pages = plan.pages.len();
        // Each page's objects and length; the first page's are part 6.
        let mut entries: BudgetVec<'_, (u64, u64, u64)> = BudgetVec::new(budget);
        entries.try_push((
            (plan.part6 - plan.part4) as u64,
            wide(length(plan.part4, plan.part6))?,
            0,
        ))?;
        for page in 1..pages {
            let start = plan.starts[page - 1];
            let end = plan.starts[page];
            entries.try_push(((end - start) as u64, wide(length(start, end))?, 0))?;
        }
        let shared_first = plan.part6 - plan.part4;
        let shared_total = shared_first + plan.part9 - plan.part8;
        let shared = |at: usize| -> Option<u64> {
            let position = plan.infos[at].at as usize;
            if (plan.part4..plan.part6).contains(&position) {
                Some((position - plan.part4) as u64)
            } else if (plan.part8..plan.part9).contains(&position) {
                Some((shared_first + position - plan.part8) as u64)
            } else {
                None
            }
        };
        let mut identifiers = BudgetVec::new(budget);
        for &(page, at) in plan.visits.iter() {
            if plan.infos[at].users > 1
                && let Some(identifier) = shared(at)
            {
                identifiers.try_push((page, identifier))?;
                entries[page as usize].2 += 1;
            }
        }
        identifiers.sort_unstable();
        let min_objects = entries.iter().map(|e| e.0).min().unwrap_or(0);
        let max_objects = entries.iter().map(|e| e.0).max().unwrap_or(0);
        let min_length = entries.iter().map(|e| e.1).min().unwrap_or(0);
        let max_length = entries.iter().map(|e| e.1).max().unwrap_or(0);
        let max_shared = entries.iter().map(|e| e.2).max().unwrap_or(0);
        let objects_bits = bits(max_objects - min_objects);
        let length_bits = bits(max_length - min_length);
        let shared_bits = bits(max_shared);
        let identifier_bits = bits(shared_total as u64);
        let mut out = Bits::new(budget);
        out.put(min_objects, 32)?;
        out.put(wide(positions[plan.part4])?, 32)?;
        out.put(objects_bits, 16)?;
        out.put(min_length, 32)?;
        out.put(length_bits, 16)?;
        out.put(0, 32)?;
        out.put(0, 16)?;
        out.put(min_length, 32)?;
        out.put(length_bits, 16)?;
        out.put(shared_bits, 16)?;
        out.put(identifier_bits, 16)?;
        out.put(0, 16)?;
        out.put(4, 16)?;
        for entry in entries.iter() {
            out.put(entry.0 - min_objects, objects_bits)?;
        }
        out.flush()?;
        for entry in entries.iter() {
            out.put(entry.1 - min_length, length_bits)?;
        }
        out.flush()?;
        for entry in entries.iter() {
            out.put(entry.2, shared_bits)?;
        }
        out.flush()?;
        for &(_, identifier) in identifiers.iter() {
            out.put(identifier, identifier_bits)?;
        }
        out.flush()?;
        // The numerators and content offsets take no bits.
        for entry in entries.iter() {
            out.put(entry.1 - min_length, length_bits)?;
        }
        out.flush()?;
        let shared_offset = out.bytes.len();
        let mut lengths = BudgetVec::new(budget);
        for position in (plan.part4..plan.part6).chain(plan.part8..plan.part9) {
            lengths.try_push(wide(length(position, position + 1))?)?;
        }
        let min_group = lengths.iter().copied().min().unwrap_or(0);
        let max_group = lengths.iter().copied().max().unwrap_or(0);
        let group_bits = bits(max_group - min_group);
        let (first_shared, first_offset) = if plan.part8 < plan.part9 {
            (
                u64::from(plan.infos[plan.order[plan.part8]].new),
                wide(positions[plan.part8])?,
            )
        } else {
            (0, 0)
        };
        out.put(first_shared, 32)?;
        out.put(first_offset, 32)?;
        out.put(shared_first as u64, 32)?;
        out.put(shared_total as u64, 32)?;
        out.put(0, 16)?;
        out.put(min_group, 32)?;
        out.put(group_bits, 16)?;
        for &length in lengths.iter() {
            out.put(length - min_group, group_bits)?;
        }
        out.flush()?;
        for _ in lengths.iter() {
            out.put(0, 1)?;
        }
        out.flush()?;
        let outlines_offset = out.bytes.len();
        if let Some((start, end)) = plan.outlines {
            out.put(u64::from(plan.infos[plan.order[start]].new), 32)?;
            out.put(wide(positions[start])?, 32)?;
            out.put((end - start) as u64, 32)?;
            out.put(wide(length(start, end))?, 32)?;
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "a hint stream has two or three entries"
        )]
        let dict: Dict = [
            (Name::new(&b"S"[..]), Value::Integer(shared_offset as i64)),
            (Name::new(&b"O"[..]), Value::Integer(outlines_offset as i64)),
        ]
        .into_iter()
        .take(if plan.outlines.is_some() { 2 } else { 1 })
        .collect();
        let mut value = Value::Stream(Box::new(Stream::new(
            dict,
            Bytes::new(out.bytes.into_vec()),
        )));
        let number = plan.hint();
        if let Some(handler) = self.mode.handler() {
            value = security::encrypt_value(handler, Reference::new(number, 0), &value, budget)?;
        }
        let mut bytes = BudgetVec::new(budget);
        object(&mut bytes, number, &value)?;
        Ok(bytes)
    }
}

/// Writes the object `number` with `value` to `out`.
fn object(out: &mut BudgetVec<'_, u8>, number: u32, value: &Value) -> Result<()> {
    out.try_extend_from_slice(fmt::format(format_args!("{number} 0 obj\n")).as_bytes())?;
    write::value(out, value)?;
    out.try_extend_from_slice(b"\nendobj\n")?;
    Ok(())
}

/// Returns the number of bits that `value` needs.
const fn bits(value: u64) -> u64 {
    (u64::BITS - value.leading_zeros()) as u64
}

/// A writer of the big-endian bit fields of hint tables.
struct Bits<'a> {
    bytes: BudgetVec<'a, u8>,
    byte: u8,
    used: u32,
}

impl<'a> Bits<'a> {
    const fn new(budget: &'a Budget) -> Self {
        Self {
            bytes: BudgetVec::new(budget),
            byte: 0,
            used: 0,
        }
    }

    /// Writes the low `count` bits of `value`, most significant first.
    fn put(&mut self, value: u64, count: u64) -> Result<()> {
        for bit in (0..count).rev() {
            self.byte = (self.byte << 1) | u8::from((value >> bit) & 1 == 1);
            self.used += 1;
            if self.used == 8 {
                self.bytes.try_push(self.byte)?;
                self.byte = 0;
                self.used = 0;
            }
        }
        Ok(())
    }

    /// Pads the last byte with zero bits.
    fn flush(&mut self) -> Result<()> {
        if self.used > 0 {
            self.put(0, u64::from(8 - self.used))?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bits_count_the_bits_of_a_value() {
        assert_eq!(bits(0), 0);
        assert_eq!(bits(1), 1);
        assert_eq!(bits(6), 3);
        assert_eq!(bits(u64::from(u32::MAX)), 32);
    }

    #[test]
    fn the_first_object_is_found_in_the_header_bytes() {
        let found = first_object(b"%PDF-1.7\n%\xE2\xE3\xCF\xD3\n12 0 obj\n<<");
        assert_eq!(found, Some(Reference::new(12, 0)));
        assert_eq!(first_object(b"%PDF-1.7\n"), None);
    }
}
