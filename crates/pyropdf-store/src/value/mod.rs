//! The values of the store (DESIGN.md section 4.2).
//!
//! Objects reach the editing API as owned values: null, booleans, integers,
//! reals, names, strings, arrays, dictionaries, streams, and references.
//! hayro-syntax parses them (F5), and [`Value::parse`] converts what it
//! parsed. A value read from bytes keeps its lexeme, the bytes it was read
//! from, wherever hayro-syntax 0.8 reports them: for arrays and dictionaries,
//! and so for the dictionary of a stream. A dictionary keeps its key order,
//! and a stream keeps its encoded data, shared with the bytes it was read
//! from.
//!
//! Every method that changes an array or a dictionary, or lends out one of
//! its values to change, forgets the container's lexeme, so a lexeme is
//! always the bytes of the value as it is. The writer, [`crate::write`],
//! writes a value that kept its lexeme as that lexeme (F3) and every other
//! value in canonical syntax.
//!
//! Equality compares what values are, not how they are written: lexemes and
//! the form of a string do not count, and reals are equal when their `f64`
//! values are identical, bit for bit. Cloning, comparing, and dropping a
//! value recurse into the values it holds; hayro-syntax 0.8 parses no object
//! nested deeper than 64 levels.

mod bytes;
mod read;

#[cfg(test)]
mod tests;

use std::fmt;

pub use bytes::Bytes;
#[cfg(test)]
pub(crate) use read::PARSES;
pub(crate) use read::is_integer;

/// A PDF value: an object, or an element of one.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    /// The null object.
    Null,
    /// A boolean.
    Boolean(bool),
    /// An integer.
    Integer(i64),
    /// A real number.
    Real(Real),
    /// A name.
    Name(Name),
    /// A string.
    String(PdfString),
    /// An array.
    Array(Array),
    /// A dictionary.
    Dict(Dict),
    /// A stream, which PDF allows only as an indirect object.
    Stream(Box<Stream>),
    /// A reference to an indirect object.
    Reference(Reference),
}

impl Value {
    /// Returns the bytes that the value was read from, if it is unchanged
    /// since and hayro-syntax reported them.
    ///
    /// Arrays and dictionaries keep their lexemes. A stream's dictionary
    /// keeps its own, which [`Stream::dict`] gives.
    #[must_use]
    pub const fn lexeme(&self) -> Option<&Bytes> {
        match self {
            Self::Real(real) => real.lexeme(),
            Self::Array(array) => array.lexeme(),
            Self::Dict(dict) => dict.lexeme(),
            _ => None,
        }
    }
}

impl From<bool> for Value {
    fn from(value: bool) -> Self {
        Self::Boolean(value)
    }
}

impl From<i64> for Value {
    fn from(value: i64) -> Self {
        Self::Integer(value)
    }
}

impl From<f64> for Value {
    fn from(value: f64) -> Self {
        Self::Real(Real::new(value))
    }
}

impl From<Real> for Value {
    fn from(real: Real) -> Self {
        Self::Real(real)
    }
}

impl From<Name> for Value {
    fn from(name: Name) -> Self {
        Self::Name(name)
    }
}

impl From<PdfString> for Value {
    fn from(string: PdfString) -> Self {
        Self::String(string)
    }
}

impl From<Array> for Value {
    fn from(array: Array) -> Self {
        Self::Array(array)
    }
}

impl From<Dict> for Value {
    fn from(dict: Dict) -> Self {
        Self::Dict(dict)
    }
}

impl From<Stream> for Value {
    fn from(stream: Stream) -> Self {
        Self::Stream(Box::new(stream))
    }
}

impl From<Reference> for Value {
    fn from(reference: Reference) -> Self {
        Self::Reference(reference)
    }
}

/// A real number: an `f64`, and the lexeme it was read from when that is
/// known.
///
/// hayro-syntax 0.8 reports no lexeme for a number, so a real read today has
/// none until I7 (yhay81/pyropdf#16), and the writer writes it with the
/// shortest representation that reads back as the same `f64`.
#[derive(Clone, Debug)]
pub struct Real {
    value: f64,
    lexeme: Option<Bytes>,
}

impl Real {
    /// Returns the real `value`.
    #[must_use]
    pub const fn new(value: f64) -> Self {
        Self {
            value,
            lexeme: None,
        }
    }

    /// Returns the value.
    #[must_use]
    pub const fn value(&self) -> f64 {
        self.value
    }

