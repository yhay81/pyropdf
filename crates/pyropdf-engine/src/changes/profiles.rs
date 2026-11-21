//! Profile effects that depend on a catalog relationship, changed page content,
//! or the previous value of a written object.

use super::resources::{Device, FontUse, Use, missing_appearance_font};
use super::*;
use crate::content::PageContent;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::{Page, PageIndex};

pub(super) fn named(dict: &Dict, key: &str, expected: &[u8]) -> bool {
    matches!(dict.get(key), Some(Value::Name(name)) if name.as_bytes() == expected)
}

pub(super) fn entry<'a>(
    version: &Version,
    dict: &'a Dict,
    key: &str,
    budget: &Budget,
) -> Result<Option<Resolved<'a>>> {
    match dict.get(key) {
        Some(value) => resolved(version, value, budget),
        None => Ok(None),
    }
}

fn widget_action(claim: Claim, dict: &Dict) -> bool {
    named(dict, "Subtype", b"Widget")
        && (dict.get("A").is_some()
            || matches!(claim, Claim::A(2 | 3, _)) && dict.get("AA").is_some())
}

fn internal_destination(version: &Version, dict: &Dict, budget: &Budget) -> Result<bool> {
    let dest = if named(dict, "Type", b"Catalog") {
        entry(version, dict, "OpenAction", budget)?
    } else if dict.get("Title").is_some() && dict.get("Parent").is_some()
        || is_annotation_dict(dict)
    {
        if dict.get("SD").is_some() {
            return Ok(false);
        }
        entry(version, dict, "Dest", budget)?
    } else if named(dict, "S", b"GoTo") && dict.get("SD").is_none() {
        entry(version, dict, "D", budget)?
    } else {
        None
    };
    let Some(Value::Array(items)) = dest.as_deref() else {
        return Ok(false);
    };
    let Some(target) = items.get(0) else {
        return Ok(false);
    };
    let target = resolved(version, target, budget)?;
    Ok(target
        .as_deref()
        .and_then(value_dict)
        .is_some_and(|dict| named(dict, "Type", b"Page")))
}

fn archival_annotation_flags(
    version: &Version,
    dict: &Dict,
    budget: &Budget,
) -> Result<Option<&'static str>> {
    if !is_annotation_dict(dict) {
        return Ok(None);
    }
    let value = entry(version, dict, "F", budget)?;
    let flags = match value.as_deref() {
        None | Some(Value::Null) => {
            return Ok(
                (!named(dict, "Subtype", b"Popup")).then_some("PDF/A-4 requires annotation flags")
            );
        }
        Some(Value::Integer(flags)) => *flags,
        // veraPDF interprets a numeric F as an integer for the profile's bit test.
        // The separate structural check verifies that F has the PDF integer type.
        #[expect(
            clippy::cast_possible_truncation,
            reason = "testing the integer portion of numeric flag bits"
        )]
        Some(Value::Real(real)) => real.value() as i64,
        _ => 0,
    };
    // ISO 19005-4, 6.3.2: Print set; Invisible, Hidden, NoView and ToggleNoView clear.
    Ok((flags & 0x127 != 4)
        .then_some("the annotation flags disable printing or hide the annotation"))
}

fn local_rules(
    claim: Claim,
    version: &Version,
    dict: &Dict,
    budget: &Budget,
) -> Result<[Option<&'static str>; 9]> {
    let mut reasons = [None; 9];
    match claim {
        Claim::A(part, level) => {
            reasons[0] = widget_action(claim, dict)
                .then_some("the widget has an action forbidden by the archival profile");
            reasons[1] =
                (part > 1 && named(dict, "Type", b"Page") && dict.get("PresSteps").is_some())
                    .then_some("the page contains forbidden presentation steps");
            if is_annotation_dict(dict)
                && let Some(appearance) = entry(version, dict, "AP", budget)?
                && let Value::Dict(modes) = &*appearance
                && modes.keys().any(|name| name.as_bytes() != b"N")
            {
                reasons[8] = Some("PDF/A permits only the normal annotation appearance");
            }
            if part == 1 {
                reasons[2] = (named(dict, "Type", b"Catalog")
                    && dict.get("OCProperties").is_some())
                .then_some("PDF/A-1 forbids optional content properties");
                reasons[3] = (named(dict, "Subtype", b"Widget") && dict.get("AP").is_none())
                    .then_some("PDF/A-1 requires a widget appearance dictionary");
                reasons[4] = (is_annotation_dict(dict)
                    && !named(dict, "Subtype", b"Popup")
                    && matches!(dict.get("F"), Some(Value::Integer(flags)) if flags & 39 != 4))
                .then_some("the annotation flags disable printing or hide the annotation");
            } else if named(dict, "Subtype", b"Screen") && is_annotation_dict(dict) {
                reasons[2] = Some("the archival profile forbids Screen annotations");
            } else if part == 4 && level != b'F' {
                reasons[2] = (is_annotation_dict(dict)
                    && named(dict, "Subtype", b"FileAttachment"))
                .then_some("this PDF/A-4 flavour forbids FileAttachment annotations");
            }
            if part == 4 {
                reasons[5] = archival_annotation_flags(version, dict, budget)?;
                if named(dict, "Type", b"Filespec") && dict.get("EF").is_some() {
                    reasons[6] = (!matches!(
                        entry(version, dict, "F", budget)?.as_deref(),
                        Some(Value::String(_))
                    ) || !matches!(
                        entry(version, dict, "UF", budget)?.as_deref(),
                        Some(Value::String(_))
                    ))
                    .then_some("the embedded file specification lacks a required filename");
                    reasons[7] = (!matches!(
                        entry(version, dict, "AFRelationship", budget)?.as_deref(),
                        Some(Value::Name(_))
                    ))
                    .then_some("the embedded file specification lacks its AFRelationship");
                }
            }
        }
        Claim::Ua(2) => {
            reasons[0] = internal_destination(version, dict, budget)?.then_some(
                "the internal destination refers to a page without a structure destination",
            );
            reasons[1] = (named(dict, "Type", b"StructTreeRoot")
                && matches!(dict.get("K"), Some(Value::Array(kids)) if kids.is_empty()))
            .then_some("the structure tree root lost its required Document child");
        }
        _ => {}
    }
    Ok(reasons)
}

