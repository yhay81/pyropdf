//! Profile effects that depend on a catalog relationship, changed page content,
//! or the previous value of a written object.

use super::*;
use crate::content::PageContent;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;

fn named(dict: &Dict, key: &str, expected: &[u8]) -> bool {
    matches!(dict.get(key), Some(Value::Name(name)) if name.as_bytes() == expected)
}

fn entry<'a>(
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
) -> Result<[Option<&'static str>; 8]> {
    let mut reasons = [None; 8];
    match claim {
        Claim::A(part, level) => {
            reasons[0] = widget_action(claim, dict)
                .then_some("the widget has an action forbidden by the archival profile");
            reasons[1] =
                (part > 1 && named(dict, "Type", b"Page") && dict.get("PresSteps").is_some())
                    .then_some("the page contains forbidden presentation steps");
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
        .unwrap_or([None; 8]);
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
        && uses_page_font(version, number, budget)?
    {
        record(
            found,
            "the font descriptor lost its embedded font program",
            Some(number),
            budget,
        )?;
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
struct PathReads<'a> {
    version: &'a Version,
    budget: &'a Budget,
    owners: BudgetVec<'a, u32>,
}
impl<'a> PathReads<'a> {
    fn new(version: &'a Version, budget: &'a Budget) -> Self {
        Self {
            version,
            budget,
            owners: BudgetVec::new(budget),
        }
    }
    fn resolve<'v>(&mut self, value: Option<&'v Value>) -> Result<Option<Resolved<'v>>> {
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
    fn root(&mut self) -> Result<Option<Resolved<'a>>> {
        self.resolve(self.version.trailer().get("Root"))
    }
}

type Rule = fn(&mut PathReads<'_>) -> Result<bool>;

fn output_intent(reads: &mut PathReads<'_>) -> Result<bool> {
    let root = reads.root()?;
    let intents = reads.resolve(
        root.as_deref()
            .and_then(value_dict)
            .and_then(|d| d.get("OutputIntents")),
    )?;
    let Some(Value::Array(items)) = intents.as_deref() else {
        return Ok(false);
    };
    for item in items.iter() {
        let item = reads.resolve(Some(item))?;
        if let Some(dict) = item.as_deref().and_then(value_dict)
            && named(dict, "S", b"GTS_PDFA1")
            && matches!(
                reads.resolve(dict.get("DestOutputProfile"))?.as_deref(),
                Some(Value::Stream(_))
            )
        {
            return Ok(true);
        }
    }
    Ok(false)
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

fn font_points_to(
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

/// Embedding applies to a font used to show glyphs. A font left in Resources
/// after its text is removed does not make the profile fail.
fn uses_page_font(version: &Version, descriptor: u32, budget: &Budget) -> Result<bool> {
    let mut reads = Reads::new(version, budget);
    for page in PageIndex::build(&mut reads)?.pages() {
        let Some(resources) = page.resources() else {
            continue;
        };
        let resources = resolved(version, resources, budget)?;
        let Some(dict) = resources.as_deref().and_then(value_dict) else {
            continue;
        };
        let fonts = entry(version, dict, "Font", budget)?;
        let Some(fonts) = fonts.as_deref().and_then(value_dict) else {
            continue;
        };
        let content = PageContent::read(&mut reads, page)?;
        let ops = content.operations();
        let mut selected = false;
        let mut saved = BudgetVec::new(budget);
        for op in ops.operations() {
            budget.poll()?;
            match ops.operator(op) {
                b"q" => saved.try_push(selected)?,
                b"Q" => selected = saved.pop().unwrap_or(false),
                b"Tf" => {
                    selected = match ops.operands(op).first() {
                        Some(Value::Name(name)) => match fonts.get(name.as_bytes()) {
                            Some(font) => font_points_to(version, font, descriptor, budget)?,
                            None => false,
                        },
                        _ => false,
                    };
                }
                b"Tj" | b"TJ" | b"'" | b"\"" if selected => return Ok(true),
                _ => {}
            }
        }
    }
    Ok(false)
}

fn tagged_operands(operands: &[Value]) -> bool {
    operands
        .get(1)
        .and_then(value_dict)
        .is_some_and(|d| d.get("MCID").is_some())
}

fn untagged_text<'b>(content: &PageContent, budget: &'b Budget) -> Result<BudgetVec<'b, u32>> {
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
                marked.try_push(inherited || artifact || tagged_operands(operands))?;
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
        let current = untagged_text(&PageContent::read(&mut reads, page)?, budget)?;
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
            Some(p) => untagged_text(&PageContent::read(&mut old_reads, p)?, budget)?,
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
        if ops
            .operations()
            .iter()
            .any(|op| ops.operator(op) == b"BDC" && tagged_operands(ops.operands(op)))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn has_device_colour(version: &Version, budget: &Budget) -> Result<bool> {
    let mut reads = Reads::new(version, budget);
    let pages = PageIndex::build(&mut reads)?;
    for page in pages.pages() {
        let content = PageContent::read(&mut reads, page)?;
        let ops = content.operations();
        let resources = page
            .resources()
            .map(|v| resolved(version, v, budget))
            .transpose()?
            .flatten();
        let spaces = match resources.as_deref().and_then(value_dict) {
            Some(dict) => entry(version, dict, "ColorSpace", budget)?,
            None => None,
        };
        for op in ops.operations() {
            let default = match ops.operator(op) {
                b"rg" | b"RG" => "DefaultRGB",
                b"k" | b"K" => "DefaultCMYK",
                _ => continue,
            };
            if !spaces
                .as_deref()
                .and_then(value_dict)
                .is_some_and(|dict| dict.get(default).is_some())
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

pub(super) fn dependencies<'b>(
    claim: Claim,
    opened: &Version,
    version: &Version,
    written: &[u32],
    found: &mut Reasons<'b>,
    budget: &'b Budget,
) -> Result<()> {
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
    if matches!(claim, Claim::A(..)) {
        lost(
            (opened, version),
            written,
            output_intent,
            has_device_colour,
            "device colour lost its archival output profile",
            found,
            budget,
        )?;
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
                lost(
                    (opened, version),
                    written,
                    parent_tree,
                    uses_structure_content,
                    "the structure tree lost its content associations",
                    found,
                    budget,
                )?;
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