    /// Returns the bytes that the real was read from, if they are known.
    #[must_use]
    pub const fn lexeme(&self) -> Option<&Bytes> {
        self.lexeme.as_ref()
    }
}

impl PartialEq for Real {
    /// Reals are equal when their values are identical, bit for bit, so that
    /// `0.0` and `-0.0` differ, as they are written.
    fn eq(&self, other: &Self) -> bool {
        self.value.to_bits() == other.value.to_bits()
    }
}

/// A name: a sequence of bytes, without the `#` escapes that write it.
#[derive(Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Name(Box<[u8]>);

impl Name {
    /// Returns the name of `bytes`.
    #[must_use]
    pub fn new(bytes: impl Into<Box<[u8]>>) -> Self {
        Self(bytes.into())
    }

    /// Returns the bytes of the name.
    #[must_use]
    pub const fn as_bytes(&self) -> &[u8] {
        &self.0
    }
}

impl From<&str> for Name {
    fn from(name: &str) -> Self {
        Self::new(name.as_bytes())
    }
}

impl From<&[u8]> for Name {
    fn from(name: &[u8]) -> Self {
        Self::new(name)
    }
}

impl fmt::Debug for Name {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "/{}", self.0.escape_ascii())
    }
}

/// A string: a sequence of bytes, and the form that writes it.
///
/// Equality compares the bytes only.
#[derive(Clone)]
pub struct PdfString {
    bytes: Box<[u8]>,
    form: StringForm,
}

/// How a string is written.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StringForm {
    /// Between parentheses, such as `(Hello)`.
    Literal,
    /// In hexadecimal digits between angle brackets, such as `<48656C6C6F>`.
    Hexadecimal,
}

impl PdfString {
    /// Returns the string of `bytes`, written in `form`.
    #[must_use]
    pub fn new(bytes: impl Into<Box<[u8]>>, form: StringForm) -> Self {
        Self {
            bytes: bytes.into(),
            form,
        }
    }

    /// Returns a literal string of `bytes`.
    #[must_use]
    pub fn literal(bytes: impl Into<Box<[u8]>>) -> Self {
        Self::new(bytes, StringForm::Literal)
    }

    /// Returns a hexadecimal string of `bytes`.
    #[must_use]
    pub fn hexadecimal(bytes: impl Into<Box<[u8]>>) -> Self {
        Self::new(bytes, StringForm::Hexadecimal)
    }

    /// Returns the bytes of the string.
    #[must_use]
    pub const fn as_bytes(&self) -> &[u8] {
        &self.bytes
    }

    /// Returns the form that writes the string.
    #[must_use]
    pub const fn form(&self) -> StringForm {
        self.form
    }
}

impl PartialEq for PdfString {
    fn eq(&self, other: &Self) -> bool {
        self.bytes == other.bytes
    }
}

impl Eq for PdfString {}

impl fmt::Debug for PdfString {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{:?}(\"{}\")",
            self.form,
            self.bytes.escape_ascii()
        )
    }
}

/// An array of values.
#[derive(Clone, Debug, Default)]
pub struct Array {
    items: Vec<Value>,
    lexeme: Option<Bytes>,
}

impl Array {
    /// Returns an empty array.
    #[must_use]
    pub const fn new() -> Self {
        Self {
            items: Vec::new(),
            lexeme: None,
        }
    }

    /// Returns the bytes that the array was read from, if it is unchanged
    /// since.
    #[must_use]
    pub const fn lexeme(&self) -> Option<&Bytes> {
        self.lexeme.as_ref()
    }

    /// Returns the number of values.
    #[must_use]
    pub const fn len(&self) -> usize {
        self.items.len()
    }

    /// Returns whether the array has no values.
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    /// Returns the values.
    #[must_use]
    pub const fn as_slice(&self) -> &[Value] {
        self.items.as_slice()
    }

