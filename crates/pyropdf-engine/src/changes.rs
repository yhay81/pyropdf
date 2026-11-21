//! The change report (DESIGN.md section 10, as #157 decided): what the edits
//! since opening invalidate before a save.
//!
//! Signatures come from the opened file's field tree and catalog permissions.
//! A change to a signed object or a full save can invalidate a signature.
//! The XMP in the opened and current versions identifies the archival and
//! accessibility claims. Compare edited values and their profile dependencies
//! between these versions, including a claim removed with its metadata.

mod permissions;
mod profiles;
mod resources;

pub use permissions::SignaturePermission;

use std::collections::HashSet;
use std::fmt;
use std::ops::Deref;
use std::sync::Arc;

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, try_insert_set};
use pyropdf_store::index::{Index, Location};
use pyropdf_store::value::{Dict, Reference, Value};
use pyropdf_store::version::{ChangeSet, Version};

use crate::annotations::version::{
    PdfVersion, dict_required_version, is_annotation_dict, is_pdfa_1_forbidden_subtype,
    parse_version,
};
use crate::forms::text_string;

/// The most field dictionaries that the walk for signatures visits.
pub const MAX_NODES: usize = 4_096;
/// The most levels of the field tree.
pub const MAX_DEPTH: usize = 64;
/// The most nested containers of one written object that the claim checks
/// walk.
pub const MAX_NESTING: usize = 64;
/// The most written objects that one report names for one signature or
/// claim.
pub const MAX_NAMED: usize = 1_024;

/// The result of computing a change report.
pub type Result<T, E = Error> = std::result::Result<T, E>;

/// Why computing a change report refused or failed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Error {
    /// A refusal or failure from the underlying store or budget.
    Store(pyropdf_store::Error),
    /// A structural safety bound (such as field-tree node count or depth).
    Pdf(String),
}

impl Error {
    /// Returns the limit code if a budget limit refused the operation.
    #[must_use]
    pub const fn limit_code(&self) -> Option<LimitCode> {
        match self {
            Self::Store(error) => match error.limit() {
                Some(refusal) => Some(refusal.code()),
                None => None,
            },
            Self::Pdf(_) => None,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Store(error) => fmt::Display::fmt(error, formatter),
            Self::Pdf(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for Error {}

impl From<pyropdf_store::Error> for Error {
    fn from(error: pyropdf_store::Error) -> Self {
        Self::Store(error)
    }
}

impl From<LimitExceeded> for Error {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Store(pyropdf_store::Error::from(refusal))
    }
}

/// A signature of the opened file that the edits or the save mode invalidate.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SignatureChange {
    /// The signature field's full name (or `Perms/<key>`).
    pub field: String,
    /// The signature field's object number, or the `/Perms` entry's object
    /// number (0 when direct).
    pub xref: u32,
    /// Why the signature is invalidated.
    pub reason: String,
    /// Edited object numbers inside the signature's signed byte ranges, empty
    /// when a non-incremental save mode alone invalidates the signature.
    pub objects: Vec<u32>,
}

/// A PDF/A or PDF/UA claim of the metadata that the edits break.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaimChange {
    /// The claim name, such as `"PDF/A-1B"` or `"PDF/UA-1"`.
    pub claim: String,
    /// Why the edits break the claim.
    pub reason: String,
    /// Edited object numbers that break the claim.
    pub objects: Vec<u32>,
}

/// What the edits since opening invalidate, for a save that is incremental or
/// not.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ChangeReport {
    /// Signatures of the opened file invalidated by the edits or save mode.
    pub signatures: Vec<SignatureChange>,
    /// PDF/A or PDF/UA metadata claims broken by the edits.
    pub claims: Vec<ClaimChange>,
    /// Modification-policy effects of the proposed incremental edits.
    pub permissions: Vec<SignaturePermission>,
}

impl ChangeReport {
    /// Returns whether the report names no invalidated signatures or broken
    /// claims.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.signatures.is_empty() && self.claims.is_empty() && self.permissions.is_empty()
    }
}

/// A signature of the opened file: its field's full name and object, and
/// the byte ranges that it signs.
struct Signature {
    name: String,
    field: u32,
    /// The signature dictionary's object, when it is one.
    value: Option<u32>,
    ranges: [(u64, u64); 2],
}

enum Resolved<'a> {
    Borrowed(&'a Value),
    Indirect(Arc<Value>),
}

impl Deref for Resolved<'_> {
    type Target = Value;

    fn deref(&self) -> &Self::Target {
        match self {
            Self::Borrowed(value) => value,
            Self::Indirect(value) => value,
        }
    }
}

/// Returns the signatures and the claims that the edits since opening, or
/// a save that is not `incremental`, invalidate.
pub fn report(version: &Version, incremental: bool, budget: &Budget) -> Result<ChangeReport> {
    let opened = version.opened();
    let change_set = version.changes(&opened, budget)?;
    let written = change_set.as_ref().map_or(&[][..], ChangeSet::objects);

    let mut signatures = BudgetVec::new(budget);
    let existing = signatures_of(&opened, budget)?;
    let permissions =
        if incremental && let Some(set) = change_set.as_ref().filter(|set| !set.is_empty()) {
            permissions::analyze(&opened, version, existing.as_slice(), set, budget)?
        } else {
            Vec::new()
        };
    let rewrites_signed_revision = if incremental || existing.is_empty() {
        false
    } else if written.is_empty() {
        covers_revision(&opened, existing.as_slice())
    } else {
        covers_revision(&opened, signatures_of(version, budget)?.as_slice())
    };
    for signature in existing.into_vec() {
        budget.poll()?;
        if !incremental {
            let reason = "a save that is not incremental rewrites the signed bytes";
            budget.charge(LimitCode::MemorySize, reason.len() as u64)?;
            signatures.try_push(SignatureChange {
                field: signature.name,
                xref: signature.field,
                reason: reason.to_owned(),
                objects: Vec::new(),
            })?;
            continue;
        }
        let Some(index) = version.base().file() else {
            continue;
        };
        let mut touched = BudgetVec::new(budget);
        for &number in written
            .iter()
            .filter(|&&number| signed(index, &signature, number))
            .take(MAX_NAMED)
        {
            touched.try_push(number)?;
        }
        if !touched.is_empty() {
            let reason = "the edits change objects that the signature signs";
            budget.charge(LimitCode::MemorySize, reason.len() as u64)?;
            signatures.try_push(SignatureChange {
                field: signature.name,
                xref: signature.field,
                reason: reason.to_owned(),
                objects: touched.into_vec(),
            })?;
        }
    }

    let claims = claims(&opened, version, written, rewrites_signed_revision, budget)?;
    Ok(ChangeReport {
        signatures: signatures.into_vec(),
        claims: claims.into_vec(),
        permissions,
    })
}

/// The PDF/A byte-range rule concerns a signature retained in the output.
fn covers_revision(opened: &Version, signatures: &[Signature]) -> bool {
    let Some(index) = opened.base().file() else {
        return false;
    };
    signatures.iter().any(|sig| {
        let [(first, _), (second, length)] = sig.ranges;
        first == 0
            && second
                .checked_add(length)
                .and_then(|n| usize::try_from(n).ok())
                .is_some_and(|end| index.revisions().iter().any(|rev| rev.end() == Some(end)))
    })
}

