//! The object writer (DESIGN.md section 4.2).
//!
//! [`value`] writes a value that kept its lexeme as that lexeme, so that an
//! unchanged value is written back as it was read (F3), and every other
//! value in canonical syntax:
//!
//! - one space between the tokens of an array or a dictionary, and none
//!   inside its brackets, as in `[0 0 612 792]` and
//!   `<</Type /Page /Parent 3 0 R>>`;
//! - an integer in decimal digits, and a real in the shortest decimal that
//!   hayro-syntax reads back as the same `f64`, always with a decimal point
//!   and never with an exponent, which PDF does not have;
//! - a name and a string escaped as ISO 32000-2 section 7.3 requires;
//! - a stream's `/Length` as the length of its data.
//!
//! [`canonical`] writes every value in canonical syntax. Whatever either
//! writes, hayro-syntax reads back as the same value. The writer is
//! iterative, and charges the output to its budget.

#[cfg(test)]
mod tests;

use std::fmt::{self, Write as _};
use std::mem;
use std::slice;

use hayro_syntax::object::{FromBytes, Number};

use crate::budget::{BudgetVec, LimitCode};
use crate::value::{LENGTH, Name, PdfString, Stream, StringForm, Value, is_integer, length_value};
use crate::{Error, LimitExceeded, Result};

/// How many values the writer writes between two polls of the budget.
const POLL_INTERVAL: u32 = 1024;

/// The largest input slice scanned or copied without polling.
const BYTE_POLL_INTERVAL: usize = 16 * 1024;

/// Significant digits that make hayro-syntax read a real exactly: more than
/// a `u64` holds, so that it reads them through Rust's parser.
const EXACT_DIGITS: usize = 21;

/// The digits of hexadecimal escapes and strings.
const HEX: &[u8; 16] = b"0123456789ABCDEF";

/// Appends `value` to `out` in PDF syntax, writing each value that kept its
/// lexeme as that lexeme.
///
/// Refuses with a malformed input error a real that is not finite and a
/// stream inside an array or a dictionary, which PDF cannot write, and with
/// `object_value_depth` a value that nests arrays and dictionaries deeper
/// than the budget of `out` allows. The writer counts the depth of what it
/// writes in canonical syntax: a lexeme is written as it is. On an error,
/// `out` may hold the start of the value.
pub fn value(out: &mut BudgetVec<'_, u8>, value: &Value) -> Result<()> {
    Writer::new(out, true).write(value)
}

/// Appends `value` to `out` in canonical syntax, ignoring every lexeme.
///
/// Refuses as [`value`] does, counting the depth of every array and
/// dictionary.
pub fn canonical(out: &mut BudgetVec<'_, u8>, value: &Value) -> Result<()> {
    Writer::new(out, false).write(value)
}

/// Writes values to one output.
struct Writer<'o, 'b> {
    out: &'o mut BudgetVec<'b, u8>,
    /// Whether a value that kept its lexeme is written as that lexeme.
    lexemes: bool,
    /// How many values the writer has written, for polling the budget.
    steps: u32,
    /// Bytes written since the last poll in `push`.
    bytes_since_poll: usize,
}

/// An array or a dictionary whose values are being written.
enum Frame<'v> {
    Array {
        items: slice::Iter<'v, Value>,
        first: bool,
    },
    Dict {
        entries: slice::Iter<'v, (Name, Value)>,
        first: bool,
        /// The stream whose dictionary this is.
        stream: Option<&'v Stream>,
        /// Whether the stream's `/Length` has been written.
        length: bool,
    },
}

impl<'o, 'b> Writer<'o, 'b> {
    const fn new(out: &'o mut BudgetVec<'b, u8>, lexemes: bool) -> Self {
        Self {
            out,
            lexemes,
            steps: 0,
            bytes_since_poll: 0,
        }
    }

    /// Writes `root` and everything in it.
    fn write(&mut self, root: &Value) -> Result<()> {
        self.out.budget().poll()?;
        self.write_inner(root)?;
        self.out.budget().poll()?;
        Ok(())
    }

    fn write_inner(&mut self, root: &Value) -> Result<()> {
        let Some(mut current) = self.start(root, 1)? else {
            return Ok(());
        };
        // The containers that hold the current one, outermost first.
        let mut parents = BudgetVec::new(self.out.budget());
        loop {
            self.tick()?;
            if let Some(value) = self.next_value(&mut current)? {
                if let Some(frame) = self.start(value, parents.len() + 2)? {
                    parents.try_push(mem::replace(&mut current, frame))?;
                }
                continue;
            }
            match parents.pop() {
                Some(parent) => current = parent,
                None => return Ok(()),
            }
        }
    }

