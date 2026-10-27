//! Typed object descriptors generated from the Arlington PDF Model
//! (DESIGN.md section 11).
//!
//! The model describes each object type of PDF 2.0 as one TSV file, a row
//! per key. `tools/arlington/generate.py` turns the files of a pinned commit
//! into [`OBJECTS`], one [`Object`] per file, sorted by name. Predicates of
//! the model (`fn:...`) are kept as the model writes them; evaluating them is
//! the structural check's work (DESIGN.md section 8).

#![warn(clippy::allow_attributes_without_reason)]

#[rustfmt::skip]
mod generated;

pub use generated::{COMMIT, OBJECTS};

/// A basic type of the model.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Kind {
    Array,
    Bitmask,
    Boolean,
    Date,
    Dictionary,
    Integer,
    Matrix,
    Name,
    NameTree,
    Null,
    Number,
    NumberTree,
    Rectangle,
    Stream,
    String,
    StringAscii,
    StringByte,
    StringText,
}

/// Whether a key is required, or must be an indirect reference.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Condition {
    Never,
    Always,
    /// When the predicate of the model holds, such as
    /// `fn:IsRequired(@Subtype==Link)`.
    When(&'static str),
}

/// A PDF version, such as 1.7.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Version {
    pub major: u8,
    pub minor: u8,
}

/// The PDF version that introduced a key.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Since {
    Version(Version),
    /// A predicate of the model, such as `fn:Extension(ADBE_Extn3,1.7)`.
    When(&'static str),
}

/// The descriptor of one object type: one TSV file of the model.
#[derive(Debug)]
pub struct Object {
    pub name: &'static str,
    /// The keys in the order of the model. A key may be a name, `*` for any
    /// other key, an array index, or `N*` for array elements that repeat.
    pub keys: &'static [Key],
}

/// One key of an object type: one row of its TSV file.
#[derive(Debug)]
pub struct Key {
    pub name: &'static str,
    /// The types that the value may have, each with the columns that the
    /// model gives per type.
    pub types: &'static [Typed],
    pub since: Since,
    pub deprecated: Option<Version>,
    pub required: Condition,
    pub inheritable: bool,
    pub note: &'static str,
}

/// One type of a key's value.
#[derive(Debug)]
pub struct Typed {
    pub kind: Kind,
    /// The predicate that admits this type, such as
    /// `fn:SinceVersion(2.0,array)`.
    pub condition: Option<&'static str>,
    pub indirect: Condition,
    pub default: Option<&'static str>,
    /// The possible values, each a value or a predicate around one.
    pub values: &'static [&'static str],
    pub special: Option<&'static str>,
    /// The object types that a value of this type may be.
    pub links: &'static [Link],
}

/// An object type that a value may be.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Link {
    /// The index of the object type in [`OBJECTS`].
    pub object: u16,
    /// The predicate that admits this object type, such as
    /// `fn:SinceVersion(1.5,FilterCrypt)`.
    pub condition: Option<&'static str>,
}

impl Link {
    /// The object type that a value may be.
    #[must_use]
    pub fn target(&self) -> &'static Object {
        &OBJECTS[usize::from(self.object)]
    }
}

impl Object {
    /// The key of this type named `name`, as the model writes it.
    #[must_use]
    pub fn key(&self, name: &str) -> Option<&'static Key> {
        self.keys.iter().find(|key| key.name == name)
    }
}

/// The object type named `name`, as the model's file is named.
#[must_use]
pub fn object(name: &str) -> Option<&'static Object> {
    OBJECTS
        .binary_search_by(|object| object.name.cmp(name))
        .ok()
        .map(|at| &OBJECTS[at])
}

#[cfg(test)]
mod tests;