/// Returns whether object `number` of the opened file lies in a byte range
/// that `signature` signs: the object itself, or the object stream that
/// holds it.
fn signed(index: &Index, signature: &Signature, number: u32) -> bool {
    let offset = match index.get(number).map(|entry| entry.location()) {
        Some(Location::Offset(offset)) => offset,
        Some(Location::Member { stream, .. }) => match index.get(stream).map(|e| e.location()) {
            Some(Location::Offset(offset)) => offset,
            _ => return false,
        },
        None => return false,
    };
    let offset = offset as u64;
    signature
        .ranges
        .iter()
        .any(|&(start, length)| offset >= start && offset - start < length)
}

/// Returns object `number` of `version`, or `None` for one that is free or
/// that the store cannot read.
fn object(version: &Version, number: u32, budget: &Budget) -> Result<Option<Arc<Value>>> {
    match version.get(number, budget) {
        Ok(object) => Ok(object.map(|object| Arc::clone(object.value()))),
        Err(error) if error.limit().is_some() => Err(Error::Store(error)),
        Err(_) => Ok(None),
    }
}

/// Returns `value`, or the object that it refers to.
fn resolved<'a>(
    version: &Version,
    value: &'a Value,
    budget: &Budget,
) -> Result<Option<Resolved<'a>>> {
    match value {
        Value::Reference(reference) => {
            Ok(object(version, reference.number(), budget)?.map(Resolved::Indirect))
        }
        _ => Ok(Some(Resolved::Borrowed(value))),
    }
}

fn value_dict(value: &Value) -> Option<&Dict> {
    match value {
        Value::Dict(dict) => Some(dict),
        Value::Stream(stream) => Some(stream.dict()),
        _ => None,
    }
}

/// Returns the catalog of `version`.
fn catalog<'a>(version: &'a Version, budget: &Budget) -> Result<Option<Resolved<'a>>> {
    match version.trailer().get("Root") {
        Some(root) => resolved(version, root, budget),
        None => Ok(None),
    }
}

/// Returns the signatures of `opened`: its signed signature fields, then
/// the signatures of the catalog's `/Perms`, such as a usage rights
/// signature, that no field holds.
fn signatures_of<'b>(opened: &Version, budget: &'b Budget) -> Result<BudgetVec<'b, Signature>> {
    let catalog_val = catalog(opened, budget)?;
    let Some(catalog) = catalog_val.as_deref().and_then(value_dict) else {
        return Ok(BudgetVec::new(budget));
    };
    let mut out = BudgetVec::new(budget);
    walk_fields(opened, catalog, budget, |number, name, is_sig, node| {
        if is_sig
            && let Some(value) = node.get("V")
            && let Some(sig_val) = resolved(opened, value, budget)?
            && let Some(signature) = value_dict(&sig_val)
            && let Some(ranges) = byte_ranges(signature)
        {
            budget.charge(LimitCode::MemorySize, name.len() as u64)?;
            out.try_push(Signature {
                name: name.to_owned(),
                field: number,
                value: match value {
                    Value::Reference(reference) => Some(reference.number()),
                    _ => None,
                },
                ranges,
            })?;
        }
        Ok(())
    })?;
    let perms_val = match catalog.get("Perms") {
        Some(perms) => resolved(opened, perms, budget)?,
        None => None,
    };
    let Some(perms) = perms_val.as_deref().and_then(value_dict) else {
        return Ok(out);
    };
    for (key, value) in perms.iter() {
        budget.poll()?;
        let number = match value {
            Value::Reference(reference) => Some(reference.number()),
            _ => None,
        };
        if number.is_some() && out.as_slice().iter().any(|sig| sig.value == number) {
            continue;
        }
        let sig_val = resolved(opened, value, budget)?;
        if let Some(signature) = sig_val.as_deref().and_then(value_dict)
            && let Some(ranges) = byte_ranges(signature)
        {
            budget.charge(
                LimitCode::MemorySize,
                (key.as_bytes().len() as u64)
                    .saturating_mul(3)
                    .saturating_add(6),
            )?;
            let name = format!("Perms/{}", String::from_utf8_lossy(key.as_bytes()));
            out.try_push(Signature {
                name,
                field: number.unwrap_or(0),
                value: number,
                ranges,
            })?;
        }
    }
    Ok(out)
}

/// Walks the AcroForm field tree with each field's fully qualified name
/// and inherited signature type, under the operation's budget.
fn walk_fields(
    opened: &Version,
    catalog: &Dict,
    budget: &Budget,
    mut visit: impl FnMut(u32, &str, bool, &Dict) -> Result<()>,
) -> Result<()> {
    let form_val = match catalog.get("AcroForm") {
        Some(form) => resolved(opened, form, budget)?,
        None => None,
    };
    let Some(form) = form_val.as_deref().and_then(value_dict) else {
        return Ok(());
    };
    let fields_val = match form.get("Fields") {
        Some(fields) => resolved(opened, fields, budget)?,
        None => None,
    };
    let Some(Value::Array(fields)) = fields_val.as_deref() else {
        return Ok(());
    };
    // A node: its object, the full name of its parent, its inherited
    // `/FT`, and its depth.
    let mut pending = BudgetVec::new(budget);
    pending.try_extend(fields.iter().rev().filter_map(|field| match field {
        Value::Reference(reference) => Some((reference.number(), String::new(), false, 1)),
        _ => None,
    }))?;
    let mut visited = BudgetVec::new(budget);
    while let Some((number, parent, inherited, depth)) = pending.pop() {
        budget.poll()?;
        if visited.as_slice().contains(&number) {
            continue;
        }
        visited.try_push(number)?;
        if visited.len() > MAX_NODES {
            return Err(Error::Pdf(format!(
                "AcroForm field tree exceeds the {MAX_NODES}-node safety limit"
            )));
        }
        let node_val = object(opened, number, budget)?;
        let Some(node) = node_val.as_deref().and_then(value_dict) else {
            continue;
        };
        if let Some(Value::String(partial)) = node.get("T") {
            // The decoded partial and the joined full name can coexist.
            budget.charge(
                LimitCode::MemorySize,
                (parent.len() as u64)
                    .saturating_add((partial.as_bytes().len() as u64).saturating_mul(6))
                    .saturating_add(1),
            )?;
        }
        let name = match node.get("T") {
            Some(Value::String(partial)) if parent.is_empty() => text_string(partial.as_bytes()),
            Some(Value::String(partial)) => {
                format!("{parent}.{}", text_string(partial.as_bytes()))
            }
            _ => parent,
        };
        let is_sig = match node.get("FT") {
            Some(Value::Name(kind)) => kind.as_bytes() == b"Sig",
            _ => inherited,
        };
        visit(number, &name, is_sig, node)?;
        if let Some(kids) = node.get("Kids")
            && let Some(kids_val) = resolved(opened, kids, budget)?
            && let Value::Array(kids) = &*kids_val
        {
            if depth >= MAX_DEPTH {
                return Err(Error::Pdf(format!(
                    "AcroForm field tree exceeds the {MAX_DEPTH}-level safety limit"
                )));
            }
            for kid in kids.iter().rev() {
                if let Value::Reference(reference) = kid {
                    budget.charge(LimitCode::MemorySize, name.len() as u64)?;
                    pending.try_push((reference.number(), name.clone(), is_sig, depth + 1))?;
                }
            }
        }
    }
    Ok(())
}

