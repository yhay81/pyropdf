//! Values from the objects that hayro-syntax parses (DESIGN.md F5).
//!
//! hayro-syntax parses every object through its public `FromBytes`, and the
//! store converts what it parsed into owned values. The conversion is
//! iterative, so the depth of an object costs no stack, and every collection
//! that it grows from the input charges the operation's budget (F6).

use std::fmt::{self, Write as _};
use std::iter;
use std::mem;
use std::panic::{self, AssertUnwindSafe};
use std::sync::OnceLock;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

use hayro_syntax::budget::{Exhausted, ParseBudget};
use hayro_syntax::object::array::ArrayIter;
use hayro_syntax::object::{self as syntax, FromBytes, MaybeRef, ObjRef, Object};

use super::bytes::offset_in;
use super::{
    Array, Bytes, Dict, LENGTH, Name, PdfString, Real, Reference, Stream, StringForm, Value,
    length_value,
};
use crate::budget::{Budget, BudgetVec, LimitCode, StreamKind};
use crate::index::lex::is_white;
use crate::{Error, LimitExceeded, Result};

/// How many values the conversion converts between two polls of the budget.
const POLL_INTERVAL: u32 = 1024;

#[cfg(test)]
thread_local! {
    /// The calls of [`Value::parse`] on this thread, which tests count.
    pub(crate) static PARSES: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

impl Value {
    /// Parses the value that `source` begins with, through hayro-syntax.
    ///
    /// The arrays and dictionaries of the value keep their lexemes as slices
    /// of `source`, and a stream keeps its data so. Refuses with a malformed
    /// input error when hayro-syntax reads no value there, and with a limit
    /// when the budget refuses: `object_depth` for an array or a dictionary
    /// nested deeper than it allows, or `memory_size` for what the value
    /// holds.
    pub fn parse(source: &Bytes, budget: &Budget) -> Result<Self> {
        Self::parse_with_lengths(source, budget, &|_| Ok(None))
    }

    /// Parses the value that `source` begins with, as [`Value::parse`] does,
    /// and reads the data of a stream whose `/Length` refers to an object as
    /// hayro reads it through the file's cross-reference table.
    ///
    /// `fetch` returns the bytes that the value of the object that a
    /// reference names begins with, as [`Base::object`] returns them. When
    /// the length's object is an integer alone, and the data that it gives
    /// ends at white space and `endstream`, the stream holds that data and
    /// its dictionary keeps the reference. Otherwise the stream holds what
    /// [`Value::parse`] reads: the data before `endstream`, without the
    /// white space that ends it, as it does when `fetch` cannot read the
    /// object. Refuses with the limits that `fetch` refuses with, and as
    /// [`Value::parse`] does.
    ///
    /// [`Base::object`]: crate::version::Base::object
    pub fn parse_with_lengths(
        source: &Bytes,
        budget: &Budget,
        fetch: &dyn Fn(Reference) -> Result<Option<Bytes>>,
    ) -> Result<Self> {
        #[cfg(test)]
        PARSES.with(|parses| parses.set(parses.get() + 1));
        // hayro-syntax polls the budget as it scans and charges what it
        // allocates, also while the conversion reads entries lazily. Once it
        // refuses, whatever the parser recovered is discarded.
        budget.poll()?;
        let charge = ParseCharge::new(budget);
        let result = guard(|| {
            let object = MaybeRef::<Object<'_>>::from_bytes_with_budget(source.as_slice(), &charge)
                .map_err(|_| charge.refusal())?
                .ok_or_else(|| Error::malformed("the bytes do not begin with a PDF value"))?;
            let mut converter = Converter::new(source, budget, fetch);
            converter.charge = Some(&charge);
            converter.convert(object)
        });
        charge.check()?;
        budget.poll()?;
        result
    }

    /// Converts `object`, which hayro-syntax parsed, keeping lexemes where it
    /// borrows from `source`, as [`Value::parse`] converts what it parses.
    ///
    /// The engine converts the operands of content streams so. `object` must
    /// borrow from plain bytes: hayro-syntax's `Pdf` decrypts the strings of
    /// an encrypted document but borrows its containers from the encrypted
    /// bytes, so the lexemes of such an object would be encrypted.
    pub fn from_object(object: &Object<'_>, source: &Bytes, budget: &Budget) -> Result<Self> {
        budget.poll()?;
        let result = guard(|| {
            Converter::new(source, budget, &|_| Ok(None)).convert(MaybeRef::NotRef(object.clone()))
        });
        budget.poll()?;
        result
    }
}

impl Real {
    /// Returns the real that `lexeme` writes, keeping `lexeme`, or `None`
    /// when `lexeme` is not a real of PDF syntax: an optional sign, digits,
    /// and one decimal point, with at least one digit.
    ///
    /// The value is the `f64` that hayro-syntax reads from `lexeme`, as it
    /// reads the real back from a saved file.
    #[must_use]
    pub fn from_lexeme(lexeme: Bytes) -> Option<Self> {
        let text = lexeme.as_slice();
        let unsigned = text
            .strip_prefix(b"+")
            .or_else(|| text.strip_prefix(b"-"))
            .unwrap_or(text);
        let points = unsigned.iter().filter(|&&byte| byte == b'.').count();
        let digits = unsigned.iter().filter(|byte| byte.is_ascii_digit()).count();
        if points != 1 || digits == 0 || points + digits != unsigned.len() {
            return None;
        }
        let number = panic::catch_unwind(|| syntax::Number::from_bytes(text)).ok()??;
        let value = number.as_f64();
        value.is_finite().then_some(Self {
            value,
            lexeme: Some(lexeme),
        })
    }
}

impl Stream {
    /// Returns the data decoded through the stream's filters, by
    /// hayro-syntax, charging its decoded size to `budget` as one stream.
    ///
    /// Refuses with a malformed input error a stream whose filters
    /// hayro-syntax does not decode, such as an image filter, and with a
    /// limit what the budget does not allow, such as a chain of more
    /// filters than its `stream_filter_count`. hayro-syntax 0.8 decodes a
    /// stream whole and without a bound (yhay81/pyropdf#48), so the budget
    /// counts the decoded bytes once they are decoded.
    pub fn decoded(&self, budget: &Budget) -> Result<Vec<u8>> {
        self.decoded_as(StreamKind::Any, budget)
    }

    /// Returns the data decoded as [`Stream::decoded`] decodes it, charging
    /// its decoded size as a stream of `kind`, whose limits also bound it.
    pub fn decoded_as(&self, kind: StreamKind, budget: &Budget) -> Result<Vec<u8>> {
        match self.dict.get("Filter") {
            None => return copy(&self.data, budget).map(Vec::from).map_err(Into::into),
            Some(Value::Array(filters)) => {
                budget.check(
                    LimitCode::StreamFilterCount,
                    u64::try_from(filters.len()).unwrap_or(u64::MAX),
                )?;
            }
            Some(_) => {}
        }
        let mut source = BudgetVec::new(budget);
        crate::write::value(&mut source, &Value::Stream(Box::new(self.clone())))?;
        let data = panic::catch_unwind(AssertUnwindSafe(|| {
            match Object::from_bytes(source.as_slice())? {
                Object::Stream(stream) => stream.decoded().ok().map(|data| data.into_owned()),
                _ => None,
            }
        }))
        .ok()
        .flatten()
        .ok_or_else(|| Error::malformed("the stream's filters cannot be decoded"))?;
        let size = u64::try_from(data.len()).unwrap_or(u64::MAX);
        budget.decode(kind).charge(size)?;
        Ok(data)
    }
}

/// Runs a conversion, and turns a panic in hayro-syntax into an error.
///
/// hayro-syntax 0.8 panics on some malformed input that it skips but cannot
/// read, such as an array that holds a name whose `#` is not followed by two
/// hexadecimal digits (I3, yhay81/pyropdf#19). Its next release reads such
/// input without panicking.
fn guard<T>(conversion: impl FnOnce() -> Result<T>) -> Result<T> {
    panic::catch_unwind(AssertUnwindSafe(conversion))
        .unwrap_or_else(|_| Err(Error::malformed("hayro-syntax panicked on the object")))
}

/// The budget of one parse as hayro-syntax charges it: it polls the
/// operation's budget as hayro-syntax scans, and charges what hayro-syntax
/// allocates to its memory.
///
/// hayro-syntax refuses once with `Exhausted`; the charge keeps the budget's
/// refusal, and keeps refusing after it. It releases on drop what
/// hayro-syntax allocated and did not release, such as the buffers of
/// escaped names and strings.
struct ParseCharge<'b> {
    budget: &'b Budget,
    refusal: OnceLock<LimitExceeded>,
    /// The memory charged and not released.
    held: AtomicU64,
}

