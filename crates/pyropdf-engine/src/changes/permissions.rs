//! Evaluate the proposed append against the transforms of each signed revision.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, try_insert_map, try_insert_set};
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Dict, Value};
use pyropdf_store::version::{ChangeSet, Document, Version};

use super::{
    Error, Result, Signature, byte_ranges, catalog, object, resolved, value_dict, walk_fields,
};
use crate::annotations::version::is_annotation_dict;
use crate::forms::text_string;

/// One signed transform's verdict for a proposed edit.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SignaturePermission {
    /// Name of the signature field or catalog permission.
    pub signature: String,
    /// Object number of that signature field or catalog permission.
    pub xref: u32,
    /// DocMDP permission level or FieldMDP action.
    pub transform: &'static str,
    /// Kind of proposed edit.
    pub edit: &'static str,
    /// Object that the edit writes, or zero for the trailer.
    pub object: u32,
    /// Fully qualified name of an affected form field.
    pub field: Option<String>,
    /// Whether the transform permits this edit, or None if unclassified.
    pub permitted: Option<bool>,
    /// Explanation of the verdict.
    pub reason: &'static str,
}

enum Transform {
    Doc {
        level: i64,
        field_lock: bool,
    },
    Fields {
        action: &'static str,
        names: HashSet<String>,
    },
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Fill,
    Sign,
    Annotation,
    PageOrMetadata,
    Other,
    Unknown,
}

impl Kind {
    fn label(self) -> &'static str {
        match self {
            Self::Fill => "form filling",
            Self::Sign => "signing",
            Self::Annotation => "annotation",
            Self::PageOrMetadata | Self::Other => "document change",
            Self::Unknown => "unclassified change",
        }
    }
}

struct Edit {
    kind: Kind,
    object: u32,
    field: Option<String>,
}

impl Transform {
    fn verdict(&self, edit: &Edit) -> (&'static str, Option<bool>, &'static str) {
        match self {
            Self::Doc { level, field_lock } => {
                let transform = match (field_lock, level) {
                    (true, 1) => "Field lock P=1",
                    (true, 2) => "Field lock P=2",
                    (true, _) => "Field lock P=3",
                    (false, 1) => "DocMDP P=1",
                    (false, 2) => "DocMDP P=2",
                    (false, _) => "DocMDP P=3",
                };
                let permitted = match edit.kind {
                    Kind::Fill | Kind::Sign => Some(*level >= 2),
                    Kind::Annotation => Some(*level == 3),
                    Kind::PageOrMetadata | Kind::Other => Some(false),
                    Kind::Unknown => None,
                };
                let reason = match permitted {
                    Some(true) if *field_lock => {
                        "the signature field permission allows this kind of edit"
                    }
                    Some(false) if *field_lock => {
                        "the signature field permission forbids this kind of edit"
                    }
                    Some(true) => "the certification permits this kind of edit",
                    Some(false) => "the certification forbids this kind of edit",
                    None => "the edit needs further modification-policy review",
                };
                (transform, permitted, reason)
            }
            Self::Fields { action, names } => {
                if let Some(field) = &edit.field {
                    let listed = names.contains(field)
                        || field
                            .match_indices('.')
                            .any(|(dot, _)| names.contains(&field[..dot]));
                    let locked = match *action {
                        "FieldMDP All" => true,
                        "FieldMDP Include" => listed,
                        _ => !listed,
                    };
                    let reason = if locked {
                        "the signed lock covers this field"
                    } else {
                        "the field is outside the signed lock"
                    };
                    (action, Some(!locked), reason)
                } else if edit.kind == Kind::PageOrMetadata {
                    (
                        action,
                        Some(true),
                        "the document edit changes no form field",
                    )
                } else if edit.kind == Kind::Sign {
                    (
                        action,
                        Some(true),
                        "this list only attaches new signature fields evaluated separately",
                    )
                } else if edit.kind == Kind::Annotation {
                    (
                        action,
                        Some(true),
                        "the annotation edit changes no form field",
                    )
                } else {
                    (
                        action,
                        None,
                        "the edit has no identified field to compare with the signed lock",
                    )
                }
            }
        }
    }
}

fn owned(text: &str, budget: &Budget) -> Result<String> {
    budget.charge(LimitCode::MemorySize, text.len() as u64)?;
    Ok(text.to_owned())
}