    /// Writes `value` at `depth`, the outermost value being at depth 1, or
    /// opens it and returns its frame when it is a container to write value
    /// by value.
    fn start<'v>(&mut self, value: &'v Value, depth: usize) -> Result<Option<Frame<'v>>> {
        if self.lexemes
            && let Some(lexeme) = value.lexeme()
        {
            self.push(lexeme)?;
            return Ok(None);
        }
        match value {
            Value::Null => self.push(b"null")?,
            Value::Boolean(true) => self.push(b"true")?,
            Value::Boolean(false) => self.push(b"false")?,
            Value::Integer(integer) => self.display(integer)?,
            Value::Real(real) => self.real(real.value())?,
            Value::Name(name) => self.name(name)?,
            Value::String(string) => self.string(string)?,
            Value::Reference(reference) => {
                self.display(reference.number())?;
                self.push(b" ")?;
                self.display(reference.generation())?;
                self.push(b" R")?;
            }
            Value::Array(array) => {
                self.check_depth(depth)?;
                self.push(b"[")?;
                return Ok(Some(Frame::Array {
                    items: array.iter(),
                    first: true,
                }));
            }
            Value::Dict(dict) => {
                self.check_depth(depth)?;
                self.push(b"<<")?;
                return Ok(Some(Frame::Dict {
                    entries: dict.entries().iter(),
                    first: true,
                    stream: None,
                    length: false,
                }));
            }
            Value::Stream(stream) => {
                if depth > 1 {
                    return Err(Error::malformed(
                        "a stream is inside an array or a dictionary, where PDF allows only a reference to it",
                    ));
                }
                self.check_depth(depth)?;
                let dict = stream.dict();
                // A stream's lexeme is its dictionary's, when that gives the
                // length of the data.
                if self.lexemes
                    && let Some(lexeme) = dict.lexeme()
                    && dict.get(LENGTH) == Some(&length_value(stream.data().len()))
                {
                    self.push(lexeme)?;
                    self.stream_data(stream)?;
                    return Ok(None);
                }
                self.push(b"<<")?;
                return Ok(Some(Frame::Dict {
                    entries: dict.entries().iter(),
                    first: true,
                    stream: Some(stream),
                    length: false,
                }));
            }
        }
        Ok(None)
    }

    /// Writes what comes before the next value of `frame` and returns that
    /// value, or closes the container and returns `None` when it has no
    /// values left.
    fn next_value<'v>(&mut self, frame: &mut Frame<'v>) -> Result<Option<&'v Value>> {
        match frame {
            Frame::Array { items, first } => {
                if let Some(item) = items.next() {
                    if !mem::take(first) {
                        self.push(b" ")?;
                    }
                    return Ok(Some(item));
                }
                self.push(b"]")?;
            }
            Frame::Dict {
                entries,
                first,
                stream,
                length,
            } => {
                for (key, value) in entries.by_ref() {
                    if !mem::take(first) {
                        self.push(b" ")?;
                    }
                    self.name(key)?;
                    self.push(b" ")?;
                    match stream {
                        // The length of a stream is the length of its data.
                        Some(stream) if key.as_bytes() == LENGTH => {
                            self.display(stream.data().len())?;
                            *length = true;
                        }
                        _ => return Ok(Some(value)),
                    }
                }
                if let Some(stream) = stream {
                    if !*length {
                        if !*first {
                            self.push(b" ")?;
                        }
                        self.push(b"/Length ")?;
                        self.display(stream.data().len())?;
                    }
                    self.push(b">>")?;
                    self.stream_data(stream)?;
                } else {
                    self.push(b">>")?;
                }
            }
        }
        Ok(None)
    }

    /// Writes the data of `stream` after its dictionary, between `stream`
    /// and `endstream` and the end-of-line markers that ISO 32000-2 section
    /// 7.3.8.1 puts around it.
    fn stream_data(&mut self, stream: &Stream) -> Result<()> {
        self.push(b"\nstream\n")?;
        self.push(stream.data())?;
        self.push(b"\nendstream")
    }

    /// Writes `value`, a real number, in the shortest decimal that
    /// hayro-syntax reads back as the same `f64`.
    ///
    /// Rust writes the shortest decimal that reads back as the same `f64`
    /// through a correctly rounded parser, without an exponent. hayro-syntax
    /// 0.8 reads a decimal of up to 19 significant digits as an integer
    /// divided by a power of ten, which can round twice, and a longer one
    /// through Rust's parser. When the shortest decimal reads back
    /// differently, zeros after its last digit make it long enough for the
    /// exact parser without changing its value.
    fn real(&mut self, value: f64) -> Result<()> {
        if !value.is_finite() {
            return Err(Error::malformed(format!(
                "the real {value} is not finite, which PDF cannot write"
            )));
        }
        let start = self.out.len();
        self.display(value)?;
        if !self.out[start..].contains(&b'.') {
            self.push(b".0")?;
        }
        if reads_back(&self.out[start..], value) {
            return Ok(());
        }
        for _ in significant_digits(&self.out[start..])..EXACT_DIGITS {
            self.push(b"0")?;
        }
        if reads_back(&self.out[start..], value) {
            return Ok(());
        }
        Err(Error::malformed(format!(
            "hayro-syntax does not read the real {value} back exactly"
        )))
    }

    /// Writes `name` after a solidus, as ISO 32000-2 section 7.3.5 requires:
    /// a byte that is not a regular character, and the number sign, as a
    /// number sign and two hexadecimal digits. A regular byte outside `!` to
    /// `~` is written so too, as the section recommends. PDF does not allow
    /// the byte 0 in a name, which is written as `#00`, as hayro-syntax
    /// reads it.
    fn name(&mut self, name: &Name) -> Result<()> {
        self.reserve_encoded(
            name.as_bytes(),
            1,
            |byte| {
                if is_name_byte(byte) { 1 } else { 3 }
            },
        )?;
        self.push(b"/")?;
        for chunk in name.as_bytes().chunks(BYTE_POLL_INTERVAL) {
            self.out.budget().poll()?;
            let mut rest = chunk;
            while let Some(escape) = rest.iter().position(|&byte| !is_name_byte(byte)) {
                let (raw, tail) = rest.split_at(escape);
                self.push(raw)?;
                let byte = tail[0];
                self.push(&[b'#', hex_digit(byte >> 4), hex_digit(byte)])?;
                rest = &tail[1..];
            }
            self.push(rest)?;
        }
        Ok(())
    }

    /// Writes `string` in its form, as ISO 32000-2 section 7.3.4 requires.
    ///
    /// A literal string escapes the reverse solidus, the parentheses, and
    /// the control characters that have names, and writes every other byte
    /// outside printable ASCII as three octal digits. It never holds an
    /// end-of-line marker, which a reader takes as a line feed. A
    /// hexadecimal string has two uppercase digits per byte.
    fn string(&mut self, string: &PdfString) -> Result<()> {
        let bytes = string.as_bytes();
        match string.form() {
            StringForm::Literal => {
                self.reserve_encoded(bytes, 2, |byte| {
                    literal_escape(byte).map_or(1, |escape| escape.len)
                })?;
                self.push(b"(")?;
                for chunk in bytes.chunks(BYTE_POLL_INTERVAL) {
                    self.out.budget().poll()?;
                    let mut rest = chunk;
                    while let Some(escape) =
                        rest.iter().position(|&byte| literal_escape(byte).is_some())
                    {
                        let (raw, tail) = rest.split_at(escape);
                        self.push(raw)?;
                        if let Some(escaped) = literal_escape(tail[0]) {
                            self.push(escaped.as_bytes())?;
                        }
                        rest = &tail[1..];
                    }
                    self.push(rest)?;
                }
                self.push(b")")
            }
            StringForm::Hexadecimal => {
                self.reserve_encoded(bytes, 2, |_| 2)?;
                self.push(b"<")?;
                let mut digits = [[0; 2]; 128];
                for chunk in bytes.chunks(digits.len()) {
                    for (pair, &byte) in digits.iter_mut().zip(chunk) {
                        *pair = [hex_digit(byte >> 4), hex_digit(byte)];
                    }
                    self.push(digits[..chunk.len()].as_flattened())?;
                }
                self.push(b">")
            }
        }
    }

    /// Reserves the full encoded size once, so a tight budget cannot force
    /// an input-sized reallocation for every small output chunk.
    fn reserve_encoded(
        &mut self,
        bytes: &[u8],
        delimiters: usize,
        encoded_len: impl Fn(u8) -> usize,
    ) -> Result<()> {
        let mut size = delimiters;
        for chunk in bytes.chunks(BYTE_POLL_INTERVAL) {
            self.out.budget().poll()?;
            for &byte in chunk {
                size = size
                    .checked_add(encoded_len(byte))
                    .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
            }
        }
        self.out.budget().poll()?;
        self.out.try_reserve_polled(size)?;
        Ok(())
    }

    /// Writes `value` as `Display` writes it.
    fn display(&mut self, value: impl fmt::Display) -> Result<()> {
        let mut sink = Sink {
            out: &mut *self.out,
            refusal: None,
        };
        if write!(sink, "{value}").is_ok() {
            return Ok(());
        }
        // Only a refusal of the budget makes the sink fail.
        Err(sink
            .refusal
            .unwrap_or_else(|| LimitExceeded::new(LimitCode::MemorySize))
            .into())
    }

    /// Appends `bytes` to the output.
    fn push(&mut self, bytes: &[u8]) -> Result<()> {
        // Reserve the complete slice before copying it in chunks. Otherwise
        // a tight memory budget can force a growing reallocation per chunk.
        if bytes.len() >= BYTE_POLL_INTERVAL || self.bytes_since_poll >= BYTE_POLL_INTERVAL {
            self.out.budget().poll()?;
            self.bytes_since_poll = 0;
        }
        self.out.try_reserve_polled(bytes.len())?;
        for chunk in bytes.chunks(BYTE_POLL_INTERVAL) {
            if self.bytes_since_poll.saturating_add(chunk.len()) > BYTE_POLL_INTERVAL {
                self.out.budget().poll()?;
                self.bytes_since_poll = 0;
            }
            self.out.try_extend_from_slice(chunk)?;
            self.bytes_since_poll = self.bytes_since_poll.saturating_add(chunk.len());
            #[cfg(test)]
            tests::after_write_chunk(self.out.len(), self.out.capacity());
        }
        Ok(())
    }

    /// Refuses with `object_value_depth` an array or a dictionary at
    /// `depth` when that is deeper than the budget allows.
    fn check_depth(&self, depth: usize) -> Result<(), LimitExceeded> {
        self.out.budget().check(
            LimitCode::ObjectValueDepth,
            u64::try_from(depth).unwrap_or(u64::MAX),
        )
    }

    /// Counts one written value, and polls the budget every
    /// [`POLL_INTERVAL`] values.
    fn tick(&mut self) -> Result<(), LimitExceeded> {
        self.steps = self.steps.wrapping_add(1);
        if self.steps.is_multiple_of(POLL_INTERVAL) {
            self.out.budget().poll()?;
        }
        Ok(())
    }
}