    /// Returns an iterator over the values.
    pub fn iter(&self) -> std::slice::Iter<'_, Value> {
        self.items.iter()
    }

    /// Returns the value at `index`, if there is one.
    #[must_use]
    pub fn get(&self, index: usize) -> Option<&Value> {
        self.items.get(index)
    }

    /// Returns the value at `index` to change, if there is one, and forgets
    /// the lexeme.
    pub fn get_mut(&mut self, index: usize) -> Option<&mut Value> {
        self.lexeme = None;
        self.items.get_mut(index)
    }

    /// Returns an iterator over the values to change, and forgets the lexeme.
    pub fn iter_mut(&mut self) -> std::slice::IterMut<'_, Value> {
        self.lexeme = None;
        self.items.iter_mut()
    }

    /// Appends `value`, and forgets the lexeme.
    pub fn push(&mut self, value: Value) {
        self.lexeme = None;
        #[expect(
            clippy::disallowed_methods,
            reason = "clippy.toml checks the store's calls of `Array::push` instead"
        )]
        self.items.push(value);
    }

    /// Inserts `value` at `index`, before the values from there on, and
    /// forgets the lexeme; or returns `value` if `index` is past the end.
    pub fn insert(&mut self, index: usize, value: Value) -> Result<(), Value> {
        if index > self.items.len() {
            return Err(value);
        }
        self.lexeme = None;
        #[expect(
            clippy::disallowed_methods,
            reason = "clippy.toml checks the store's calls of `Array::insert` instead"
        )]
        self.items.insert(index, value);
        Ok(())
    }

    /// Removes and returns the value at `index`, if there is one, and forgets
    /// the lexeme.
    pub fn remove(&mut self, index: usize) -> Option<Value> {
        if index >= self.items.len() {
            return None;
        }
        self.lexeme = None;
        Some(self.items.remove(index))
    }
}

impl PartialEq for Array {
    fn eq(&self, other: &Self) -> bool {
        self.items == other.items
    }
}

impl From<Vec<Value>> for Array {
    fn from(items: Vec<Value>) -> Self {
        Self {
            items,
            lexeme: None,
        }
    }
}

impl FromIterator<Value> for Array {
    #[expect(
        clippy::disallowed_methods,
        reason = "clippy.toml checks the store's calls of `collect` and `from_iter` instead"
    )]
    fn from_iter<I: IntoIterator<Item = Value>>(iter: I) -> Self {
        Self::from(iter.into_iter().collect::<Vec<_>>())
    }
}

impl<'a> IntoIterator for &'a Array {
    type Item = &'a Value;
    type IntoIter = std::slice::Iter<'a, Value>;

    fn into_iter(self) -> Self::IntoIter {
        self.iter()
    }
}

/// A dictionary: entries from names to values, in the order they were read
/// or added.
///
/// Each key appears once. When the bytes repeat a key, hayro-syntax keeps
/// its last value, and the entry stays where the key first appears.
#[derive(Clone, Debug, Default)]
pub struct Dict {
    entries: Vec<(Name, Value)>,
    lexeme: Option<Bytes>,
}

impl Dict {
    /// Returns an empty dictionary.
    #[must_use]
    pub const fn new() -> Self {
        Self {
            entries: Vec::new(),
            lexeme: None,
        }
    }

    /// Returns the bytes that the dictionary was read from, if it is
    /// unchanged since.
    #[must_use]
    pub const fn lexeme(&self) -> Option<&Bytes> {
        self.lexeme.as_ref()
    }

    /// Returns the number of entries.
    #[must_use]
    pub const fn len(&self) -> usize {
        self.entries.len()
    }

    /// Returns whether the dictionary has no entries.
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Returns the entries, in order.
    #[must_use]
    pub const fn entries(&self) -> &[(Name, Value)] {
        self.entries.as_slice()
    }

    /// Returns an iterator over the entries, in order.
    pub fn iter(&self) -> impl ExactSizeIterator<Item = (&Name, &Value)> {
        self.entries.iter().map(|(key, value)| (key, value))
    }

    /// Returns an iterator over the keys, in order.
    pub fn keys(&self) -> impl ExactSizeIterator<Item = &Name> {
        self.entries.iter().map(|(key, _)| key)
    }

    /// Returns the value of `key`, if the dictionary has it.
    #[must_use]
    pub fn get(&self, key: impl AsRef<[u8]>) -> Option<&Value> {
        let key = key.as_ref();
        self.entries
            .iter()
            .find_map(|(name, value)| (name.as_bytes() == key).then_some(value))
    }

    /// Returns the value of `key` to change, if the dictionary has it, and
    /// forgets the lexeme.
    pub fn get_mut(&mut self, key: impl AsRef<[u8]>) -> Option<&mut Value> {
        let key = key.as_ref();
        self.lexeme = None;
        self.entries
            .iter_mut()
            .find_map(|(name, value)| (name.as_bytes() == key).then_some(value))
    }

    /// Returns an iterator over the entries, whose values may change, and
    /// forgets the lexeme.
    pub fn iter_mut(&mut self) -> impl ExactSizeIterator<Item = (&Name, &mut Value)> {
        self.lexeme = None;
        self.entries.iter_mut().map(|(key, value)| (&*key, value))
    }