fn name(value: Option<&Value>) -> Option<&[u8]> {
    match value {
        Some(Value::Name(name)) => Some(name.as_bytes()),
        _ => None,
    }
}

fn entry<'a>(
    version: &'a Version,
    dict: &'a Dict,
    key: &str,
    budget: &Budget,
) -> Result<Option<super::Resolved<'a>>> {
    match dict.get(key) {
        Some(value) => resolved(version, value, budget),
        None => Ok(None),
    }
}

// Match the signature-structure reader's transform and field-name bounds.
const MAX_TRANSFORMS: usize = 64;
const MAX_LOCK_FIELDS: usize = 4096;

fn check_count(count: usize, limit: usize) -> Result<()> {
    if count > limit {
        return Err(LimitExceeded::over(LimitCode::ObjectCount, limit as u64, count as u64).into());
    }
    Ok(())
}

fn transforms(
    version: &Version,
    sig: &Dict,
    field_number: u32,
    budget: &Budget,
) -> Result<Vec<Transform>> {
    let mut result = BudgetVec::new(budget);
    let refs = entry(version, sig, "Reference", budget)?;
    if let Some(Value::Array(refs)) = refs.as_deref() {
        check_count(refs.len(), MAX_TRANSFORMS)?;
        for value in refs.iter() {
            budget.poll()?;
            let reference = resolved(version, value, budget)?;
            let Some(reference) = reference.as_deref().and_then(value_dict) else {
                continue;
            };
            let params = entry(version, reference, "TransformParams", budget)?;
            let dict = params.as_deref().and_then(value_dict);
            match name(reference.get("TransformMethod")) {
                Some(b"DocMDP") => {
                    let level = match dict.and_then(|dict| dict.get("P")) {
                        None if params.is_none() || dict.is_some() => 2,
                        Some(Value::Integer(level @ 1..=3)) => *level,
                        _ => return Err(Error::Pdf("invalid DocMDP permission".into())),
                    };
                    result.try_push(Transform::Doc {
                        level,
                        field_lock: false,
                    })?;
                }
                Some(b"FieldMDP") => {
                    let Some(dict) = dict else {
                        return Err(Error::Pdf("missing FieldMDP transform parameters".into()));
                    };
                    let action = match name(dict.get("Action")) {
                        Some(b"All") => "FieldMDP All",
                        Some(b"Include") => "FieldMDP Include",
                        Some(b"Exclude") => "FieldMDP Exclude",
                        _ => return Err(Error::Pdf("invalid FieldMDP action".into())),
                    };
                    let mut names = HashSet::new();
                    if action != "FieldMDP All" {
                        let fields = entry(version, dict, "Fields", budget)?;
                        let Some(Value::Array(fields)) = fields.as_deref() else {
                            return Err(Error::Pdf("missing FieldMDP field names".into()));
                        };
                        check_count(fields.len(), MAX_LOCK_FIELDS)?;
                        for field in fields.iter() {
                            budget.poll()?;
                            let field = resolved(version, field, budget)?;
                            let Some(Value::String(field)) = field.as_deref() else {
                                return Err(Error::Pdf("invalid FieldMDP field name".into()));
                            };
                            budget.charge(
                                LimitCode::MemorySize,
                                (field.as_bytes().len() as u64).saturating_mul(3),
                            )?;
                            try_insert_set(&mut names, text_string(field.as_bytes()), budget)?;
                        }
                    }
                    result.try_push(Transform::Fields { action, names })?;
                }
                _ => {}
            }
        }
    }
    if field_number != 0
        && let Some(level) = field_permission(version, field_number, budget)?
    {
        result.try_push(Transform::Doc {
            level,
            field_lock: true,
        })?;
    }
    Ok(result.into_vec())
}

/// Table 236 /P applies to the whole document, separately from the field names in /Fields.
fn field_permission(version: &Version, field: u32, budget: &Budget) -> Result<Option<i64>> {
    let field = object(version, field, budget)?;
    let Some(field) = field.as_deref().and_then(value_dict) else {
        return Ok(None);
    };
    let lock = entry(version, field, "Lock", budget)?;
    let permission = lock
        .as_deref()
        .and_then(value_dict)
        .and_then(|lock| lock.get("P"));
    match permission {
        None => Ok(None),
        Some(Value::Integer(level @ 1..=3)) => Ok(Some(*level)),
        Some(Value::Real(real)) => [1, 2, 3]
            .into_iter()
            .find(|level| real.value() == f64::from(*level))
            .map(|level| Some(i64::from(level)))
            .ok_or_else(|| Error::Pdf("invalid signature field lock permission".into())),
        _ => Err(Error::Pdf("invalid signature field lock permission".into())),
    }
}

