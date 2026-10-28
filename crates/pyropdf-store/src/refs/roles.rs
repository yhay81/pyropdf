//! The roles of references: what a reference means to the object that
//! holds it, which decides the rule that a structural edit runs for it
//! (DESIGN.md section 8).
//!
//! Each role has a hand-written matcher of the referrer's value and the
//! path to the reference. The matchers cover the roles that structural
//! edits of pages need; roles derived from the Arlington model come later.

use crate::value::{Dict, Value};

use super::Step;

/// What a reference means to the object that holds it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum Role {
    /// A page or a node of the page tree, in a node's `/Kids`.
    PageTreeKid,
    /// The parent node of a page or a node, its `/Parent`.
    PageTreeParent,
    /// An annotation of a page, in its `/Annots`.
    PageAnnotation,
    /// The page of an annotation, its `/P`.
    AnnotationPage,
    /// The page of an explicit destination, the first item of its array.
    Destination,
    /// A link between outline items: `/First`, `/Last`, `/Next`, `/Prev`,
    /// or `/Parent` of an item or of the outline root.
    OutlineLink,
    /// The parent of a structure element, its `/P`.
    StructureParent,
    /// A kid of a structure element or of the structure tree root, in its
    /// `/K`.
    StructureKid,
    /// The page of a structure element or of a marked-content or object
    /// reference, its `/Pg`.
    StructurePage,
    /// The object of an object reference, its `/Obj`.
    StructureObject,
    /// A kid of a name tree or number tree node, in its `/Kids`.
    TreeKid,
    /// A value of a name tree node, in its `/Names`.
    NameTreeValue,
    /// A value of a number tree node, in its `/Nums`, or in an array that is
    /// one, as the structure parent tree holds.
    NumberTreeValue,
}

/// A matcher: the role that a referrer whose value is the dictionary gives
/// a reference at the path, if it is its role.
type Matcher = fn(&Dict, &[Step]) -> Option<Role>;

/// The matchers, in the order that they are tried.
const MATCHERS: &[Matcher] = &[page_tree, annotation, outline, structure, tree];

/// The names of the kinds of explicit destination.
const FITS: &[&[u8]] = &[
    b"XYZ", b"Fit", b"FitH", b"FitV", b"FitR", b"FitB", b"FitBH", b"FitBV",
];

/// Returns the role of the reference at `path` in `top`, the value of the
/// object that holds it.
pub(super) fn role(top: &Value, path: &[Step]) -> Option<Role> {
    if is_destination(top, path) {
        return Some(Role::Destination);
    }
    let dict = match top {
        Value::Dict(dict) => dict,
        Value::Stream(stream) => stream.dict(),
        _ => return None,
    };
    MATCHERS.iter().find_map(|matcher| matcher(dict, path))
}

/// Returns whether the reference at `path` in `top` is the page of an
/// explicit destination: the first item of an array whose second is the
/// name of a kind of destination.
fn is_destination(top: &Value, path: &[Step]) -> bool {
    let Some((Step::Index(0), parent)) = path.split_last() else {
        return false;
    };
    let Some(Value::Array(items)) = at(top, parent) else {
        return false;
    };
    matches!(items.get(1), Some(Value::Name(kind)) if FITS.contains(&kind.as_bytes()))
}

/// Returns the value at `path` in `top`, through arrays, dictionaries, and
/// the dictionaries of streams.
fn at<'v>(top: &'v Value, path: &[Step]) -> Option<&'v Value> {
    path.iter()
        .try_fold(top, |value, step| match (value, step) {
            (Value::Array(items), Step::Index(index)) => items.get(*index),
            (Value::Dict(dict), Step::Key(key)) => dict.get(key.as_bytes()),
            (Value::Stream(stream), Step::Key(key)) => stream.dict().get(key.as_bytes()),
            _ => None,
        })
}

/// Returns whether `dict` has `/Type` named `kind`.
fn is_type(dict: &Dict, kind: &[u8]) -> bool {
    matches!(dict.get("Type"), Some(Value::Name(name)) if name.as_bytes() == kind)
}