pub(super) fn object_changes<'b>(
    claim: Claim,
    (opened, version): (&Version, &Version),
    number: u32,
    previous: Option<&Value>,
    value: &Value,
    found: &mut Reasons<'b>,
    budget: &'b Budget,
) -> Result<()> {
    let Some(dict) = value_dict(value) else {
        return Ok(());
    };
    let old = previous.and_then(value_dict);
    let before = old
        .map(|d| local_rules(claim, opened, d, budget))
        .transpose()?
        .unwrap_or([None; 9]);
    for reason in local_rules(claim, version, dict, budget)?
        .into_iter()
        .flatten()
        .filter(|r| !before.contains(&Some(*r)))
    {
        record(found, reason, Some(number), budget)?;
    }
    let Some(old) = old else {
        return Ok(());
    };
    if matches!(claim, Claim::Ua(_))
        && named(dict, "S", b"Figure")
        && (old.get("Alt").is_some() || old.get("ActualText").is_some())
        && dict.get("Alt").is_none()
        && dict.get("ActualText").is_none()
    {
        record(
            found,
            "the Figure lost its alternate description",
            Some(number),
            budget,
        )?;
    }
    if named(dict, "Type", b"FontDescriptor")
        && ["FontFile", "FontFile2", "FontFile3"]
            .iter()
            .any(|k| old.get(k).is_some())
        && ["FontFile", "FontFile2", "FontFile3"]
            .iter()
            .all(|k| dict.get(k).is_none())
        && Use::Descriptor(number).in_document(version, budget)?
    {
        record(
            found,
            "the font descriptor lost its embedded font program",
            Some(number),
            budget,
        )?;
    }
    // A Type 3 font embeds a procedure per character code. Only a
    // removed procedure selected by a shown glyph affects the claim.
    let (old_procs, procs) = if named(dict, "Subtype", b"Type3") {
        (
            old.get("CharProcs").and_then(value_dict),
            dict.get("CharProcs").and_then(value_dict),
        )
    } else {
        (Some(old), Some(dict))
    };
    if let Some(old_procs) = old_procs {
        for (name, previous) in old_procs.iter() {
            if procs.is_some_and(|d| d.get(name.as_bytes()) == Some(previous)) {
                continue;
            }
            if matches!(previous, Value::Reference(_))
                && matches!(
                    resolved(opened, previous, budget)?.as_deref(),
                    Some(Value::Stream(_))
                )
                && !matches!(
                    procs
                        .and_then(|d| d.get(name.as_bytes()))
                        .map(|v| resolved(version, v, budget))
                        .transpose()?
                        .flatten()
                        .as_deref(),
                    Some(Value::Stream(_))
                )
                && (Use::Procedure {
                    owner: number,
                    name: name.as_bytes(),
                })
                .in_document(version, budget)?
            {
                record(
                    found,
                    "the Type 3 font lost the procedure for a shown glyph",
                    Some(number),
                    budget,
                )?;
            }
        }
    }
    if claim == Claim::Ua(2)
        && named(dict, "Type", b"Catalog")
        && has_text_entry(opened, old, "Lang", budget)?
        && !has_text_entry(version, dict, "Lang", budget)?
    {
        record(
            found,
            "the catalog lost its required default language",
            Some(number),
            budget,
        )?;
    }
    Ok(())
}