fn policy_in(version: &Version, signature: &Signature, budget: &Budget) -> Result<Vec<Transform>> {
    let from = |dict: Option<&Dict>| match dict {
        Some(dict) if byte_ranges(dict) == Some(signature.ranges) => {
            transforms(version, dict, signature.field, budget)
        }
        _ => Ok(Vec::new()),
    };
    if let Some(number) = signature.value {
        let value = object(version, number, budget)?;
        return from(value.as_deref().and_then(value_dict));
    }
    if signature.field != 0 {
        let field = object(version, signature.field, budget)?;
        return from(
            field
                .as_deref()
                .and_then(value_dict)
                .and_then(|dict| dict.get("V"))
                .and_then(value_dict),
        );
    }
    let root = catalog(version, budget)?;
    let Some(root) = root.as_deref().and_then(value_dict) else {
        return Ok(Vec::new());
    };
    let perms = entry(version, root, "Perms", budget)?;
    from(
        perms
            .as_deref()
            .and_then(value_dict)
            .and_then(|dict| dict.get(signature.name.trim_start_matches("Perms/")))
            .and_then(value_dict),
    )
}

/// Resolve indirect transform parameters in the revision whose bytes the signature covers.
fn signed_policy(
    opened: &Version,
    signature: &Signature,
    budget: &Budget,
) -> Result<Vec<Transform>> {
    let Some(index) = opened.base().file() else {
        return Ok(Vec::new());
    };
    let (start, len) = signature.ranges[1];
    let Some(end) = start
        .checked_add(len)
        .and_then(|end| usize::try_from(end).ok())
    else {
        return Ok(Vec::new());
    };
    for (at, revision) in index.revisions().iter().enumerate() {
        budget.poll()?;
        if let Some(trailing) = revision.end().and_then(|n| index.bytes().get(end..n))
            && trailing.iter().all(|b| matches!(b, b'\r' | b'\n'))
        {
            if at + 1 == index.revisions().len() {
                return policy_in(opened, signature, budget);
            }
            if let Some(base) = FileBase::open_revision(index, at, budget)? {
                return policy_in(
                    &Document::new(Arc::new(base), Some(0)).current(),
                    signature,
                    budget,
                );
            }
        }
    }
    Ok(Vec::new())
}

pub(super) fn analyze(
    opened: &Version,
    version: &Version,
    signatures: &[Signature],
    changes: &ChangeSet,
    budget: &Budget,
) -> Result<Vec<SignaturePermission>> {
    let mut result = BudgetVec::new(budget);
    let mut edits = None;
    for signature in signatures {
        for transform in signed_policy(opened, signature, budget)? {
            if edits.is_none() {
                edits = Some(classify(opened, version, changes, budget)?);
            }
            for edit in edits.as_deref().unwrap_or_default() {
                budget.poll()?;
                let (transform, permitted, reason) = transform.verdict(edit);
                check_count(result.len() + 1, super::MAX_NODES * MAX_TRANSFORMS)?;
                result.try_push(SignaturePermission {
                    signature: owned(&signature.name, budget)?,
                    xref: signature.field,
                    edit: edit.kind.label(),
                    object: edit.object,
                    field: edit
                        .field
                        .as_ref()
                        .map(|name| owned(name, budget))
                        .transpose()?,
                    transform,
                    permitted,
                    reason,
                })?;
            }
        }
    }
    Ok(result.into_vec())
}

/// Store names only for field or widget objects in the proposed change set.
fn fields(
    version: &Version,
    changes: &ChangeSet,
    budget: &Budget,
) -> Result<HashMap<u32, (String, bool)>> {
    let mut fields = HashMap::new();
    let root = catalog(version, budget)?;
    if let Some(root) = root.as_deref().and_then(value_dict) {
        walk_fields(version, root, budget, |number, name, signing, _| {
            if changes.contains(number) {
                try_insert_map(&mut fields, number, (owned(name, budget)?, signing), budget)?;
            }
            Ok(())
        })?;
    }
    Ok(fields)
}