/// Returns the key of a path of one step.
fn only_key(path: &[Step]) -> Option<&[u8]> {
    match path {
        [Step::Key(key)] => Some(key.as_bytes()),
        _ => None,
    }
}

/// Returns the key of a path of a key and an array position.
fn key_index(path: &[Step]) -> Option<(&[u8], usize)> {
    match path {
        [Step::Key(key), Step::Index(index)] => Some((key.as_bytes(), *index)),
        _ => None,
    }
}

fn page_tree(dict: &Dict, path: &[Step]) -> Option<Role> {
    let pages = is_type(dict, b"Pages");
    let page = is_type(dict, b"Page");
    if pages && key_index(path).is_some_and(|(key, _)| key == b"Kids") {
        return Some(Role::PageTreeKid);
    }
    if (pages || page) && only_key(path) == Some(b"Parent") {
        return Some(Role::PageTreeParent);
    }
    if page && key_index(path).is_some_and(|(key, _)| key == b"Annots") {
        return Some(Role::PageAnnotation);
    }
    None
}

fn annotation(dict: &Dict, path: &[Step]) -> Option<Role> {
    let annotation =
        is_type(dict, b"Annot") || (dict.get("Subtype").is_some() && dict.get("Rect").is_some());
    (annotation && only_key(path) == Some(b"P")).then_some(Role::AnnotationPage)
}

fn outline(dict: &Dict, path: &[Step]) -> Option<Role> {
    let outline = is_type(dict, b"Outlines") || dict.get("Title").is_some();
    let link = only_key(path)
        .is_some_and(|key| matches!(key, b"First" | b"Last" | b"Next" | b"Prev" | b"Parent"));
    (outline && link).then_some(Role::OutlineLink)
}

fn structure(dict: &Dict, path: &[Step]) -> Option<Role> {
    let root = is_type(dict, b"StructTreeRoot");
    let element = is_type(dict, b"StructElem") || (!root && dict.get("S").is_some());
    let reference = is_type(dict, b"MCR") || is_type(dict, b"OBJR");
    let kid = |path: &[Step]| match path {
        [Step::Key(key)] | [Step::Key(key), Step::Index(_)] => key.as_bytes() == b"K",
        _ => false,
    };
    // A kid that a structure element holds directly: a marked-content or
    // object reference, `/K << /Pg … /Obj … >>`, or in `/K [ … ]`.
    let direct = |name: &[u8]| match path {
        [Step::Key(k), Step::Key(key)] | [Step::Key(k), Step::Index(_), Step::Key(key)] => {
            k.as_bytes() == b"K" && key.as_bytes() == name
        }
        _ => false,
    };
    if (root || element) && kid(path) {
        return Some(Role::StructureKid);
    }
    if element && only_key(path) == Some(b"P") {
        return Some(Role::StructureParent);
    }
    if ((element || reference) && only_key(path) == Some(b"Pg")) || (element && direct(b"Pg")) {
        return Some(Role::StructurePage);
    }
    if (reference && only_key(path) == Some(b"Obj")) || (element && direct(b"Obj")) {
        return Some(Role::StructureObject);
    }
    None
}

fn tree(dict: &Dict, path: &[Step]) -> Option<Role> {
    if dict.get("Type").is_some() {
        return None;
    }
    let node = dict.get("Limits").is_some()
        || dict.get("Names").is_some()
        || dict.get("Nums").is_some()
        || dict.iter().all(|(key, _)| key.as_bytes() == b"Kids");
    match path {
        [Step::Key(key), Step::Index(_)] if key.as_bytes() == b"Kids" && node => {
            Some(Role::TreeKid)
        }
        [Step::Key(key), Step::Index(index)] if key.as_bytes() == b"Names" && index % 2 == 1 => {
            Some(Role::NameTreeValue)
        }
        [Step::Key(key), Step::Index(index)]
        | [Step::Key(key), Step::Index(index), Step::Index(_)]
            if key.as_bytes() == b"Nums" && index % 2 == 1 =>
        {
            Some(Role::NumberTreeValue)
        }
        _ => None,
    }
}