impl<'b> ParseCharge<'b> {
    const fn new(budget: &'b Budget) -> Self {
        Self {
            budget,
            refusal: OnceLock::new(),
            held: AtomicU64::new(0),
        }
    }

    /// Returns the refusal, or a malformed input error if hayro-syntax
    /// reported one without the budget refusing.
    fn refusal(&self) -> Error {
        self.refusal.get().map_or_else(
            || Error::malformed("hayro-syntax refused without a budget refusal"),
            |&refusal| refusal.into(),
        )
    }

    /// Refuses with the budget's refusal, if it refused.
    fn check(&self) -> Result<()> {
        match self.refusal.get() {
            Some(&refusal) => Err(refusal.into()),
            None => Ok(()),
        }
    }

    fn refuse(
        &self,
        result: std::result::Result<(), LimitExceeded>,
    ) -> std::result::Result<(), Exhausted> {
        if self.exhausted() {
            return Err(Exhausted);
        }
        result.map_err(|refusal| {
            let _ = self.refusal.set(refusal);
            Exhausted
        })
    }
}

impl ParseBudget for ParseCharge<'_> {
    fn work(&self, _bytes: usize) -> std::result::Result<(), Exhausted> {
        // The work of a parse is linear in its input, which other limits
        // bound; the deadline and the cancellation bound its time.
        self.refuse(self.budget.poll())
    }

    fn allocate(&self, bytes: usize) -> std::result::Result<(), Exhausted> {
        let bytes = u64::try_from(bytes).unwrap_or(u64::MAX);
        self.refuse(self.budget.charge(LimitCode::MemorySize, bytes))?;
        self.held.fetch_add(bytes, Relaxed);
        Ok(())
    }

    fn release(&self, bytes: usize) {
        let bytes = u64::try_from(bytes).unwrap_or(u64::MAX);
        self.held
            .update(Relaxed, Relaxed, |held| held.saturating_sub(bytes));
        self.budget.release(LimitCode::MemorySize, bytes);
    }

    fn exhausted(&self) -> bool {
        self.refusal.get().is_some()
    }
}