/// Returns the two byte ranges of a signature's `/ByteRange`.
fn byte_ranges(signature: &Dict) -> Option<[(u64, u64); 2]> {
    let Some(Value::Array(items)) = signature.get("ByteRange") else {
        return None;
    };
    let mut numbers = [0_u64; 4];
    if items.len() != 4 {
        return None;
    }
    for (slot, item) in numbers.iter_mut().zip(items.iter()) {
        let Value::Integer(number) = item else {
            return None;
        };
        *slot = u64::try_from(*number).ok()?;
    }
    Some([(numbers[0], numbers[1]), (numbers[2], numbers[3])])
}

/// A conformance claim of the XMP metadata.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Claim {
    /// PDF/A, its part and conformance level.
    A(u32, u8),
    /// PDF/UA, its part.
    Ua(u32),
}

impl Claim {
    fn name(self) -> String {
        match self {
            Self::A(part, 0) => format!("PDF/A-{part}"),
            Self::A(part, level) => format!("PDF/A-{part}{}", char::from(level)),
            Self::Ua(part) => format!("PDF/UA-{part}"),
        }
    }
}

/// Returns the `pdfaid:part` claimed by `version`'s XMP metadata, if any.
pub(crate) fn pdfa_part(
    version: &Version,
    budget: &Budget,
) -> std::result::Result<Option<u32>, pyropdf_store::Error> {
    let to_store = |error: Error| match error {
        Error::Store(err) => err,
        Error::Pdf(message) => pyropdf_store::Error::malformed(message),
    };
    let catalog_val = catalog(version, budget).map_err(to_store)?;
    let metadata_val = match catalog_val
        .as_deref()
        .and_then(value_dict)
        .and_then(|catalog| catalog.get("Metadata"))
    {
        Some(metadata) => resolved(version, metadata, budget).map_err(to_store)?,
        None => None,
    };
    let Some(Value::Stream(metadata)) = metadata_val.as_deref() else {
        return Ok(None);
    };
    let xmp = match metadata.decoded(budget) {
        Ok(data) => data,
        Err(error) if error.limit().is_some() => return Err(error),
        Err(_) => return Ok(None),
    };
    Ok(property(&xmp, b"pdfaid:part").and_then(number))
}

const VERSION_A1: &str = "the edits use features newer than PDF 1.4, which PDF/A-1 forbids";
const VERSION_A2: &str = "the edits use features newer than PDF 1.7, which PDF/A-2 forbids";
const VERSION_A3: &str = "the edits use features newer than PDF 1.7, which PDF/A-3 forbids";

fn catalog_declared_version(version: &Version, budget: &Budget) -> Result<Option<PdfVersion>> {
    let catalog_val = catalog(version, budget)?;
    let Some(dict) = catalog_val.as_deref().and_then(value_dict) else {
        return Ok(None);
    };
    let Some(declared) = dict.get("Version") else {
        return Ok(None);
    };
    let resolved_val = resolved(version, declared, budget)?;
    Ok(match resolved_val.as_deref() {
        Some(Value::Name(name)) => parse_version(name.as_bytes()),
        _ => None,
    })
}

#[derive(Default)]
struct Identification {
    claims: [Option<Claim>; 2],
    revision: [Option<u32>; 2],
    stream: Option<u32>,
}

/// Read both identifications from the same decoded packet.
fn version_claims(version: &Version, budget: &Budget) -> Result<Identification> {
    let catalog_val = catalog(version, budget)?;
    let Some(metadata) = catalog_val
        .as_deref()
        .and_then(value_dict)
        .and_then(|dict| dict.get("Metadata"))
    else {
        return Ok(Identification::default());
    };
    let metadata_val = resolved(version, metadata, budget)?;
    match metadata_val.as_deref() {
        Some(Value::Stream(stream)) => {
            let xmp = stream.decoded(budget)?;
            Ok(Identification {
                claims: read_claims(&xmp),
                revision: [
                    property(&xmp, b"pdfaid:rev").and_then(number),
                    property(&xmp, b"pdfuaid:rev").and_then(number),
                ],
                stream: reference_number(Some(metadata)),
            })
        }
        _ => Ok(Identification {
            stream: reference_number(Some(metadata)),
            ..Default::default()
        }),
    }
}

type Reasons<'b> = BudgetVec<'b, (&'static str, BudgetVec<'b, u32>)>;

fn record<'b>(
    found: &mut Reasons<'b>,
    reason: &'static str,
    number: Option<u32>,
    budget: &'b Budget,
) -> Result<()> {
    if let Some((_, objects)) = found
        .as_mut_slice()
        .iter_mut()
        .find(|(known, _)| *known == reason)
    {
        if let Some(number) = number
            && objects.len() < MAX_NAMED
            && !objects.contains(&number)
        {
            objects.try_push(number)?;
        }
    } else {
        let mut objects = BudgetVec::new(budget);
        if let Some(number) = number {
            objects.try_push(number)?;
        }
        found.try_push((reason, objects))?;
    }
    Ok(())
}

fn claims<'b>(
    opened: &Version,
    version: &Version,
    written: &[u32],
    rewrites_signature: bool,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, ClaimChange>> {
    let mut out = BudgetVec::new(budget);
    if written.is_empty() && !rewrites_signature {
        return Ok(out);
    }
    let original = version_claims(opened, budget)?;
    let current = version_claims(version, budget)?;
    if original.claims == [None, None] && current.claims == [None, None] {
        return Ok(out);
    }
    let removed = removed_annotations(opened, version, written, budget)?;
    let root = reference_number(version.trailer().get("Root"));
    let metadata_owner = if original.stream == current.stream {
        original.stream
    } else {
        root
    };
    for (index, (old, new)) in original.claims.into_iter().zip(current.claims).enumerate() {
        for claim in old
            .into_iter()
            .filter(|claim| Some(*claim) != new)
            .chain(new)
        {
            let mut found = Reasons::new(budget);
            if new != Some(claim) {
                record(
                    &mut found,
                    "the edits remove the required XMP profile identification",
                    metadata_owner,
                    budget,
                )?;
            } else {
                if matches!(claim, Claim::Ua(2) | Claim::A(4, _))
                    && original.revision[index].is_some()
                    && current.revision[index].is_none()
                {
                    record(
                        &mut found,
                        "the XMP identification lost its required revision year",
                        metadata_owner,
                        budget,
                    )?;
                }
                if matches!(claim, Claim::A(2 | 3, _)) && rewrites_signature {
                    record(
                        &mut found,
                        "a full save rewrites the byte range covered by a signature",
                        None,
                        budget,
                    )?;
                }
                for &number in written {
                    budget.poll()?;
                    let Some(value) = object(version, number, budget)? else {
                        continue;
                    };
                    if let Value::Dict(dict) = &*value
                        && is_annotation_dict(dict)
                        && is_removed_annotation(version, removed.as_slice(), number, dict, budget)?
                    {
                        continue;
                    }
                    let previous = object(opened, number, budget)?;
                    let is_catalog = Some(number) == root;
                    let before = previous
                        .as_deref()
                        .map(|v| breaks(claim, v, budget))
                        .transpose()?
                        .unwrap_or([None; 4]);
                    for reason in breaks(claim, &value, budget)?
                        .into_iter()
                        .flatten()
                        .filter(|r| !before.contains(&Some(*r)))
                    {
                        record(&mut found, reason, Some(number), budget)?;
                        // A promoted catalog also names the higher version needed by this annotation.
                        if matches!(reason, VERSION_A1 | VERSION_A2 | VERSION_A3)
                            && let Some(root) = root
                            && written.contains(&root)
                            && catalog_declared_version(version, budget)?
                                > catalog_declared_version(opened, budget)?
                        {
                            record(&mut found, reason, Some(root), budget)?;
                        }
                    }
                    profiles::object_changes(
                        claim,
                        (opened, version),
                        number,
                        previous.as_deref(),
                        &value,
                        &mut found,
                        budget,
                    )?;
                    if claim == Claim::Ua(1)
                        && is_catalog
                        && lost_alt_language(opened, version, &value, budget)?
                    {
                        record(
                            &mut found,
                            "removing the catalog language leaves structure alternate text without a language",
                            Some(number),
                            budget,
                        )?;
                    }
                }
                if !written.is_empty() {
                    profiles::dependencies(claim, opened, version, written, &mut found, budget)?;
                }
            }
            for (reason, mut breaking) in found.into_vec() {
                breaking.as_mut_slice().sort_unstable();
                let claim_name = claim.name();
                budget.charge(
                    LimitCode::MemorySize,
                    (claim_name.len() + reason.len()) as u64,
                )?;
                out.try_push(ClaimChange {
                    claim: claim_name,
                    reason: reason.to_owned(),
                    objects: breaking.into_vec(),
                })?;
            }
        }
    }
    Ok(out)
}