fn only_keys(before: &Dict, after: &Dict, allowed: &[&[u8]], budget: &Budget) -> Result<bool> {
    for key in before.keys().chain(after.keys()) {
        budget.poll()?;
        let key = key.as_bytes();
        if !allowed.contains(&key) && before.get(key) != after.get(key) {
            return Ok(false);
        }
    }
    Ok(true)
}

fn collect_references(
    value: &Value,
    out: &mut BudgetVec<'_, u32>,
    budget: &Budget,
    depth: usize,
) -> Result<()> {
    budget.poll()?;
    if depth > super::MAX_NESTING {
        return Err(Error::Pdf(
            "appearance resources exceed change report nesting limit".into(),
        ));
    }
    match value {
        Value::Reference(r) => out.try_push(r.number())?,
        Value::Array(items) => {
            for item in items.iter() {
                collect_references(item, out, budget, depth + 1)?;
            }
        }
        _ => {
            if let Some(dict) = value_dict(value) {
                for (_, item) in dict.iter() {
                    collect_references(item, out, budget, depth + 1)?;
                }
            }
        }
    }
    Ok(())
}

struct Classification<'a> {
    opened: &'a Version,
    version: &'a Version,
    budget: &'a Budget,
    fields: &'a HashMap<u32, (String, bool)>,
    /// New objects reachable from a classified edit's appearance or signature value.
    supporting: HashSet<u32>,
}

impl Classification<'_> {
    fn support(&mut self, root: Option<&Value>) -> Result<()> {
        let mut pending = BudgetVec::new(self.budget);
        if let Some(root) = root {
            collect_references(root, &mut pending, self.budget, 0)?;
        }
        while let Some(number) = pending.pop() {
            self.budget.poll()?;
            if self.opened.generation(number).is_none()
                && try_insert_set(&mut self.supporting, number, self.budget)?
                && let Some(value) = object(self.version, number, self.budget)?
            {
                collect_references(&value, &mut pending, self.budget, 0)?;
            }
        }
        Ok(())
    }

    fn field_edit(
        &mut self,
        before: Option<&Dict>,
        after: Option<&Dict>,
        signing: bool,
    ) -> Result<Kind> {
        let Some(after) = after else {
            return Ok(Kind::Other);
        };
        if signing {
            let value = entry(self.version, after, "V", self.budget)?;
            if value.as_deref().and_then(value_dict).is_some_and(|v| {
                name(v.get("Type")) == Some(b"Sig")
                    && v.get("Contents").is_some()
                    && byte_ranges(v).is_some()
            }) && before.is_none_or(|old| matches!(old.get("V"), None | Some(Value::Null)))
                && match before {
                    Some(old) => only_keys(old, after, &[b"V", b"AP", b"AS"], self.budget)?,
                    None => true,
                }
            {
                self.support(after.get("V"))?;
                self.support(after.get("AP"))?;
                return Ok(Kind::Sign);
            }
        } else if let Some(before) = before
            && only_keys(before, after, &[b"V", b"I", b"AS", b"AP"], self.budget)?
        {
            self.support(after.get("AP"))?;
            return Ok(Kind::Fill);
        }
        Ok(Kind::Other)
    }

    fn memberships(
        &self,
        old: Option<&Value>,
        new: Option<&Value>,
        annotations: bool,
    ) -> Result<Kind> {
        let old = old
            .map(|v| resolved(self.opened, v, self.budget))
            .transpose()?
            .flatten();
        let new = new
            .map(|v| resolved(self.version, v, self.budget))
            .transpose()?
            .flatten();
        fn items(value: Option<&Value>) -> Option<&[Value]> {
            match value {
                None => Some(&[]),
                Some(Value::Array(array)) => Some(array.as_slice()),
                _ => None,
            }
        }
        let (Some(old), Some(new)) = (items(old.as_deref()), items(new.as_deref())) else {
            return Ok(Kind::Unknown);
        };
        if let Some(added) = new.strip_prefix(old)
            && !added.is_empty()
        {
            let mut all_signatures = true;
            for v in added {
                self.budget.poll()?;
                let value = resolved(self.version, v, self.budget)?;
                let Some(dict) = value.as_deref().and_then(value_dict) else {
                    return Ok(Kind::Unknown);
                };
                all_signatures &= matches!(v, Value::Reference(r)
                    if self.opened.generation(r.number()).is_none()
                    && self.fields.get(&r.number()).is_some_and(|(_, signing)| *signing))
                    && name(dict.get("FT")) == Some(b"Sig")
                    && dict.get("V").is_some();
            }
            if all_signatures {
                return Ok(Kind::Sign);
            }
        }
        if annotations {
            // Widget membership changes can also add or remove a form field.
            // Preserve their relative order and identity when classifying markup edits.
            let mut previous_widgets = BudgetVec::new(self.budget);
            let mut current_widgets = BudgetVec::new(self.budget);
            for (version, list, widgets) in [
                (self.opened, old, &mut previous_widgets),
                (self.version, new, &mut current_widgets),
            ] {
                for item in list {
                    self.budget.poll()?;
                    let target = resolved(version, item, self.budget)?;
                    let Some(dict) = target
                        .as_deref()
                        .and_then(value_dict)
                        .filter(|d| is_annotation_dict(d))
                    else {
                        return Ok(Kind::Unknown);
                    };
                    if name(dict.get("Subtype")) == Some(b"Widget") {
                        let Value::Reference(reference) = item else {
                            return Ok(Kind::Unknown);
                        };
                        widgets.try_push(*reference)?;
                    }
                }
            }
            if previous_widgets.as_slice() == current_widgets.as_slice() {
                return Ok(Kind::Annotation);
            }
        }
        Ok(Kind::Unknown)
    }

    fn other_edit(&mut self, old: Option<&Value>, new: Option<&Value>) -> Result<Kind> {
        let before = old.and_then(value_dict);
        let after = new.and_then(value_dict);
        if before
            .or(after)
            .is_some_and(|d| is_annotation_dict(d) && name(d.get("Subtype")) != Some(b"Widget"))
            && after
                .is_none_or(|d| is_annotation_dict(d) && name(d.get("Subtype")) != Some(b"Widget"))
            && new.is_none_or(|v| matches!(v, Value::Dict(_)))
        {
            self.support(after.and_then(|d| d.get("AP")))?;
            return Ok(Kind::Annotation);
        }
        if let (Some(before), Some(after)) = (before, after) {
            if name(before.get("Type")) == Some(b"Page") {
                if only_keys(before, after, &[b"Annots"], self.budget)? {
                    return self.memberships(before.get("Annots"), after.get("Annots"), true);
                }
                if only_keys(
                    before,
                    after,
                    &[
                        b"Rotate",
                        b"MediaBox",
                        b"CropBox",
                        b"Contents",
                        b"Resources",
                    ],
                    self.budget,
                )? {
                    return Ok(Kind::PageOrMetadata);
                }
                return Ok(Kind::Other);
            }
            if before.get("Fields").is_some()
                && only_keys(before, after, &[b"Fields"], self.budget)?
            {
                return self.memberships(before.get("Fields"), after.get("Fields"), false);
            }
            if name(before.get("Type")) == Some(b"Catalog")
                && only_keys(before, after, &[b"Lang"], self.budget)?
            {
                return Ok(Kind::PageOrMetadata);
            }
        }
        Ok(Kind::Unknown)
    }
}