impl Drop for ParseCharge<'_> {
    fn drop(&mut self) {
        self.budget
            .release(LimitCode::MemorySize, *self.held.get_mut());
    }
}

/// Converts the objects that hayro-syntax parsed from one source.
struct Converter<'s, 'b> {
    /// The bytes that the objects borrow from, which lexemes and stream data
    /// share.
    source: &'s Bytes,
    budget: &'b Budget,
    /// Returns the bytes of the object that a stream's `/Length` refers to.
    fetch: &'s dyn Fn(Reference) -> Result<Option<Bytes>>,
    /// How many values the conversion has converted, for polling the budget.
    steps: u32,
    /// What hayro-syntax charges while it reads the objects, if it read them
    /// with the budget.
    charge: Option<&'b ParseCharge<'b>>,
}

/// An array or a dictionary whose values are being converted.
enum Frame<'a, 'b> {
    Array {
        items: ArrayIter<'a>,
        values: BudgetVec<'b, Value>,
        lexeme: Option<Bytes>,
    },
    Dict {
        dict: syntax::Dict<'a>,
        /// The keys in the order of the bytes, each after the place that
        /// sorted it.
        keys: BudgetVec<'b, (usize, syntax::Name<'a>)>,
        /// How many of the keys have had their values started.
        next: usize,
        entries: BudgetVec<'b, (Name, Value)>,
        lexeme: Option<Bytes>,
        /// The stream whose dictionary this is.
        stream: Option<syntax::Stream<'a>>,
    },
}

/// What converting an object starts with: the value, or the frame of a
/// container whose values come next.
enum Start<'a, 'b> {
    Value(Value),
    Frame(Frame<'a, 'b>),
}

impl<'s, 'b> Converter<'s, 'b> {
    const fn new(
        source: &'s Bytes,
        budget: &'b Budget,
        fetch: &'s dyn Fn(Reference) -> Result<Option<Bytes>>,
    ) -> Self {
        Self {
            source,
            budget,
            fetch,
            steps: 0,
            charge: None,
        }
    }