fn reference_number(value: Option<&Value>) -> Option<u32> {
    match value {
        Some(Value::Reference(r)) => Some(r.number()),
        _ => None,
    }
}

/// ISO 14289-1 §7.2: alternate text can inherit its language from a
/// structure element. A missing catalog /Lang alone does not break this rule.
fn lost_alt_language(
    opened: &Version,
    version: &Version,
    value: &Value,
    budget: &Budget,
) -> Result<bool> {
    let Value::Dict(current) = value else {
        return Ok(false);
    };
    if has_text_entry(version, current, "Lang", budget)? {
        return Ok(false);
    }
    let original = catalog(opened, budget)?;
    let Some(original) = original.as_deref().and_then(value_dict) else {
        return Ok(false);
    };
    if !has_text_entry(opened, original, "Lang", budget)? {
        return Ok(false);
    }
    let Some(root) = current.get("StructTreeRoot") else {
        return Ok(false);
    };
    let root = resolved(version, root, budget)?;
    let Some(kids) = root
        .as_deref()
        .and_then(value_dict)
        .and_then(|dict| dict.get("K"))
    else {
        return Ok(false);
    };
    alt_needs_language(version, kids, &mut HashSet::new(), 0, budget)
}

fn has_text_entry(version: &Version, dict: &Dict, key: &str, budget: &Budget) -> Result<bool> {
    let Some(value) = dict.get(key) else {
        return Ok(false);
    };
    Ok(
        matches!(resolved(version, value, budget)?.as_deref(), Some(Value::String(text)) if !text.as_bytes().is_empty()),
    )
}

/// Follow only /K children of structure elements, stopping at each language
/// boundary. Only paths that still depend on the catalog enter the visited set.
fn alt_needs_language(
    version: &Version,
    value: &Value,
    seen: &mut HashSet<Reference>,
    depth: usize,
    budget: &Budget,
) -> Result<bool> {
    budget.poll()?;
    if depth > MAX_DEPTH {
        return Err(LimitExceeded::over(
            LimitCode::StructureTreeCount,
            MAX_DEPTH as u64,
            depth as u64,
        )
        .into());
    }
    if let Value::Reference(reference) = value
        && (version.generation(reference.number()) != Some(reference.generation())
            || !try_insert_set(seen, *reference, budget)?)
    {
        return Ok(false);
    }
    let node = resolved(version, value, budget)?;
    match node.as_deref() {
        Some(Value::Array(children)) => {
            for child in children.iter() {
                if alt_needs_language(version, child, seen, depth + 1, budget)? {
                    return Ok(true);
                }
            }
        }
        Some(Value::Dict(element)) if element.get("S").is_some() => {
            if has_text_entry(version, element, "Lang", budget)? {
                return Ok(false);
            }
            if has_text_entry(version, element, "Alt", budget)? {
                return Ok(true);
            }
            if let Some(kids) = element.get("K") {
                return alt_needs_language(version, kids, seen, depth + 1, budget);
            }
        }
        _ => {}
    }
    Ok(false)
}

/// Returns the PDF/A-1, PDF/A-2, PDF/A-3, PDF/A-4, and PDF/UA claims of XMP
/// metadata.
fn read_claims(xmp: &[u8]) -> [Option<Claim>; 2] {
    let pdfa = property(xmp, b"pdfaid:part")
        .and_then(number)
        .and_then(|part| {
            let level = property(xmp, b"pdfaid:conformance")
                .and_then(|level| level.first().copied())
                .map_or(0, |level| level.to_ascii_uppercase());
            matches!(part, 1..=4).then_some(Claim::A(part, level))
        });
    let pdfua = property(xmp, b"pdfuaid:part")
        .and_then(number)
        .map(Claim::Ua);
    [pdfa, pdfua]
}

/// Returns the value of an XMP property, written as an attribute
/// `name="value"` or as an element `<name>value</name>`, trimmed.
fn property<'a>(xmp: &'a [u8], name: &[u8]) -> Option<&'a [u8]> {
    let mut at = 0;
    while let Some(found) = find(&xmp[at..], name) {
        let start = at + found;
        at = start + name.len();
        // The name must not continue a longer name, such as `notpdfaid:part`.
        if start > 0 && (xmp[start - 1].is_ascii_alphanumeric() || xmp[start - 1] == b':') {
            continue;
        }
        let rest = trim_start(&xmp[at..]);
        if let Some(rest) = rest.strip_prefix(b"=") {
            let rest = trim_start(rest);
            let Some((&quote, rest)) = rest.split_first() else {
                continue;
            };
            if quote != b'"' && quote != b'\'' {
                continue;
            }
            if let Some(end) = rest.iter().position(|&byte| byte == quote) {
                return Some(trim(&rest[..end]));
            }
        } else if start > 0
            && xmp[start - 1] == b'<'
            && let Some(rest) = rest.strip_prefix(b">")
            && let Some(end) = rest.iter().position(|&byte| byte == b'<')
        {
            return Some(trim(&rest[..end]));
        }
    }
    None
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn trim_start(bytes: &[u8]) -> &[u8] {
    let start = bytes
        .iter()
        .position(|b| !b.is_ascii_whitespace())
        .unwrap_or(bytes.len());
    &bytes[start..]
}

fn trim(bytes: &[u8]) -> &[u8] {
    let bytes = trim_start(bytes);
    let end = bytes
        .iter()
        .rposition(|b| !b.is_ascii_whitespace())
        .map_or(0, |end| end + 1);
    &bytes[..end]
}

fn number(bytes: &[u8]) -> Option<u32> {
    std::str::from_utf8(bytes).ok()?.parse().ok()
}