// Follow only the specified catalog relationships and remember every indirect
// owner. Comparing the two versions excludes defects already present at open.
pub(super) struct PathReads<'a> {
    pub(super) version: &'a Version,
    pub(super) budget: &'a Budget,
    pub(super) owners: BudgetVec<'a, u32>,
    pub(super) font_owners: BudgetVec<'a, u32>,
    pub(super) appearance_fonts: BudgetVec<'a, FontUse>,
    pub(super) changed_programs: BudgetVec<'a, u32>,
}
impl<'a> PathReads<'a> {
    pub(super) fn new(version: &'a Version, budget: &'a Budget) -> Self {
        Self {
            version,
            budget,
            owners: BudgetVec::new(budget),
            font_owners: BudgetVec::new(budget),
            appearance_fonts: BudgetVec::new(budget),
            changed_programs: BudgetVec::new(budget),
        }
    }
    pub(super) fn resolve<'v>(&mut self, value: Option<&'v Value>) -> Result<Option<Resolved<'v>>> {
        let Some(value) = value else {
            return Ok(None);
        };
        if let Value::Reference(r) = value {
            self.owners.try_push(r.number())?;
            if self.version.generation(r.number()) != Some(r.generation()) {
                return Ok(None);
            }
        }
        resolved(self.version, value, self.budget)
    }
    /// PageIndex exposes resolved resources. Keep the owning page-tree path
    /// too, so a changed inherited resource can be named in the report.
    pub(super) fn resources<'p>(&mut self, page: &'p Page) -> Result<Option<Resolved<'p>>> {
        let mut next = reference_number(page.dict().get("Parent"));
        if let Some(r) = page.object() {
            self.owners.try_push(r.number())?;
        }
        if let Some(resources) = page.dict().get("Resources") {
            return self.resolve(Some(resources));
        }
        let mut depth = 0;
        while let Some(n) = next {
            depth += 1;
            if depth > MAX_DEPTH {
                return Err(LimitExceeded::over(
                    LimitCode::ObjectDepth,
                    MAX_DEPTH as u64,
                    depth as u64,
                )
                .into());
            }
            self.owners.try_push(n)?;
            let Some(parent) = object(self.version, n, self.budget)? else {
                break;
            };
            let Some(dict) = value_dict(&parent) else {
                break;
            };
            if let Some(resource) = dict.get("Resources") {
                if let Value::Reference(r) = resource {
                    self.owners.try_push(r.number())?;
                }
                break;
            }
            next = reference_number(dict.get("Parent"));
        }
        Ok(page.resources().map(Resolved::Borrowed))
    }
    fn root(&mut self) -> Result<Option<Resolved<'a>>> {
        self.resolve(self.version.trailer().get("Root"))
    }
}

type Rule = fn(&mut PathReads<'_>) -> Result<bool>;

/// Return the data colour space signature from the destination ICC header.
fn intent_space(reads: &mut PathReads<'_>, owner: Option<&Dict>) -> Result<Option<[u8; 4]>> {
    let intents = reads.resolve(owner.and_then(|dict| dict.get("OutputIntents")))?;
    let Some(Value::Array(items)) = intents.as_deref() else {
        return Ok(None);
    };
    for item in items.iter() {
        let item = reads.resolve(Some(item))?;
        if let Some(dict) = item.as_deref().and_then(value_dict)
            && named(dict, "S", b"GTS_PDFA1")
            && let Some(Value::Stream(profile)) =
                reads.resolve(dict.get("DestOutputProfile"))?.as_deref()
        {
            let data = match profile.decoded(reads.budget) {
                Ok(data) => data,
                Err(e) if e.limit().is_none() => continue,
                Err(e) => return Err(e.into()),
            };
            if let Some(space) = data.get(16..20).and_then(|s| s.try_into().ok()) {
                return Ok(Some(space));
            }
        }
    }
    Ok(None)
}

fn structure(reads: &mut PathReads<'_>) -> Result<bool> {
    let root = reads.root()?;
    Ok(matches!(
        reads
            .resolve(
                root.as_deref()
                    .and_then(value_dict)
                    .and_then(|d| d.get("StructTreeRoot"))
            )?
            .as_deref(),
        Some(Value::Dict(_))
    ))
}

fn parent_tree(reads: &mut PathReads<'_>) -> Result<bool> {
    let root = reads.root()?;
    let structure = reads.resolve(
        root.as_deref()
            .and_then(value_dict)
            .and_then(|d| d.get("StructTreeRoot")),
    )?;
    Ok(matches!(
        reads
            .resolve(
                structure
                    .as_deref()
                    .and_then(value_dict)
                    .and_then(|d| d.get("ParentTree"))
            )?
            .as_deref(),
        Some(Value::Dict(_))
    ))
}

fn lost<'b>(
    (opened, version): (&Version, &Version),
    written: &[u32],
    rule: Rule,
    applies: fn(&Version, &Budget) -> Result<bool>,
    reason: &'static str,
    found: &mut Reasons<'b>,
    budget: &'b Budget,
) -> Result<bool> {
    let mut old = PathReads::new(opened, budget);
    let mut new = PathReads::new(version, budget);
    if !rule(&mut old)? || rule(&mut new)? || !applies(version, budget)? {
        return Ok(false);
    }
    for &n in old.owners.iter().chain(new.owners.iter()) {
        if written.binary_search(&n).is_ok() {
            record(found, reason, Some(n), budget)?;
        }
    }
    Ok(true)
}