    /// Converts `object` and everything in it.
    fn convert<'a>(&mut self, object: MaybeRef<Object<'a>>) -> Result<Value> {
        let mut current = match self.start(object, 1)? {
            Start::Value(value) => return Ok(value),
            Start::Frame(frame) => frame,
        };
        // The containers that hold the current one, outermost first.
        let mut parents = BudgetVec::new(self.budget);
        loop {
            self.tick()?;
            if let Some(object) = current.next_object() {
                match self.start(object, parents.len() + 2)? {
                    Start::Value(value) => current.accept(value, self.budget)?,
                    Start::Frame(frame) => parents.try_push(mem::replace(&mut current, frame))?,
                }
                continue;
            }
            let value = self.finish(current)?;
            match parents.pop() {
                Some(mut parent) => {
                    parent.accept(value, self.budget)?;
                    current = parent;
                }
                None => return Ok(value),
            }
        }
    }

    /// Converts `object` at `depth`, or starts its frame when it is a
    /// container, after checking `object_depth`.
    fn start<'a>(&self, object: MaybeRef<Object<'a>>, depth: usize) -> Result<Start<'a, 'b>> {
        let object = match object {
            MaybeRef::Ref(reference) => return Ok(Start::Value(reference_value(reference))),
            MaybeRef::NotRef(object) => object,
        };
        let value = match object {
            Object::Null(_) => Value::Null,
            Object::Boolean(value) => Value::Boolean(value),
            Object::Number(number) => number_value(number),
            Object::Name(name) => Value::Name(self.name(&name)?),
            Object::String(string) => Value::String(self.string(&string)?),
            Object::Array(array) => {
                self.check_depth(depth)?;
                return Ok(Start::Frame(Frame::Array {
                    lexeme: self.array_lexeme(&array),
                    items: array.raw_iter(),
                    values: BudgetVec::new(self.budget),
                }));
            }
            Object::Dict(dict) => {
                self.check_depth(depth)?;
                return Ok(Start::Frame(self.dict_frame(dict, None)?));
            }
            Object::Stream(stream) => {
                self.check_depth(depth)?;
                let dict = stream.dict().clone();
                return Ok(Start::Frame(self.dict_frame(dict, Some(stream))?));
            }
        };
        Ok(Start::Value(value))
    }

    /// Returns the value of a container whose values are all converted.
    fn finish(&self, frame: Frame<'_, 'b>) -> Result<Value> {
        Ok(match frame {
            Frame::Array { values, lexeme, .. } => Value::Array(Array {
                items: values.into_vec(),
                lexeme,
            }),
            Frame::Dict {
                entries,
                lexeme,
                stream: None,
                ..
            } => Value::Dict(Dict {
                entries: entries.into_vec(),
                lexeme,
            }),
            Frame::Dict {
                mut entries,
                mut lexeme,
                stream: Some(stream),
                ..
            } => {
                // hayro reads the data that an indirect length gives, and
                // the dictionary keeps the reference.
                if let Some((_, Value::Reference(reference))) =
                    entries.iter().find(|(key, _)| key.as_bytes() == LENGTH)
                    && let Some(data) = self.indirect_data(&stream, *reference)?
                {
                    let dict = Dict {
                        entries: entries.into_vec(),
                        lexeme,
                    };
                    return Ok(Value::Stream(Box::new(Stream { dict, data })));
                }
                let data = self.stream_data(&stream)?;
                // The data is what hayro-syntax found, which its length
                // describes. A dictionary that says otherwise, or refers to
                // a length that does not fit the data, is changed and
                // forgets its lexeme.
                let length = length_value(data.len());
                match entries.iter_mut().find(|(key, _)| key.as_bytes() == LENGTH) {
                    Some((_, value)) if *value == length => {}
                    Some((_, value)) => {
                        *value = length;
                        lexeme = None;
                    }
                    None => {
                        entries.try_push((Name(copy(LENGTH, self.budget)?), length))?;
                        lexeme = None;
                    }
                }
                let dict = Dict {
                    entries: entries.into_vec(),
                    lexeme,
                };
                Value::Stream(Box::new(Stream { dict, data }))
            }
        })
    }

    /// Returns the frame of `dict`, the dictionary of `stream` if it has one.
    fn dict_frame<'a>(
        &self,
        dict: syntax::Dict<'a>,
        stream: Option<syntax::Stream<'a>>,
    ) -> Result<Frame<'a, 'b>> {
        let keys = self.key_order(&dict)?;
        let mut entries = BudgetVec::new(self.budget);
        // A stream's dictionary may gain its `/Length`.
        entries.try_reserve_exact(keys.len() + usize::from(stream.is_some()))?;
        Ok(Frame::Dict {
            lexeme: self.source.share(dict.data()),
            dict,
            keys,
            next: 0,
            entries,
            stream,
        })
    }

    /// Returns the keys of `dict` in the order of its bytes.
    ///
    /// hayro-syntax 0.8 keeps the keys of a dictionary in a hash map. A key
    /// that it borrows from the bytes, as it does every key written without
    /// a `#` escape, points where it is written. When a key has an escape,
    /// [`Converter::rank_keys`] finds the order from the entries.
    fn key_order<'a>(
        &self,
        dict: &syntax::Dict<'a>,
    ) -> Result<BudgetVec<'b, (usize, syntax::Name<'a>)>> {
        let data = dict.data();
        let mut keys = BudgetVec::new(self.budget);
        keys.try_reserve_exact(dict.len())?;
        let mut escaped = false;
        for key in dict.keys() {
            let place = offset_in(data, &key);
            escaped |= place.is_none();
            keys.try_push((place.unwrap_or(usize::MAX), key))?;
        }
        if escaped {
            self.rank_keys(dict, &mut keys)?;
        }
        // A key whose place stays unknown comes after the others, in the
        // order of its bytes.
        keys.sort_unstable_by(|(place, key), (other_place, other)| {
            place
                .cmp(other_place)
                .then_with(|| key.as_ref().cmp(other.as_ref()))
        });
        Ok(keys)
    }

    /// Places each key of `dict` by its first appearance among the
    /// dictionary's entries, read once more as the elements of an array.
    ///
    /// Between `<<` and `>>`, a dictionary's bytes are its keys and values in
    /// order, which hayro-syntax tokenizes as it does an array's elements
    /// between `[` and `]`. As in hayro-syntax's dictionaries, anything but a
    /// name where a key belongs is skipped. When this reading does not find
    /// exactly the dictionary's keys, or hayro-syntax panics on it, the
    /// places stay as they were.
    fn rank_keys<'a>(
        &self,
        dict: &syntax::Dict<'a>,
        keys: &mut [(usize, syntax::Name<'a>)],
    ) -> Result<()> {
        let data = dict.data();
        let (Some(inner), true) = (
            data.get(2..data.len().saturating_sub(2)),
            data.starts_with(b"<<") && data.ends_with(b">>"),
        ) else {
            return Ok(());
        };
        let mut array = BudgetVec::new(self.budget);
        array.try_reserve_exact(inner.len() + 2)?;
        array.try_push(b'[')?;
        array.try_extend_from_slice(inner)?;
        array.try_push(b']')?;
        keys.sort_unstable_by(|(_, key), (_, other)| key.as_ref().cmp(other.as_ref()));
        let mut ranks = BudgetVec::new(self.budget);
        ranks.try_reserve_exact(keys.len())?;
        ranks.try_extend(iter::repeat_n(usize::MAX, keys.len()))?;
        let read = panic::catch_unwind(AssertUnwindSafe(|| {
            let elements = match self.charge {
                Some(charge) => syntax::Array::from_bytes_with_budget(&array, charge).ok()??,
                None => syntax::Array::from_bytes(&array)?,
            };
            let mut rank = 0;
            let mut key_next = true;
            for element in elements.raw_iter() {
                if !key_next {
                    key_next = true;
                    continue;
                }
                let MaybeRef::NotRef(Object::Name(name)) = element else {
                    continue;
                };
                let index = keys
                    .binary_search_by(|(_, key)| key.as_ref().cmp(name.as_ref()))
                    .ok()?;
                if ranks[index] == usize::MAX {
                    ranks[index] = rank;
                }
                rank += 1;
                key_next = false;
            }
            key_next.then_some(())
        }));
        // A refusal is the operation's, not a reading that failed.
        if let Some(charge) = self.charge {
            charge.check()?;
        }
        if matches!(read, Ok(Some(()))) && ranks.iter().all(|&rank| rank != usize::MAX) {
            for ((place, _), &rank) in keys.iter_mut().zip(ranks.iter()) {
                *place = rank;
            }
        }
        Ok(())
    }

    /// Returns the lexeme of `array`: its data, which hayro-syntax reports
    /// without the brackets, and the brackets around it.
    fn array_lexeme(&self, array: &syntax::Array<'_>) -> Option<Bytes> {
        let inner = array.data();
        let start = self.source.offset_of(inner)?.checked_sub(1)?;
        let lexeme = self.source.slice(start..start + inner.len() + 2)?;
        (lexeme.first() == Some(&b'[') && lexeme.last() == Some(&b']')).then_some(lexeme)
    }

    /// Returns the data of `stream` that its length's object, which
    /// `reference` names, gives, as hayro reads it: the bytes after the end
    /// of line that follows `stream`, when white space and `endstream`
    /// follow them in the source.
    ///
    /// Returns `None` when the object is unreadable or not an integer alone,
    /// or the data that it gives does not fit, so that the stream falls back
    /// to what hayro-syntax found. Refuses as the budget refuses reading the
    /// object. The object is read alone, resolving no length of its own, so
    /// a length that names its own stream reads no object again.
    fn indirect_data(
        &self,
        stream: &syntax::Stream<'_>,
        reference: Reference,
    ) -> Result<Option<Bytes>> {
        let bytes = match (self.fetch)(reference) {
            Ok(bytes) => bytes,
            // hayro reads an object that it cannot read as no length.
            Err(error) if error.limit().is_none() => None,
            Err(error) => return Err(error),
        };
        let length = bytes
            .as_deref()
            .and_then(|bytes| length_object(bytes, self.charge));
        if let Some(charge) = self.charge {
            charge.check()?;
        }
        let Some(length) = length else {
            return Ok(None);
        };
        let Some(start) = self.source.offset_of(&stream.raw_data()) else {
            return Ok(None);
        };
        let Some(end) = start.checked_add(length) else {
            return Ok(None);
        };
        let Some(after) = self.source.get(end..) else {
            return Ok(None);
        };
        let keyword = after
            .iter()
            .position(|&byte| !is_white(byte))
            .unwrap_or(after.len());
        if !after[keyword..].starts_with(b"endstream") {
            return Ok(None);
        }
        Ok(self.source.slice(start..end))
    }

    /// Returns the encoded data of `stream`: a slice of the source when it is
    /// one, and a copy otherwise.
    fn stream_data(&self, stream: &syntax::Stream<'_>) -> Result<Bytes> {
        let data = stream.raw_data();
        if let Some(shared) = self.source.share(&data) {
            return Ok(shared);
        }
        Ok(Bytes::from(copy(&data, self.budget)?.into_vec()))
    }

    /// Returns the name of the bytes of `name`.
    fn name(&self, name: &syntax::Name<'_>) -> Result<Name> {
        Ok(Name(copy(name, self.budget)?))
    }

    /// Returns the string of the bytes of `string`.
    ///
    /// hayro-syntax 0.8 does not report the form of a string, which waits
    /// for I7 (yhay81/pyropdf#16). It borrows a literal string that needs no
    /// decoding from the bytes, so such a string is literal. It decodes every
    /// other string, hexadecimal or literal, and such a string takes the
    /// literal form when its bytes are text and the hexadecimal form
    /// otherwise.
    fn string(&self, string: &syntax::String<'_>) -> Result<PdfString> {
        let bytes = string.as_bytes();
        let form = if self.source.offset_of(bytes).is_some() || is_text(bytes) {
            StringForm::Literal
        } else {
            StringForm::Hexadecimal
        };
        Ok(PdfString {
            bytes: copy(bytes, self.budget)?,
            form,
        })
    }

    /// Refuses with `object_depth` an array or a dictionary at `depth`, the
    /// outermost being at depth 1, when that is deeper than the budget
    /// allows.
    fn check_depth(&self, depth: usize) -> Result<(), LimitExceeded> {
        self.budget.check(
            LimitCode::ObjectDepth,
            u64::try_from(depth).unwrap_or(u64::MAX),
        )
    }

    /// Counts one converted value, and polls the budget every
    /// [`POLL_INTERVAL`] values, so that a long conversion stops at the
    /// operation's deadline or cancellation.
    fn tick(&mut self) -> Result<(), LimitExceeded> {
        self.steps = self.steps.wrapping_add(1);
        if self.steps.is_multiple_of(POLL_INTERVAL) {
            self.budget.poll()?;
        }
        Ok(())
    }
}