/// Returns the reasons for which a written object breaks `claim`.
///
/// PDF/A-1 forbids transparency, embedded files, annotation subtypes outside
/// ISO 19005-1 §6.5.2, and PDF features newer than 1.4 (ISO 19005-1 sections
/// 6.1.2, 6.1.11, 6.4, and 6.5.2). PDF/A-2 and PDF/A-3 forbid PDF features
/// newer than 1.7, and PDF/A-2 and PDF/A-4 (other than PDF/A-4f) restrict
/// embedded files to allowed PDF/A parts (ISO 19005-2 section 6.8, ISO 19005-4
/// section 6.9). PDF/UA requires every annotation in the structure tree.
fn breaks(claim: Claim, value: &Value, budget: &Budget) -> Result<[Option<&'static str>; 4]> {
    let reasons = match claim {
        Claim::A(1, _) => [
            has_transparency(value, budget)?
                .then_some("the edits add transparency, which PDF/A-1 forbids"),
            has_embedded_file(value, budget)?
                .then_some("the edits add an embedded file, which PDF/A-1 forbids"),
            is_pdfa_1_forbidden_annotation(value)
                .then_some("the edits add an annotation type that PDF/A-1 forbids"),
            exceeds_pdf_version(value, PdfVersion(1, 4), true)
                .then_some(VERSION_A1),
        ],
        Claim::A(2, _) => [
            is_embedded_file_outside(value, &[1, 2], budget)?.then_some(
                "the edits add an embedded file that is not PDF/A-1 or PDF/A-2, which PDF/A-2 forbids",
            ),
            exceeds_pdf_version(value, PdfVersion(1, 7), false)
                .then_some(VERSION_A2),
            None,
            None,
        ],
        Claim::A(3, _) => [
            exceeds_pdf_version(value, PdfVersion(1, 7), false)
                .then_some(VERSION_A3),
            None,
            None,
            None,
        ],
        Claim::A(4, level) if !matches!(level, b'E' | b'F') => [
            is_embedded_file_outside(value, &[1, 2, 4], budget)?.then_some(
                "the edits add an embedded file that is not PDF/A-1, PDF/A-2, or PDF/A-4, which PDF/A-4 forbids",
            ),
            None,
            None,
            None,
        ],
        Claim::A(..) => [None, None, None, None],
        Claim::Ua(_) => [
            value_dict(value)
                .is_some_and(is_untagged_annotation)
                .then_some("the edits add an annotation that is not in the structure tree"),
            None,
            None,
            None,
        ],
    };
    Ok(reasons)
}

fn is_pdfa_1_forbidden_annotation(value: &Value) -> bool {
    let Value::Dict(dict) = value else {
        return false;
    };
    if !is_annotation_dict(dict) {
        return false;
    }
    matches!(
        dict.get("Subtype"),
        Some(Value::Name(subtype)) if is_pdfa_1_forbidden_subtype(subtype.as_bytes())
    )
}

fn exceeds_pdf_version(value: &Value, limit: PdfVersion, skip_pdfa_1_forbidden: bool) -> bool {
    let Value::Dict(dict) = value else {
        return false;
    };
    if skip_pdfa_1_forbidden
        && matches!(
            dict.get("Subtype"),
            Some(Value::Name(subtype)) if is_pdfa_1_forbidden_subtype(subtype.as_bytes())
        )
    {
        return false;
    }
    dict_required_version(dict).is_some_and(|needed| needed > limit)
}

/// Returns whether `value` holds, in itself or in its direct containers, a
/// file specification with an embedded file or a name dictionary with
/// embedded files: the `/EF` and `/EmbeddedFiles` keys that PDF/A-1
/// forbids.
fn has_embedded_file(value: &Value, budget: &Budget) -> Result<bool> {
    let mut pending = BudgetVec::new(budget);
    pending.try_push((value, 0))?;
    while let Some((value, depth)) = pending.pop() {
        budget.poll()?;
        let dict = match value {
            Value::Dict(dict) => dict,
            Value::Stream(stream) => stream.dict(),
            Value::Array(items) => {
                if depth < MAX_NESTING {
                    pending.try_extend(items.iter().map(|item| (item, depth + 1)))?;
                }
                continue;
            }
            _ => continue,
        };
        for (key, entry) in dict.iter() {
            if matches!(key.as_bytes(), b"EF" | b"EmbeddedFiles") {
                return Ok(true);
            }
            if depth < MAX_NESTING {
                pending.try_push((entry, depth + 1))?;
            }
        }
    }
    Ok(false)
}

/// Returns whether `value` is an embedded file stream whose data is not a
/// PDF file that claims one of the PDF/A `parts`.
///
/// The claim is the `pdfaid:part` of the file's XMP metadata, which PDF/A
/// writes unfiltered; this is not a validation of the embedded file.
fn is_embedded_file_outside(value: &Value, parts: &[u32], budget: &Budget) -> Result<bool> {
    let Value::Stream(stream) = value else {
        return Ok(false);
    };
    if !matches!(stream.dict().get("Type"), Some(Value::Name(kind)) if kind.as_bytes() == b"EmbeddedFile")
    {
        return Ok(false);
    }
    // Data that does not decode is no PDF/A file; a refusal for a limit
    // stays an error.
    let data = match stream.decoded(budget) {
        Ok(data) => data,
        Err(error) if error.limit().is_some() => return Err(Error::Store(error)),
        Err(_) => return Ok(true),
    };
    let claimed = data.starts_with(b"%PDF-")
        && property(&data, b"pdfaid:part")
            .and_then(number)
            .is_some_and(|part| parts.contains(&part));
    Ok(!claimed)
}

/// Returns whether `value` holds, in itself or in its direct containers,
/// what PDF/A-1 forbids as transparency (ISO 19005-1 section 6.4): a soft
/// mask other than `/None`, a constant alpha below 1, a blend mode other
/// than `/Normal` or `/Compatible`, or a transparency group.
fn has_transparency(value: &Value, budget: &Budget) -> Result<bool> {
    let mut pending = BudgetVec::new(budget);
    pending.try_push((value, 0))?;
    while let Some((value, depth)) = pending.pop() {
        budget.poll()?;
        let dict = match value {
            Value::Dict(dict) => dict,
            Value::Stream(stream) => stream.dict(),
            Value::Array(items) => {
                if depth < MAX_NESTING {
                    pending.try_extend(items.iter().map(|item| (item, depth + 1)))?;
                }
                continue;
            }
            _ => continue,
        };
        for (key, entry) in dict.iter() {
            let forbidden = match (key.as_bytes(), entry) {
                (b"SMask", Value::Name(name)) => name.as_bytes() != b"None",
                (b"SMask", Value::Dict(_) | Value::Stream(_) | Value::Reference(_)) => true,
                (b"CA" | b"ca", Value::Integer(alpha)) => *alpha < 1,
                (b"CA" | b"ca", Value::Real(alpha)) => alpha.value() < 1.0,
                (b"BM", Value::Name(mode)) => !matches!(mode.as_bytes(), b"Normal" | b"Compatible"),
                (b"S", Value::Name(kind)) => kind.as_bytes() == b"Transparency",
                _ => false,
            };
            if forbidden {
                return Ok(true);
            }
            if depth < MAX_NESTING {
                pending.try_push((entry, depth + 1))?;
            }
        }
    }
    Ok(false)
}

/// Returns whether `dict` is an annotation that PDF/UA requires in the
/// structure tree, without the `/StructParent` that would put it there.
fn is_untagged_annotation(dict: &Dict) -> bool {
    let Some(Value::Name(subtype)) = dict.get("Subtype") else {
        return false;
    };
    dict.get("Rect").is_some()
        && !matches!(subtype.as_bytes(), b"Popup")
        && dict.get("StructParent").is_none()
}