fn classify(
    opened: &Version,
    version: &Version,
    changes: &ChangeSet,
    budget: &Budget,
) -> Result<Vec<Edit>> {
    let mut result = BudgetVec::new(budget);
    let before_fields = fields(opened, changes, budget)?;
    let after_fields = fields(version, changes, budget)?;
    let mut classification = Classification {
        opened,
        version,
        budget,
        fields: &after_fields,
        supporting: HashSet::new(),
    };
    for &number in changes.objects() {
        budget.poll()?;
        let old = object(opened, number, budget)?;
        let new = object(version, number, budget)?;
        if old == new {
            continue;
        }
        let (kind, field) = if let Some((name, signing)) = before_fields
            .get(&number)
            .or_else(|| after_fields.get(&number))
        {
            let kind = classification.field_edit(
                old.as_deref().and_then(value_dict),
                new.as_deref().and_then(value_dict),
                *signing,
            )?;
            (kind, Some(owned(name, budget)?))
        } else {
            (
                classification.other_edit(old.as_deref(), new.as_deref())?,
                None,
            )
        };
        result.try_push(Edit {
            kind,
            field,
            object: number,
        })?;
    }
    if changes.trailer() && opened.trailer() != version.trailer() {
        result.try_push(Edit {
            kind: Kind::Unknown,
            field: None,
            object: 0,
        })?;
    }
    let mut edits = result.into_vec();
    edits.retain(|edit| {
        edit.kind != Kind::Unknown || !classification.supporting.contains(&edit.object)
    });
    Ok(edits)
}