impl<'a, 'b> Frame<'a, 'b> {
    /// Returns the next object of the container to convert, or `None` when
    /// it has no values left.
    fn next_object(&mut self) -> Option<MaybeRef<Object<'a>>> {
        match self {
            Self::Array { items, .. } => items.next(),
            Self::Dict {
                dict, keys, next, ..
            } => {
                let (_, key) = keys.get(*next)?;
                *next += 1;
                // A value that hayro-syntax skips but cannot read, such as a
                // lone `+`, reads as null, which PDF reads as no value.
                Some(
                    dict.get_raw::<Object<'a>>(key.as_ref())
                        .unwrap_or(MaybeRef::NotRef(Object::Null(syntax::Null))),
                )
            }
        }
    }

    /// Adds `value`, the value of the object that [`Frame::next_object`]
    /// returned last.
    fn accept(&mut self, value: Value, budget: &'b Budget) -> Result<()> {
        match self {
            Self::Array { values, .. } => values.try_push(value)?,
            Self::Dict {
                keys,
                next,
                entries,
                ..
            } => {
                let (_, key) = &keys[*next - 1];
                entries.try_push((Name(copy(key, budget)?), value))?;
            }
        }
        Ok(())
    }
}

/// Returns a copy of `bytes`, charged to `budget`.
fn copy(bytes: &[u8], budget: &Budget) -> Result<Box<[u8]>, LimitExceeded> {
    let mut copy = BudgetVec::new(budget);
    copy.try_reserve_exact(bytes.len())?;
    copy.try_extend_from_slice(bytes)?;
    Ok(copy.into_vec().into_boxed_slice())
}