fn push_annot_refs(
    items: &pyropdf_store::value::Array,
    out: &mut BudgetVec<'_, u32>,
) -> Result<()> {
    for item in items.iter() {
        if let Value::Reference(reference) = item {
            out.try_push(reference.number())?;
        }
    }
    Ok(())
}

fn page_annot_refs(
    version: &Version,
    page: &Dict,
    out: &mut BudgetVec<'_, u32>,
    budget: &Budget,
) -> Result<()> {
    let Some(annots) = page.get("Annots") else {
        return Ok(());
    };
    let resolved_annots = resolved(version, annots, budget)?;
    if let Some(Value::Array(items)) = resolved_annots.as_deref() {
        push_annot_refs(items, out)?;
    }
    Ok(())
}

/// Returns sorted annotation object numbers that appeared in `/Annots` of
/// edited pages or edited indirect `/Annots` arrays in `opened`, and no longer
/// appear in those containers in `version`.
fn removed_annotations<'b>(
    opened: &Version,
    version: &Version,
    written: &[u32],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u32>> {
    let mut was_listed = BudgetVec::new(budget);
    let mut still_listed = BudgetVec::new(budget);
    for &number in written {
        budget.poll()?;
        let Some(old_val) = object(opened, number, budget)? else {
            continue;
        };
        match &*old_val {
            Value::Dict(old_dict) if old_dict.get("Annots").is_some() => {
                page_annot_refs(opened, old_dict, &mut was_listed, budget)?;
                if let Some(new_val) = object(version, number, budget)?
                    && let Value::Dict(new_dict) = &*new_val
                {
                    page_annot_refs(version, new_dict, &mut still_listed, budget)?;
                }
            }
            Value::Array(old_items) => {
                for item in old_items.iter() {
                    if let Value::Reference(reference) = item
                        && let Some(target) = object(opened, reference.number(), budget)?
                        && let Value::Dict(dict) = &*target
                        && is_annotation_dict(dict)
                    {
                        was_listed.try_push(reference.number())?;
                    }
                }
                if let Some(new_val) = object(version, number, budget)?
                    && let Value::Array(new_items) = &*new_val
                {
                    push_annot_refs(new_items, &mut still_listed)?;
                }
            }
            _ => {}
        }
    }
    let mut kept = still_listed.into_vec();
    kept.sort_unstable();
    kept.dedup();
    let mut removed_vec = was_listed.into_vec();
    removed_vec.sort_unstable();
    removed_vec.dedup();
    let mut out = BudgetVec::new(budget);
    for item in removed_vec {
        if kept.binary_search(&item).is_err() {
            out.try_push(item)?;
        }
    }
    Ok(out)
}

/// Returns whether annotation `number` (`dict`) was removed from its page in
/// `version`: either it left an edited `/Annots` list since `opened`, or its
/// `/P` page no longer lists it in `/Annots`.
fn is_removed_annotation(
    version: &Version,
    removed_annots: &[u32],
    number: u32,
    dict: &Dict,
    budget: &Budget,
) -> Result<bool> {
    if removed_annots.binary_search(&number).is_ok() {
        return Ok(true);
    }
    let Some(Value::Reference(page_ref)) = dict.get("P") else {
        return Ok(false);
    };
    let Some(page_val) = object(version, page_ref.number(), budget)? else {
        return Ok(true);
    };
    let Some(page_dict) = value_dict(&page_val) else {
        return Ok(true);
    };
    let Some(annots) = page_dict.get("Annots") else {
        return Ok(true);
    };
    let resolved_annots = resolved(version, annots, budget)?;
    let Some(Value::Array(items)) = resolved_annots.as_deref() else {
        return Ok(true);
    };
    Ok(!items
        .iter()
        .any(|item| matches!(item, Value::Reference(reference) if reference.number() == number)))
}

#[cfg(test)]
mod tests {
    use proptest::prelude::*;
    use pyropdf_store::budget::DocumentLimits;
    use pyropdf_store::save::FileBase;
    use pyropdf_store::value::{Array, Bytes, Name, PdfString, Stream};
    use pyropdf_store::version::Document;

    use super::*;

    fn build_pdf(objects: &[(u32, &str)], signed_before: Option<u32>) -> Vec<u8> {
        let mut out = b"%PDF-1.7\n".to_vec();
        let mut offsets = std::collections::BTreeMap::new();
        for &(number, raw) in objects {
            offsets.insert(number, out.len());
            let body = if let Some(target) = signed_before {
                let end = offsets.get(&target).copied().unwrap_or(0);
                raw.replace("BYTERANGE", &format!("[0 {end} {end} 0]"))
            } else {
                raw.to_owned()
            };
            out.extend_from_slice(format!("{number} 0 obj\n{body}\nendobj\n").as_bytes());
        }
        let size = offsets.keys().next_back().map_or(1, |max| max + 1);
        let xref = out.len();
        out.extend_from_slice(format!("xref\n0 {size}\n0000000000 65535 f \n").as_bytes());
        for number in 1..size {
            if let Some(&offset) = offsets.get(&number) {
                out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
            } else {
                out.extend_from_slice(b"0000000000 65535 f \n");
            }
        }
        out.extend_from_slice(
            format!("trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n")
                .as_bytes(),
        );
        out
    }