/// The output as a target of `write!`, which keeps the budget's refusal.
struct Sink<'s, 'b> {
    out: &'s mut BudgetVec<'b, u8>,
    refusal: Option<LimitExceeded>,
}

impl fmt::Write for Sink<'_, '_> {
    fn write_str(&mut self, text: &str) -> fmt::Result {
        self.out
            .try_reserve_polled(text.len())
            .and_then(|()| self.out.try_extend_from_slice(text.as_bytes()))
            .map_err(|refusal| {
                self.refusal = Some(refusal);
                fmt::Error
            })
    }
}

/// Returns whether hayro-syntax reads `text` as the real `value`, bit for
/// bit.
fn reads_back(text: &[u8], value: f64) -> bool {
    Number::from_bytes(text)
        .is_some_and(|number| !is_integer(number) && number.as_f64().to_bits() == value.to_bits())
}

/// Returns the number of digits in `text`, a decimal, from its first digit
/// that is not zero.
fn significant_digits(text: &[u8]) -> usize {
    text.iter()
        .filter(|byte| byte.is_ascii_digit())
        .skip_while(|&&byte| byte == b'0')
        .count()
}

/// Returns whether a name writes `byte` as itself: a regular character from
/// `!` to `~` other than the number sign.
const fn is_name_byte(byte: u8) -> bool {
    matches!(byte, b'!'..=b'~')
        && !matches!(
            byte,
            b'#' | b'(' | b')' | b'<' | b'>' | b'[' | b']' | b'{' | b'}' | b'/' | b'%'
        )
}

/// Returns how a literal string writes `byte` when it does not write it as
/// itself.
fn literal_escape(byte: u8) -> Option<Escape> {
    let named = match byte {
        b'\n' => b'n',
        b'\r' => b'r',
        b'\t' => b't',
        0x08 => b'b',
        0x0C => b'f',
        b'(' | b')' | b'\\' => byte,
        b' '..=b'~' => return None,
        _ => {
            return Some(Escape {
                bytes: [
                    b'\\',
                    b'0' + (byte >> 6),
                    b'0' + ((byte >> 3) & 7),
                    b'0' + (byte & 7),
                ],
                len: 4,
            });
        }
    };
    Some(Escape {
        bytes: [b'\\', named, 0, 0],
        len: 2,
    })
}

/// An escape sequence of a literal string.
struct Escape {
    bytes: [u8; 4],
    len: usize,
}

impl Escape {
    fn as_bytes(&self) -> &[u8] {
        &self.bytes[..self.len]
    }
}

/// Returns the uppercase hexadecimal digit of the low four bits of `nibble`.
const fn hex_digit(nibble: u8) -> u8 {
    HEX[(nibble & 0x0F) as usize]
}