/// Returns the length that `bytes`, the bytes that a stream's length object
/// begins with, give: an integer from 0, which hayro-syntax reads alone.
///
/// A reference or any other value gives no length. hayro-syntax reads the
/// bytes as one value, so it reads a stream there through no other object.
fn length_object(bytes: &[u8], charge: Option<&ParseCharge<'_>>) -> Option<usize> {
    let object = panic::catch_unwind(AssertUnwindSafe(|| match charge {
        Some(charge) => MaybeRef::<Object<'_>>::from_bytes_with_budget(bytes, charge).ok()?,
        None => MaybeRef::<Object<'_>>::from_bytes(bytes),
    }))
    .ok()??;
    match object {
        MaybeRef::NotRef(Object::Number(number)) if is_integer(number) => {
            usize::try_from(number.as_i64()).ok()
        }
        _ => None,
    }
}

/// Returns the value of a reference that hayro-syntax read.
///
/// A reference names an object only with a number from 0 to `u32::MAX` and
/// a generation from 0 to 65535. Any other refers to no object, which PDF
/// reads as null.
fn reference_value(reference: ObjRef) -> Value {
    match (
        u32::try_from(reference.obj_number),
        u16::try_from(reference.gen_number),
    ) {
        (Ok(number), Ok(generation)) => Value::Reference(Reference::new(number, generation)),
        _ => Value::Null,
    }
}

