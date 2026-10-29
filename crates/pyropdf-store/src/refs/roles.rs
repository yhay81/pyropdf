//! The roles of references: what a reference means to the object that
//! holds it, which decides the rule that a structural edit runs for it
//! (DESIGN.md section 8).
//!
//! The roles come from the links of the Arlington model. An object type
//! that names itself by `/Type`, `/Subtype`, or `/S` gives a reference at
//! one of its linking keys, or in the array at one, the role of that
//! [`Link`]; `tools/arlington/generate.py` writes these links into
//! `derived.rs`. The roles that rules name attach to links in [`ATTACHED`].
//!
//! An object type known only from where it is reached, such as an outline
//! item, a structure element without `/Type`, a node of a name or number
//! tree, or an explicit destination, does not name itself; hand-written
//! matchers of the referrer's value and the path give those roles.

use crate::value::{Dict, Value};

use super::Step;
use super::derived::HOLDERS;

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
    /// A link of the Arlington model that no role above names.
    Link(Link),
}

/// A link of the Arlington model: a key of an object type that names
/// itself, at which the type holds references.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Link {
    /// The object type that holds the reference, as the model's file is
    /// named, such as `PageObject`.
    pub holder: &'static str,
    /// The key, as the model writes it.
    pub key: &'static str,
    /// Whether the reference is an entry of the array at the key, rather
    /// than the key's value.
    pub element: bool,
}

/// An object type that names itself, and its links.
#[derive(Debug)]
pub(super) struct Holder {
    pub name: &'static str,
    /// The keys that name the type. A dictionary is of the type when it
    /// has one of them, and each that it has, or must have, holds one of
    /// the names.
    pub identity: &'static [Identity],
    pub links: &'static [Linking],
}

/// A key that names an object type, such as `/Type /Page`.
#[derive(Debug)]
pub(super) struct Identity {
    pub key: &'static str,
    pub values: &'static [&'static str],
    pub required: bool,
}

/// A key of an object type at which it holds references.
#[derive(Debug)]
pub(super) struct Linking {
    pub key: &'static str,
    /// Whether the references are entries of the array at the key.
    pub element: bool,
    /// The object types that the references may be.
    #[cfg_attr(
        not(test),
        expect(dead_code, reason = "the tests check the links against the model")
    )]
    pub targets: &'static [&'static str],
}

impl Holder {
    /// Returns whether `dict` names itself as of this type.
    fn names(&self, dict: &Dict) -> bool {
        let mut named = false;
        for identity in self.identity {
            match dict.get(identity.key) {
                Some(Value::Name(name))
                    if identity
                        .values
                        .iter()
                        .any(|value| value.as_bytes() == name.as_bytes()) =>
                {
                    named = true;
                }
                None if !identity.required => {}
                _ => return false,
            }
        }
        named
    }
}

/// Which links a role attaches to: the holders, a name or a prefix ending
/// in `*`, the key, and whether the reference is an entry of the key's
/// array, or either when `None`.
type Attachment = (&'static [&'static str], &'static str, Option<bool>, Role);

/// The roles that rules name, by the links that they attach to.
pub(super) const ATTACHED: &[Attachment] = &[
    (
        &["PageTreeNode", "PageTreeNodeRoot"],
        "Kids",
        Some(true),
        Role::PageTreeKid,
    ),
    (
        &["PageObject", "PageTreeNode"],
        "Parent",
        Some(false),
        Role::PageTreeParent,
    ),
    (&["PageObject"], "Annots", Some(true), Role::PageAnnotation),
    (&["Annot*"], "P", Some(false), Role::AnnotationPage),
    (&["Outline"], "First", Some(false), Role::OutlineLink),
    (&["Outline"], "Last", Some(false), Role::OutlineLink),
    (
        &["StructTreeRoot", "StructElem"],
        "K",
        None,
        Role::StructureKid,
    ),
    (&["StructElem"], "P", Some(false), Role::StructureParent),
    (
        &["StructElem", "MarkedContentReference", "ObjectReference"],
        "Pg",
        Some(false),
        Role::StructurePage,
    ),
    (
        &["ObjectReference"],
        "Obj",
        Some(false),
        Role::StructureObject,
    ),
];

/// Returns whether `pattern`, a name or a prefix ending in `*`, matches the
/// object type `holder`.
pub(super) fn matches(pattern: &str, holder: &str) -> bool {
    pattern
        .strip_suffix('*')
        .map_or(pattern == holder, |prefix| holder.starts_with(prefix))
}

/// Returns the role that `link` has: the role attached to it, or the link.
pub(super) fn attached(link: Link) -> Role {
    ATTACHED
        .iter()
        .find(|(holders, key, element, _)| {
            *key == link.key
                && element.is_none_or(|element| element == link.element)
                && holders.iter().any(|pattern| matches(pattern, link.holder))
        })
        .map_or(Role::Link(link), |(.., role)| *role)
}

/// Returns the link of the reference at `path` in `dict`, if an object type
/// that `dict` names itself as links there.
fn derived(dict: &Dict, path: &[Step]) -> Option<Link> {
    let (key, element) = match path {
        [Step::Key(key)] => (key.as_bytes(), false),
        [Step::Key(key), Step::Index(_)] => (key.as_bytes(), true),
        _ => return None,
    };
    HOLDERS
        .iter()
        .filter(|holder| holder.names(dict))
        .find_map(|holder| {
            holder
                .links
                .iter()
                .find(|linking| linking.key.as_bytes() == key && linking.element == element)
                .map(|linking| Link {
                    holder: holder.name,
                    key: linking.key,
                    element,
                })
        })
}

/// A matcher: the role that a referrer whose value is the dictionary gives
/// a reference at the path, if it is its role.
type Matcher = fn(&Dict, &[Step]) -> Option<Role>;

/// The matchers of the roles that the links do not give, in the order
/// that they are tried.
const MATCHERS: &[Matcher] = &[outline, structure, tree];

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
    derived(dict, path)
        .map(attached)
        .or_else(|| MATCHERS.iter().find_map(|matcher| matcher(dict, path)))
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