    /// Sets the value of `key`, and forgets the lexeme. A key that the
    /// dictionary has keeps its place, and the old value is returned; a new
    /// key comes last.
    pub fn insert(&mut self, key: Name, value: Value) -> Option<Value> {
        self.lexeme = None;
        match self.entries.iter_mut().find(|(name, _)| *name == key) {
            Some((_, old)) => Some(std::mem::replace(old, value)),
            None => {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "clippy.toml checks the store's calls of `Dict::insert` instead"
                )]
                self.entries.push((key, value));
                None
            }
        }
    }

    /// Removes the entry of `key` and returns its value, if the dictionary
    /// has it, and forgets the lexeme. The other entries keep their order.
    pub fn remove(&mut self, key: impl AsRef<[u8]>) -> Option<Value> {
        let key = key.as_ref();
        let index = self
            .entries
            .iter()
            .position(|(name, _)| name.as_bytes() == key)?;
        self.lexeme = None;
        Some(self.entries.remove(index).1)
    }
}

impl PartialEq for Dict {
    /// Dictionaries are equal when they have the same entries in the same
    /// order.
    fn eq(&self, other: &Self) -> bool {
        self.entries == other.entries
    }
}

impl FromIterator<(Name, Value)> for Dict {
    /// Returns the dictionary of the entries, in order; a repeated key keeps
    /// its first place and its last value.
    fn from_iter<I: IntoIterator<Item = (Name, Value)>>(iter: I) -> Self {
        let mut dict = Self::new();
        for (key, value) in iter {
            #[expect(
                clippy::disallowed_methods,
                reason = "clippy.toml checks the store's calls of `collect` and `from_iter` instead"
            )]
            dict.insert(key, value);
        }
        dict
    }
}

/// A stream: its dictionary and its encoded data.
///
/// The `/Length` of the dictionary is the length of the data: making the
/// stream or setting its data sets it, and the writer writes the length of
/// the data whatever the dictionary says.
#[derive(Clone, Debug, PartialEq)]
pub struct Stream {
    dict: Dict,
    data: Bytes,
}

/// The key of a stream's length.
pub(crate) const LENGTH: &[u8] = b"Length";

impl Stream {
    /// Returns the stream of `dict` and the encoded `data`, setting the
    /// dictionary's `/Length`.
    #[must_use]
    pub fn new(dict: Dict, data: Bytes) -> Self {
        let mut stream = Self { dict, data };
        stream.set_length();
        stream
    }

    /// Returns the dictionary.
    #[must_use]
    pub const fn dict(&self) -> &Dict {
        &self.dict
    }

    /// Returns the dictionary to change. The writer writes the length of the
    /// data as `/Length`, whatever the dictionary says.
    pub const fn dict_mut(&mut self) -> &mut Dict {
        &mut self.dict
    }

    /// Returns the encoded data.
    #[must_use]
    pub const fn data(&self) -> &Bytes {
        &self.data
    }

    /// Replaces the encoded data, and sets `/Length` to its length.
    pub fn set_data(&mut self, data: Bytes) {
        self.data = data;
        self.set_length();
    }

    /// Sets `/Length` to the length of the data, keeping the dictionary's
    /// lexeme when it already says so.
    fn set_length(&mut self) {
        let length = length_value(self.data.len());
        if self.dict.get(LENGTH) != Some(&length) {
            #[expect(
                clippy::disallowed_methods,
                reason = "a stream's dictionary gains one entry here, its `/Length`"
            )]
            self.dict.insert(Name::new(LENGTH), length);
        }
    }
}

/// Returns the value of `/Length` for data of `len` bytes.
pub(crate) fn length_value(len: usize) -> Value {
    // No buffer holds more than `isize::MAX` bytes, which fits an `i64`.
    Value::Integer(i64::try_from(len).unwrap_or(i64::MAX))
}

/// A reference to an indirect object: its object number and generation.
///
/// hayro-syntax 0.8 reads object numbers as `i32` (I8,
/// yhay81/pyropdf#18), so it reads back only references whose number is at
/// most `i32::MAX`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Reference {
    number: u32,
    generation: u16,
}

impl Reference {
    /// Returns the reference to object `number` of `generation`.
    #[must_use]
    pub const fn new(number: u32, generation: u16) -> Self {
        Self { number, generation }
    }

    /// Returns the object number.
    #[must_use]
    pub const fn number(&self) -> u32 {
        self.number
    }

    /// Returns the generation.
    #[must_use]
    pub const fn generation(&self) -> u16 {
        self.generation
    }
}