/// Returns the value of a number that hayro-syntax read.
fn number_value(number: syntax::Number) -> Value {
    if is_integer(number) {
        Value::Integer(number.as_i64())
    } else {
        Value::Real(Real::new(number.as_f64()))
    }
}

/// Returns whether hayro-syntax read `number` as an integer.
///
/// hayro-syntax 0.8 keeps the kind of a number private, but its `Display`
/// writes an integer as its decimal digits and a real as Rust's `Debug` form
/// of an `f64`, which always has a decimal point, an exponent, `inf`, or
/// `NaN`.
pub(crate) fn is_integer(number: syntax::Number) -> bool {
    /// Whether all the text written to it is digits and minus signs.
    struct Digits(bool);

    impl fmt::Write for Digits {
        fn write_str(&mut self, text: &str) -> fmt::Result {
            self.0 &= text
                .bytes()
                .all(|byte| byte.is_ascii_digit() || byte == b'-');
            Ok(())
        }
    }

    let mut digits = Digits(true);
    write!(digits, "{number}").is_ok() && digits.0
}

/// Returns whether `bytes` read as text: printable ASCII, and the white space
/// and control characters that a literal string escapes by name.
fn is_text(bytes: &[u8]) -> bool {
    bytes
        .iter()
        .all(|&byte| matches!(byte, b' '..=b'~' | b'\n' | b'\r' | b'\t' | 0x08 | 0x0C))
}