pub(super) fn font_points_to(
    version: &Version,
    font: &Value,
    descriptor: u32,
    budget: &Budget,
) -> Result<bool> {
    let font = resolved(version, font, budget)?;
    let Some(font) = font.as_deref().and_then(value_dict) else {
        return Ok(false);
    };
    if reference_number(font.get("FontDescriptor")) == Some(descriptor) {
        return Ok(true);
    }
    let descendants = entry(version, font, "DescendantFonts", budget)?;
    if let Some(Value::Array(fonts)) = descendants.as_deref() {
        for cid in fonts.iter() {
            let child = resolved(version, cid, budget)?;
            if child
                .as_deref()
                .and_then(value_dict)
                .and_then(|d| reference_number(d.get("FontDescriptor")))
                == Some(descriptor)
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

fn mcid(path: &mut PathReads<'_>, page: &Page, operand: Option<&Value>) -> Result<Option<usize>> {
    let number = |dict: &Dict| match dict.get("MCID") {
        Some(Value::Integer(n)) => usize::try_from(*n).ok(),
        _ => None,
    };
    match operand {
        Some(Value::Dict(d)) => Ok(number(d)),
        Some(Value::Name(name)) => {
            let resources = path.resources(page)?;
            let props = path.resolve(
                resources
                    .as_deref()
                    .and_then(value_dict)
                    .and_then(|d| d.get("Properties")),
            )?;
            let entry = path.resolve(
                props
                    .as_deref()
                    .and_then(value_dict)
                    .and_then(|d| d.get(name.as_bytes())),
            )?;
            Ok(entry.as_deref().and_then(value_dict).and_then(number))
        }
        _ => Ok(None),
    }
}

fn untagged_text<'b>(
    content: &PageContent,
    page: &Page,
    version: &Version,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u32>> {
    let ops = content.operations();
    let mut marked = BudgetVec::new(budget);
    let mut untagged = BudgetVec::new(budget);
    for op in ops.operations() {
        budget.poll()?;
        let operands = ops.operands(op);
        match ops.operator(op) {
            b"BMC" | b"BDC" => {
                let inherited = marked.last().copied().unwrap_or(false);
                let artifact = matches!(operands.first(), Some(Value::Name(tag)) if tag.as_bytes() == b"Artifact");
                marked.try_push(
                    inherited
                        || artifact
                        || mcid(&mut PathReads::new(version, budget), page, operands.get(1))?
                            .is_some(),
                )?;
            }
            b"EMC" => {
                marked.pop();
            }
            b"Tj" | b"TJ" | b"'" | b"\"" if !marked.last().copied().unwrap_or(false) => {
                if let Some(r) = content.stream_of(op)
                    && untagged.last() != Some(&r.number())
                {
                    untagged.try_push(r.number())?;
                }
            }
            _ => {}
        }
    }
    untagged.as_mut_slice().sort_unstable();
    Ok(untagged)
}

fn page_effects<'b>(
    version: &Version,
    opened: &Version,
    written: &[u32],
    found: &mut Reasons<'b>,
    budget: &'b Budget,
) -> Result<()> {
    let mut reads = Reads::new(version, budget);
    let pages = PageIndex::build(&mut reads)?;
    let mut old_reads = Reads::new(opened, budget);
    let mut old_pages = None;
    for page in pages.pages() {
        budget.poll()?;
        let Some(contents) = page.dict().get("Contents") else {
            continue;
        };
        let page_number = page.object().map(|r| r.number());
        let is_written = |n: u32| written.binary_search(&n).is_ok();
        let page_written = page_number.is_some_and(is_written);
        let holder = reference_number(Some(contents)).filter(|n| is_written(*n));
        let contents = resolved(version, contents, budget)?;
        if !page_written
            && holder.is_none()
            && !matches!(contents.as_deref(), Some(Value::Array(items)) if items.iter().filter_map(|v| reference_number(Some(v))).any(is_written))
        {
            continue;
        }
        let current = untagged_text(&PageContent::read(&mut reads, page)?, page, version, budget)?;
        if current.is_empty() {
            continue;
        }
        if old_pages.is_none() {
            old_pages = Some(PageIndex::build(&mut old_reads)?);
        }
        let previous = match old_pages
            .as_ref()
            .and_then(|pages| pages.get(pages.index_of(page.object()?)?))
        {
            Some(p) => untagged_text(&PageContent::read(&mut old_reads, p)?, p, opened, budget)?,
            None => BudgetVec::new(budget),
        };
        for &n in current
            .iter()
            .filter(|n| previous.binary_search(n).is_err())
        {
            let owner = if is_written(n) {
                Some(n)
            } else {
                holder.or(page_number.filter(|n| is_written(*n)))
            };
            if owner.is_some() {
                record(
                    found,
                    "the edited text is neither tagged nor marked as an artifact",
                    owner,
                    budget,
                )?;
            }
        }
    }
    Ok(())
}

fn uses_structure_content(version: &Version, budget: &Budget) -> Result<bool> {
    let mut reads = Reads::new(version, budget);
    let pages = PageIndex::build(&mut reads)?;
    for page in pages.pages() {
        let content = PageContent::read(&mut reads, page)?;
        let ops = content.operations();
        for op in ops.operations() {
            if ops.operator(op) == b"BDC"
                && mcid(
                    &mut PathReads::new(version, budget),
                    page,
                    ops.operands(op).get(1),
                )?
                .is_some()
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

/// Resolve one MCID through the page's StructParents key in its number tree.
/// The path includes indirect Kids, Nums and the array indexed by the MCID.
fn slot(
    path: &mut PathReads<'_>,
    value: &Value,
    key: i64,
    index: usize,
    depth: usize,
) -> Result<bool> {
    path.budget.charge(LimitCode::InterpretationFuel, 1)?;
    if depth > MAX_DEPTH {
        return Err(
            LimitExceeded::over(LimitCode::ObjectDepth, MAX_DEPTH as u64, depth as u64).into(),
        );
    }
    let node = path.resolve(Some(value))?;
    let Some(dict) = node.as_deref().and_then(value_dict) else {
        return Ok(false);
    };
    if let Some(Value::Array(limits)) = dict.get("Limits")
        && let [Value::Integer(lo), Value::Integer(hi)] = limits.as_slice()
        && !(*lo..=*hi).contains(&key)
    {
        return Ok(false);
    }
    let nums = path.resolve(dict.get("Nums"))?;
    if let Some(Value::Array(pairs)) = nums.as_deref() {
        for pair in pairs.as_slice().as_chunks::<2>().0 {
            path.budget.poll()?;
            if pair[0] != Value::Integer(key) {
                continue;
            }
            let entries = path.resolve(Some(&pair[1]))?;
            let Some(Value::Array(entries)) = entries.as_deref() else {
                return Ok(false);
            };
            let target = path.resolve(entries.get(index))?;
            return Ok(target.as_deref().and_then(value_dict).is_some());
        }
    }
    let kids = path.resolve(dict.get("Kids"))?;
    if let Some(Value::Array(kids)) = kids.as_deref() {
        for child in kids.iter() {
            if slot(path, child, key, index, depth + 1)? {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

fn associated(path: &mut PathReads<'_>, page: &Page, operand: Option<&Value>) -> Result<bool> {
    let Some(index) = mcid(path, page, operand)? else {
        return Ok(false);
    };
    let Some(Value::Integer(key)) = page.dict().get("StructParents") else {
        return Ok(false);
    };
    if let Some(r) = page.object() {
        path.owners.try_push(r.number())?;
    }
    let root = path.root()?;
    let tree = path.resolve(
        root.as_deref()
            .and_then(value_dict)
            .and_then(|d| d.get("StructTreeRoot")),
    )?;
    let Some(parent_tree) = tree
        .as_deref()
        .and_then(value_dict)
        .and_then(|d| d.get("ParentTree"))
    else {
        return Ok(false);
    };
    slot(path, parent_tree, *key, index, 0)
}

/// A page or catalog on the path can also have an unrelated edit, e.g.
/// rotation on the same save as the loss of an indirect property dictionary.
fn record_path<'b>(
    old: &PathReads<'_>,
    new: &PathReads<'_>,
    written: &[u32],
    keys: &[&str],
    reason: &'static str,
    found: &mut Reasons<'b>,
    budget: &'b Budget,
) -> Result<()> {
    for &n in old
        .owners
        .iter()
        .chain(new.owners.iter())
        .filter(|n| written.binary_search(n).is_ok())
    {
        let before = object(old.version, n, budget)?;
        let after = object(new.version, n, budget)?;
        if let (Some(Value::Dict(a)), Some(Value::Dict(b))) = (before.as_deref(), after.as_deref())
            && [
                b"Catalog".as_slice(),
                b"Page",
                b"Pages",
                b"StructElem",
                b"StructTreeRoot",
            ]
            .iter()
            .any(|ty| named(a, "Type", ty))
            && keys.iter().all(|key| a.get(key) == b.get(key))
        {
            continue;
        }
        record(found, reason, Some(n), budget)?;
    }
    Ok(())
}

/// Metadata and unrelated page/catalog edits cannot change a content
/// resource. Keep them within the same budget even on a large painted page.
fn paths_changed(
    opened: &Version,
    version: &Version,
    written: &[u32],
    keys: &[&str],
    budget: &Budget,
) -> Result<bool> {
    for &n in written {
        let before = object(opened, n, budget)?;
        let after = object(version, n, budget)?;
        match (before.as_deref(), after.as_deref()) {
            (Some(Value::Dict(a)), Some(Value::Dict(b)))
                if a.get("Type") == b.get("Type")
                    && [b"Catalog".as_slice(), b"Page", b"Pages"]
                        .iter()
                        .any(|ty| named(a, "Type", ty)) =>
            {
                if keys.iter().any(|k| a.get(k) != b.get(k)) {
                    return Ok(true);
                }
            }
            (Some(Value::Stream(a)), Some(Value::Stream(b)))
                if named(a.dict(), "Type", b"Metadata") && named(b.dict(), "Type", b"Metadata") => {
            }
            _ => return Ok(true),
        }
    }
    Ok(false)
}

const ASSOCIATIONS: &[&str] = &[
    "StructTreeRoot",
    "ParentTree",
    "StructParents",
    "Resources",
    "Parent",
    "Kids",
    "Type",
];
const COLOUR: &[&str] = &[
    "OutputIntents",
    "Resources",
    "Contents",
    "Parent",
    "Kids",
    "Type",
];

fn association_changes<'b>(
    opened: &Version,
    version: &Version,
    written: &[u32],
    found: &mut Reasons<'b>,
    budget: &'b Budget,
) -> Result<()> {
    if !paths_changed(opened, version, written, ASSOCIATIONS, budget)? {
        return Ok(());
    }
    let mut reads = Reads::new(version, budget);
    let pages = PageIndex::build(&mut reads)?;
    let old_pages = PageIndex::build(&mut Reads::new(opened, budget))?;
    for page in pages.pages() {
        let Some(old_page) = page
            .object()
            .and_then(|r| old_pages.index_of(r))
            .and_then(|i| old_pages.get(i))
        else {
            continue;
        };
        let content = PageContent::read(&mut reads, page)?;
        let ops = content.operations();
        for op in ops
            .operations()
            .iter()
            .filter(|op| ops.operator(op) == b"BDC")
        {
            let operand = ops.operands(op).get(1);
            let mut now = PathReads::new(version, budget);
            if associated(&mut now, page, operand)? {
                continue;
            }
            let mut old = PathReads::new(opened, budget);
            if associated(&mut old, old_page, operand)? {
                record_path(
                    &old,
                    &now,
                    written,
                    ASSOCIATIONS,
                    "the structure tree lost its content associations",
                    found,
                    budget,
                )?;
            }
        }
    }
    Ok(())
}

fn page_space(
    path: &mut PathReads<'_>,
    page: &Page,
    part: u32,
    document_space: Option<[u8; 4]>,
) -> Result<Option<[u8; 4]>> {
    if let Some(r) = page.object() {
        path.owners.try_push(r.number())?;
    }
    let local = if part == 4 {
        intent_space(path, Some(page.dict()))?
    } else {
        None
    };
    Ok(local.or(document_space))
}

fn colour_changes<'b>(
    opened: &Version,
    version: &Version,
    written: &[u32],
    part: u32,
    found: &mut Reasons<'b>,
    budget: &'b Budget,
) -> Result<()> {
    if !paths_changed(opened, version, written, COLOUR, budget)? {
        return Ok(());
    }
    let mut old_catalog = PathReads::new(opened, budget);
    let mut catalog = PathReads::new(version, budget);
    let old_root = old_catalog.root()?;
    let root = catalog.root()?;
    let old_space = intent_space(&mut old_catalog, old_root.as_deref().and_then(value_dict))?;
    let space = intent_space(&mut catalog, root.as_deref().and_then(value_dict))?;
    let pages = PageIndex::build(&mut Reads::new(version, budget))?;
    let old_pages = PageIndex::build(&mut Reads::new(opened, budget))?;
    for page in pages.pages() {
        let mut now = PathReads::new(version, budget);
        now.owners.try_extend_from_slice(&catalog.owners)?;
        let effective = page_space(&mut now, page, part, space)?;
        let previous = page
            .object()
            .and_then(|r| old_pages.index_of(r))
            .and_then(|i| old_pages.get(i));
        let mut old = PathReads::new(opened, budget);
        old.owners.try_extend_from_slice(&old_catalog.owners)?;
        let previous_space = previous
            .map(|p| page_space(&mut old, p, part, old_space))
            .transpose()?
            .flatten();
        let fixed_owners = (old.owners.len(), now.owners.len());
        for device in [Device::Rgb, Device::Cmyk, Device::Gray] {
            old.owners.truncate(fixed_owners.0);
            now.owners.truncate(fixed_owners.1);
            if device.covered(effective) || !Use::Colour(device).page(&mut now, page)? {
                continue;
            }
            if let Some(p) = previous
                && !device.covered(previous_space)
                && Use::Colour(device).page(&mut old, p)?
            {
                continue;
            }
            record_path(
                &old,
                &now,
                written,
                COLOUR,
                "device colour lost its archival output profile",
                found,
                budget,
            )?;
        }
    }
    Ok(())
}

// A form's private entries, a widget's label and a page's rotation do
// not affect the selected font. Direct resources live in their parent object.
fn appearance_owner_changed(before: Option<&Value>, after: Option<&Value>) -> bool {
    let (a, b, keys): (&Dict, &Dict, &[&str]) = match (before, after) {
        (Some(Value::Stream(a)), Some(Value::Stream(b))) if named(a.dict(), "Subtype", b"Form") => {
            if a.data() != b.data() {
                return true;
            }
            (
                a.dict(),
                b.dict(),
                &["Subtype", "Resources", "Filter", "DecodeParms"],
            )
        }
        (Some(Value::Dict(a)), Some(Value::Dict(b))) if is_annotation_dict(a) => (a, b, &["AP"]),
        (Some(Value::Dict(a)), Some(Value::Dict(b))) if named(a, "Type", b"Catalog") => {
            (a, b, &["Pages"])
        }
        (Some(Value::Stream(a)), Some(Value::Stream(b)))
            if named(a.dict(), "Type", b"Metadata") && named(b.dict(), "Type", b"Metadata") =>
        {
            return false;
        }
        (Some(Value::Dict(a)), Some(Value::Dict(b)))
            if named(a, "Type", b"Page") || named(a, "Type", b"Pages") =>
        {
            (a, b, &["Annots", "Resources", "Kids", "Parent"])
        }
        (Some(Value::Dict(a)), Some(Value::Dict(b))) if named(a, "Type", b"FontDescriptor") => {
            (a, b, &["FontFile", "FontFile2", "FontFile3"])
        }
        (Some(Value::Dict(a)), Some(Value::Dict(b))) if named(a, "Type", b"Font") => {
            (a, b, &["Subtype", "FontDescriptor", "DescendantFonts"])
        }
        _ => return true,
    };
    keys.iter().any(|key| a.get(key) != b.get(key))
}

/// Compare the normal Forms of reachable annotations in both versions. Merely
/// editing an existing nonconforming file does not introduce its font defect.
fn appearance_font_changes<'b>(
    opened: &Version,
    version: &Version,
    written: &[u32],
    found: &mut Reasons<'b>,
    budget: &'b Budget,
) -> Result<()> {
    const REASON: &str = "a normal annotation appearance uses a font without an embedded program";
    let mut relevant = false;
    for &number in written {
        let before = object(opened, number, budget)?;
        let after = object(version, number, budget)?;
        if appearance_owner_changed(before.as_deref(), after.as_deref()) {
            relevant = true;
            break;
        }
    }
    if !relevant {
        return Ok(());
    }
    let old_pages = PageIndex::build(&mut Reads::new(opened, budget))?;
    let mut original = BudgetVec::new(budget);
    for (index, page) in old_pages.pages().iter().enumerate() {
        let mut annotations = BudgetVec::new(budget);
        page_annot_refs(opened, page.dict(), &mut annotations, budget)?;
        original.try_extend(annotations.iter().map(|&number| (number, index)))?;
    }
    original.as_mut_slice().sort_unstable();
    let pages = PageIndex::build(&mut Reads::new(version, budget))?;
    for page in pages.pages() {
        let mut page_path = PathReads::new(version, budget);
        if let Some(id) = page.object() {
            page_path.owners.try_push(id.number())?;
        }
        let annots = page_path.resolve(page.dict().get("Annots"))?;
        let Some(Value::Array(items)) = annots.as_deref() else {
            continue;
        };
        for item in items.iter() {
            budget.poll()?;
            let Value::Reference(id) = item else {
                continue;
            };
            let mut now = PathReads::new(version, budget);
            now.owners.try_extend_from_slice(&page_path.owners)?;
            let current = now.resolve(Some(item))?;
            let Some(Value::Dict(annotation)) = current.as_deref() else {
                continue;
            };
            let start = now.owners.len();
            if !missing_appearance_font(&mut now, annotation, page, Some(&[]))? {
                continue;
            }
            let mut old = PathReads::new(opened, budget);
            let old_page = original
                .binary_search_by_key(&id.number(), |&(number, _)| number)
                .ok()
                .and_then(|index| old_pages.get(original[index].1));
            let previous = if old_page.is_some() {
                old.resolve(Some(item))?
            } else {
                None
            };
            let before = previous.as_deref().and_then(value_dict);
            let mut existing = BudgetVec::new(budget);
            if let (Some(dict), Some(old_page)) = (before, old_page) {
                missing_appearance_font(&mut old, dict, old_page, None)?;
                existing.try_extend(
                    old.appearance_fonts
                        .into_vec()
                        .into_iter()
                        .filter_map(|use_| use_.shown.then_some((use_.font, use_.missing))),
                )?;
                existing.as_mut_slice().sort_unstable();
            }
            if !existing.is_empty() {
                now.owners.truncate(start);
                now.font_owners.truncate(0);
                if !missing_appearance_font(&mut now, annotation, page, Some(&existing))? {
                    if now.changed_programs.is_empty() {
                        continue;
                    }
                    now.owners.truncate(0);
                }
            }
            if !now.owners.is_empty()
                && written.binary_search(&id.number()).is_ok()
                && before.is_none_or(|d| d.get("AP") != annotation.get("AP"))
            {
                record(found, REASON, Some(id.number()), budget)?;
            } else {
                for &owner in now
                    .owners
                    .iter()
                    .chain(now.changed_programs.iter())
                    .filter(|n| written.binary_search(n).is_ok())
                {
                    let before = object(opened, owner, budget)?;
                    let after = object(version, owner, budget)?;
                    if appearance_owner_changed(before.as_deref(), after.as_deref()) {
                        record(found, REASON, Some(owner), budget)?;
                    }
                }
            }
        }
    }
    Ok(())
}

pub(super) fn dependencies<'b>(
    claim: Claim,
    opened: &Version,
    version: &Version,
    written: &[u32],
    found: &mut Reasons<'b>,
    budget: &'b Budget,
) -> Result<()> {
    appearance_font_changes(opened, version, written, found, budget)?;
    if claim == Claim::Ua(2) {
        let old = undescribed_files(opened, budget)?;
        for &(spec, holder) in undescribed_files(version, budget)?.iter() {
            if old
                .binary_search_by_key(&spec, |(number, _)| *number)
                .is_err()
            {
                let cause = if written.contains(&spec) {
                    spec
                } else {
                    holder
                };
                record(
                    found,
                    "an EmbeddedFiles file specification lacks a description",
                    Some(cause),
                    budget,
                )?;
            }
        }
    }
    if let Claim::A(part, _) = claim {
        colour_changes(opened, version, written, part, found, budget)?;
    }
    if matches!(claim, Claim::Ua(_) | Claim::A(1..=3, b'A')) {
        let root_lost = lost(
            (opened, version),
            written,
            structure,
            |_, _| Ok(true),
            "the catalog lost its required structure tree",
            found,
            budget,
        )?;
        if matches!(claim, Claim::Ua(_)) {
            if !root_lost {
                let tree_lost = lost(
                    (opened, version),
                    written,
                    parent_tree,
                    uses_structure_content,
                    "the structure tree lost its content associations",
                    found,
                    budget,
                )?;
                if !tree_lost {
                    association_changes(opened, version, written, found, budget)?;
                }
            }
            page_effects(version, opened, written, found, budget)?;
        }
    }
    Ok(())
}

/// FileAttachment annotations and associated files have other descriptions.
/// ISO 14289-2 8.14.1 checks the file specifications listed in EmbeddedFiles.
fn undescribed_files<'b>(
    version: &'b Version,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, (u32, u32)>> {
    let mut out = BudgetVec::new(budget);
    let catalog = catalog(version, budget)?;
    let Some(catalog) = catalog.as_deref().and_then(value_dict) else {
        return Ok(out);
    };
    let names = entry(version, catalog, "Names", budget)?;
    let Some(names) = names.as_deref().and_then(value_dict) else {
        return Ok(out);
    };
    let Some(root) = names.get("EmbeddedFiles") else {
        return Ok(out);
    };
    let holder = reference_number(Some(root))
        .or_else(|| reference_number(version.trailer().get("Root")))
        .unwrap_or(0);
    fn visit(
        tree: &mut PathReads<'_>,
        value: &Value,
        owner: u32,
        depth: usize,
        seen: &mut HashSet<Reference>,
        out: &mut BudgetVec<'_, (u32, u32)>,
    ) -> Result<()> {
        tree.budget.poll()?;
        if depth > MAX_DEPTH {
            return Err(LimitExceeded::over(
                LimitCode::ObjectDepth,
                MAX_DEPTH as u64,
                depth as u64,
            )
            .into());
        }
        if let Value::Reference(r) = value
            && !try_insert_set(seen, *r, tree.budget)?
        {
            return Ok(());
        }
        let owner = reference_number(Some(value)).unwrap_or(owner);
        let node = tree.resolve(Some(value))?;
        let Some(dict) = node.as_deref().and_then(value_dict) else {
            return Ok(());
        };
        let names = tree.resolve(dict.get("Names"))?;
        if let Some(Value::Array(pairs)) = names.as_deref() {
            let holder = reference_number(dict.get("Names")).unwrap_or(owner);
            for pair in pairs.as_slice().as_chunks::<2>().0 {
                let spec = tree.resolve(Some(&pair[1]))?;
                if spec
                    .as_deref()
                    .and_then(value_dict)
                    .is_some_and(|d| d.get("Desc").is_none())
                {
                    out.try_push((reference_number(Some(&pair[1])).unwrap_or(holder), holder))?;
                }
            }
        }
        let kids = tree.resolve(dict.get("Kids"))?;
        if let Some(Value::Array(kids)) = kids.as_deref() {
            for child in kids.iter() {
                visit(tree, child, owner, depth + 1, seen, out)?;
            }
        }
        Ok(())
    }
    visit(
        &mut PathReads::new(version, budget),
        root,
        holder,
        0,
        &mut HashSet::new(),
        &mut out,
    )?;
    out.as_mut_slice().sort_unstable();
    Ok(out)
}