    fn open_pdf(objects: &[(u32, &str)], signed_before: Option<u32>) -> Document {
        let bytes = build_pdf(objects, signed_before);
        let budget = Budget::default();
        let index = Index::open(Bytes::new(bytes), &budget).unwrap();
        Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None)
    }

    fn xmp_stream(properties: &str) -> String {
        let data = format!(
            "<x:xmpmeta xmlns:x=\"adobe:ns:meta/\"><rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\"><rdf:Description {properties}/></rdf:RDF></x:xmpmeta>"
        );
        format!(
            "<< /Type /Metadata /Subtype /XML /Length {} >>\nstream\n{data}\nendstream",
            data.len()
        )
    }

    fn dict_of(entries: &[(&str, Value)]) -> Dict {
        let mut dict = Dict::new();
        for (key, value) in entries {
            dict.insert(Name::new(key.as_bytes()), value.clone());
        }
        dict
    }

    #[test]
    fn signature_coverage_tracks_signed_ranges_object_streams_and_versions() {
        // Object 4 is an `/ObjStm` inside the signed range holding member object 7;
        // object 5 is an unsigned note outside the signed range.
        let stm_body = b"7 0 << /Type /Annot /Subtype /Text >>";
        let prefix = b"7 0 ";
        let stm_obj = format!(
            "<< /Type /ObjStm /N 1 /First {} /Length {} >>\nstream\n{}\nendstream",
            prefix.len(),
            stm_body.len(),
            std::str::from_utf8(stm_body).unwrap()
        );
        // Build an xref stream PDF so `Index` indexes object 7 as `Location::Member { stream: 4, .. }`.
        let mut pdf = b"%PDF-1.7\n".to_vec();
        let mut offsets = [0_usize; 9];
        let write_obj = |pdf: &mut Vec<u8>, offsets: &mut [usize; 9], num: usize, body: &str| {
            offsets[num] = pdf.len();
            pdf.extend_from_slice(format!("{num} 0 obj\n{body}\nendobj\n").as_bytes());
        };
        write_obj(
            &mut pdf,
            &mut offsets,
            1,
            "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [6 0 R] >> >>",
        );
        write_obj(
            &mut pdf,
            &mut offsets,
            2,
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        );
        write_obj(
            &mut pdf,
            &mut offsets,
            3,
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        );
        write_obj(&mut pdf, &mut offsets, 4, &stm_obj);
        let signed_end = pdf.len();
        write_obj(&mut pdf, &mut offsets, 5, "<< /Note (unsigned) >>");
        write_obj(
            &mut pdf,
            &mut offsets,
            6,
            "<< /FT /Sig /T (Parent) /Kids [8 0 R] >>",
        );
        let sig_body = format!(
            "<< /T <FEFF004100700070> /V << /Type /Sig /ByteRange [0 {signed_end} {signed_end} 0] /Contents <00> >> >>"
        );
        write_obj(&mut pdf, &mut offsets, 8, &sig_body);

        // Xref stream at object 9: W [1 4 2], entries 0..=9.
        let xref_offset = pdf.len();
        let mut xref_data = Vec::new();
        let push_entry = |buf: &mut Vec<u8>, kind: u8, f1: u32, f2: u16| {
            buf.push(kind);
            buf.extend_from_slice(&f1.to_be_bytes());
            buf.extend_from_slice(&f2.to_be_bytes());
        };
        push_entry(&mut xref_data, 0, 0, 65535);
        for (num, &off) in offsets.iter().enumerate().skip(1) {
            if num == 7 {
                push_entry(&mut xref_data, 2, 4, 0);
            } else {
                push_entry(&mut xref_data, 1, u32::try_from(off).unwrap(), 0);
            }
        }
        push_entry(&mut xref_data, 1, u32::try_from(xref_offset).unwrap(), 0);
        pdf.extend_from_slice(
            format!(
                "9 0 obj\n<< /Type /XRef /Size 10 /Root 1 0 R /W [1 4 2] /Length {} >>\nstream\n",
                xref_data.len()
            )
            .as_bytes(),
        );
        pdf.extend_from_slice(&xref_data);
        pdf.extend_from_slice(
            format!("\nendstream\nendobj\nstartxref\n{xref_offset}\n%%EOF\n").as_bytes(),
        );

        let budget = Budget::default();
        let index = Index::open(Bytes::new(pdf), &budget).unwrap();
        let doc = Document::new(
            Arc::new(FileBase::new(index, &budget).unwrap()),
            Some(1 << 20),
        );

        // Unedited incremental report is empty; non-incremental save invalidates the signature.
        let initial = report(&doc.current(), true, &budget).unwrap();
        assert!(initial.is_empty());

        let full_save = report(&doc.current(), false, &budget).unwrap();
        assert_eq!(
            full_save.signatures,
            vec![SignatureChange {
                field: "Parent.App".to_owned(),
                xref: 8,
                reason: "a save that is not incremental rewrites the signed bytes".to_owned(),
                objects: Vec::new(),
            }]
        );

        // Editing unsigned object 5 and allocating a new object leaves incremental signature intact.
        let mut tx = doc.begin();
        tx.write(
            5,
            &Value::Dict(dict_of(&[(
                "Note",
                Value::String(PdfString::literal(b"edited".to_vec())),
            )])),
            &budget,
        )
        .unwrap();
        let fresh = tx.allocate().unwrap();
        tx.write(fresh.number(), &Value::Dict(Dict::new()), &budget)
            .unwrap();
        tx.commit(&budget).unwrap();
        assert!(report(&doc.current(), true, &budget).unwrap().is_empty());

        // Editing page 3 (direct offset) and object 7 (member of signed `/ObjStm` 4), plus freeing
        // page 3 in an open draft snapshot, reports both signed objects; undo restores validity.
        let mut tx = doc.begin();
        tx.write(
            3,
            &Value::Dict(dict_of(&[("Rotate", Value::Integer(90))])),
            &budget,
        )
        .unwrap();
        tx.write(
            7,
            &Value::Dict(dict_of(&[(
                "Subtype",
                Value::Name(Name::new(b"Note".as_slice())),
            )])),
            &budget,
        )
        .unwrap();
        // Draft snapshot inside open transaction sees uncommitted edits.
        let draft_report = report(&tx.snapshot(), true, &budget).unwrap();
        assert_eq!(draft_report.signatures.len(), 1);
        assert_eq!(draft_report.signatures[0].field, "Parent.App");
        assert_eq!(draft_report.signatures[0].xref, 8);
        assert_eq!(draft_report.signatures[0].objects, vec![3, 7]);

        tx.commit(&budget).unwrap();
        assert_eq!(
            report(&doc.current(), true, &budget).unwrap().signatures[0].objects,
            vec![3, 7]
        );

        doc.undo().unwrap();
        assert!(report(&doc.current(), true, &budget).unwrap().is_empty());
    }

    #[test]
    fn perms_signatures_deduplicate_field_values_and_field_tree_bounds_hold() {
        // Object 7 is shared by field 5 and `/Perms /DocMDP` (deduplicated); object 8 is only in `/Perms /UR3`.
        let doc = open_pdf(
            &[
                (
                    1,
                    "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [5 0 R] >> /Perms << /DocMDP 7 0 R /UR3 8 0 R >> >>",
                ),
                (2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
                (3, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"),
                (4, "<< /After true >>"),
                (5, "<< /FT /Sig /T (MainSig) /Kids [5 0 R] /V 7 0 R >>"),
                (7, "<< /Type /Sig /ByteRange BYTERANGE /Contents <00> >>"),
                (8, "<< /Type /Sig /ByteRange BYTERANGE /Contents <00> >>"),
            ],
            Some(4),
        );
        let budget = Budget::default();
        let mut tx = doc.begin();
        tx.free(3, &budget).unwrap();
        tx.commit(&budget).unwrap();

        let found = report(&doc.current(), true, &budget).unwrap();
        assert_eq!(
            found
                .signatures
                .iter()
                .map(|s| (s.field.as_str(), s.xref, s.objects.as_slice()))
                .collect::<Vec<_>>(),
            vec![("MainSig", 5, &[3_u32][..]), ("Perms/UR3", 8, &[3_u32][..]),]
        );

        // Exceeding `MAX_DEPTH` refuses with `Error::Pdf`.
        let mut deep_objs: Vec<(u32, String)> = vec![
            (
                1,
                "<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [4 0 R] >> >>".to_owned(),
            ),
            (2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>".to_owned()),
            (
                3,
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>".to_owned(),
            ),
        ];
        for depth in 0..MAX_DEPTH {
            let num = 4 + u32::try_from(depth).unwrap();
            let next = num + 1;
            deep_objs.push((num, format!("<< /Kids [{next} 0 R] >>")));
        }
        let leaf = 4 + u32::try_from(MAX_DEPTH).unwrap();
        deep_objs.push((leaf, "<< /FT /Sig /T (Leaf) >>".to_owned()));
        let deep_refs: Vec<(u32, &str)> = deep_objs.iter().map(|(n, s)| (*n, s.as_str())).collect();
        let deep_doc = open_pdf(&deep_refs, None);
        let err = report(&deep_doc.current(), false, &budget).unwrap_err();
        assert!(matches!(err, Error::Pdf(ref msg) if msg.contains("64-level")));
    }

    #[test]
    fn metadata_claims_detect_pdfa_and_pdfua_violations() {
        let meta = xmp_stream(
            "notpdfaid:part='3' pdfaid:part=\"1\" pdfaid:conformance=\"b\" pdfuaid:part='1'",
        );
        let doc = open_pdf(
            &[
                (1, "<< /Type /Catalog /Pages 2 0 R /Metadata 4 0 R >>"),
                (2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
                (3, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"),
                (4, &meta),
            ],
            None,
        );
        let budget = Budget::default();
        let mut tx = doc.begin();

        // Object 10: transparency + embedded file spec (breaks both PDF/A-1 reasons).
        let both = tx.allocate().unwrap().number();
        tx.write(
            both,
            &Value::Dict(dict_of(&[
                ("ca", Value::from(0.5)),
                ("EF", Value::Dict(Dict::new())),
            ])),
            &budget,
        )
        .unwrap();

        // Object 11: untagged annotation (breaks PDF/UA-1).
        let untagged = tx.allocate().unwrap().number();
        let mut rect = Array::new();
        for v in [0, 0, 10, 10] {
            rect.push(Value::Integer(v));
        }
        tx.write(
            untagged,
            &Value::Dict(dict_of(&[
                ("Subtype", Value::Name(Name::new(b"Text".as_slice()))),
                ("Rect", Value::Array(rect.clone())),
            ])),
            &budget,
        )
        .unwrap();

        // Object 12: tagged annotation and Popup annotation (allowed by PDF/UA-1).
        let tagged = tx.allocate().unwrap().number();
        tx.write(
            tagged,
            &Value::Dict(dict_of(&[
                ("Subtype", Value::Name(Name::new(b"Text".as_slice()))),
                ("Rect", Value::Array(rect.clone())),
                ("StructParent", Value::Integer(0)),
            ])),
            &budget,
        )
        .unwrap();
        let popup = tx.allocate().unwrap().number();
        tx.write(
            popup,
            &Value::Dict(dict_of(&[
                ("Subtype", Value::Name(Name::new(b"Popup".as_slice()))),
                ("Rect", Value::Array(rect)),
            ])),
            &budget,
        )
        .unwrap();
        tx.commit(&budget).unwrap();

        let found = report(&doc.current(), true, &budget).unwrap();
        assert_eq!(
            found.claims,
            vec![
                ClaimChange {
                    claim: "PDF/A-1B".to_owned(),
                    reason: "the edits add transparency, which PDF/A-1 forbids".to_owned(),
                    objects: vec![both],
                },
                ClaimChange {
                    claim: "PDF/A-1B".to_owned(),
                    reason: "the edits add an embedded file, which PDF/A-1 forbids".to_owned(),
                    objects: vec![both],
                },
                ClaimChange {
                    claim: "PDF/UA-1".to_owned(),
                    reason: "the edits add an annotation that is not in the structure tree"
                        .to_owned(),
                    objects: vec![untagged],
                },
            ]
        );
    }

    #[test]
    fn embedded_file_claims_and_budget_refusals_are_enforced() {
        let meta = xmp_stream("pdfaid:part=\"2\" pdfaid:conformance=\"B\"");
        let doc = open_pdf(
            &[
                (1, "<< /Type /Catalog /Pages 2 0 R /Metadata 4 0 R >>"),
                (2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
                (3, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"),
                (4, &meta),
            ],
            None,
        );
        let budget = Budget::default();
        let mut tx = doc.begin();

        let ef_dict = dict_of(&[("Type", Value::Name(Name::new(b"EmbeddedFile".as_slice())))]);
        let allowed = tx.allocate().unwrap().number();
        tx.write(
            allowed,
            &Value::Stream(Box::new(Stream::new(
                ef_dict.clone(),
                Bytes::new(b"%PDF-1.7\n<pdfaid:part>2</pdfaid:part>".to_vec()),
            ))),
            &budget,
        )
        .unwrap();

        let disallowed = tx.allocate().unwrap().number();
        tx.write(
            disallowed,
            &Value::Stream(Box::new(Stream::new(
                ef_dict,
                Bytes::new(b"%PDF-1.7\n<pdfaid:part>4</pdfaid:part>".to_vec()),
            ))),
            &budget,
        )
        .unwrap();
        tx.commit(&budget).unwrap();

        let found = report(&doc.current(), true, &budget).unwrap();
        assert_eq!(found.claims.len(), 1);
        assert_eq!(found.claims[0].claim, "PDF/A-2B");
        assert_eq!(found.claims[0].objects, vec![disallowed]);

        // Tight memory budget refuses cleanly with `LimitCode::MemorySize`.
        let tight =
            Budget::new(&DocumentLimits::default()).with_limit(LimitCode::MemorySize, Some(8));
        let err = report(&doc.current(), true, &tight).unwrap_err();
        assert_eq!(err.limit_code(), Some(LimitCode::MemorySize));

        // Corrupt FlateDecode stream on an EmbeddedFile counts as non-PDF/A rather than failing,
        // whereas a `DecompressedSize` limit refusal surfaces as `Error::Store`.
        let mut flate_dict = dict_of(&[
            ("Type", Value::Name(Name::new(b"EmbeddedFile".as_slice()))),
            ("Filter", Value::Name(Name::new(b"FlateDecode".as_slice()))),
        ]);
        let mut tx2 = doc.begin();
        let corrupt = tx2.allocate().unwrap().number();
        tx2.write(
            corrupt,
            &Value::Stream(Box::new(Stream::new(
                flate_dict.clone(),
                Bytes::new(b"not-zlib".to_vec()),
            ))),
            &budget,
        )
        .unwrap();
        tx2.commit(&budget).unwrap();
        let with_corrupt = report(&doc.current(), true, &budget).unwrap();
        assert_eq!(with_corrupt.claims[0].objects, vec![disallowed, corrupt]);

        // Now replace `corrupt` with a valid Flate stream and refuse via `DecompressedSize`.
        let mut encoder =
            flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
        std::io::Write::write_all(&mut encoder, &[b'x'; 256]).unwrap();
        let compressed = encoder.finish().unwrap();
        flate_dict.insert(
            Name::new(b"Length".as_slice()),
            Value::Integer(i64::try_from(compressed.len()).unwrap()),
        );
        let mut tx3 = doc.begin();
        tx3.write(
            corrupt,
            &Value::Stream(Box::new(Stream::new(flate_dict, Bytes::new(compressed)))),
            &budget,
        )
        .unwrap();
        tx3.commit(&budget).unwrap();
        let stream_tight = Budget::new(&DocumentLimits::default())
            .with_limit(LimitCode::DecompressedSize, Some(64));
        let err = report(&doc.current(), true, &stream_tight).unwrap_err();
        assert_eq!(err.limit_code(), Some(LimitCode::DecompressedSize));
        assert!(matches!(err, Error::Store(_)));
    }

    proptest! {
        #[test]
        fn xmp_property_parser_respects_boundaries_and_never_panics(
            noise in prop::collection::vec(any::<u8>(), 0..128),
            part in 1_u32..5,
            use_element in any::<bool>(),
            single_quote in any::<bool>(),
        ) {
            // Arbitrary bytes never panic `read_claims`.
            let _ = read_claims(&noise);

            // Prefixed imposter followed by real property extracts the real value.
            let snippet = if use_element {
                format!("xxpdfaid:part=\"9\" <pdfaid:part> {part} </pdfaid:part>")
            } else if single_quote {
                format!("other:pdfaid:part='9' pdfaid:part = ' {part} '")
            } else {
                format!("notpdfaid:part=\"9\" pdfaid:part = \" {part} \"")
            };
            prop_assert_eq!(
                property(snippet.as_bytes(), b"pdfaid:part").and_then(number),
                Some(part)
            );
        }
    }
}
