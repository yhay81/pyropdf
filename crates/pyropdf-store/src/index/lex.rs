//! The syntax around values that the index reads itself (DESIGN.md section
//! 6): white space and comments, unsigned integers, keywords, object
//! headers, and `startxref` lines, with the characters of ISO 32000-2
//! section 7.2.

/// Whether `byte` is white space (ISO 32000-2 Table 1).
pub(crate) const fn is_white(byte: u8) -> bool {
    matches!(byte, b'\0' | b'\t' | b'\n' | b'\x0C' | b'\r' | b' ')
}

/// Whether `byte` is a delimiter (ISO 32000-2 Table 2).
const fn is_delimiter(byte: u8) -> bool {
    matches!(
        byte,
        b'(' | b')' | b'<' | b'>' | b'[' | b']' | b'{' | b'}' | b'/' | b'%'
    )
}

/// Whether `byte` is a regular character, which continues a token.
const fn is_regular(byte: u8) -> bool {
    !is_white(byte) && !is_delimiter(byte)
}

/// Returns the position after the white space and comments at `pos`.
pub(super) fn skip_space(bytes: &[u8], mut pos: usize) -> usize {
    while let Some(&byte) = bytes.get(pos) {
        if is_white(byte) {
            pos += 1;
        } else if byte == b'%' {
            pos = comment_end(bytes, pos);
        } else {
            break;
        }
    }
    pos
}

/// Returns the position after the white space at `pos`.
pub(super) fn skip_white(bytes: &[u8], mut pos: usize) -> usize {
    while bytes.get(pos).copied().is_some_and(is_white) {
        pos += 1;
    }
    pos
}

/// Returns the position after the end-of-line marker at `pos`, CR LF, LF,
/// or CR, or `pos` when none is there.
pub(super) fn skip_eol(bytes: &[u8], pos: usize) -> usize {
    match bytes.get(pos..).unwrap_or_default() {
        [b'\r', b'\n', ..] => pos + 2,
        [b'\n' | b'\r', ..] => pos + 1,
        _ => pos,
    }
}

/// Returns the end of the comment that starts at `pos`: the end-of-line
/// marker after it, or the end of the bytes.
fn comment_end(bytes: &[u8], pos: usize) -> usize {
    bytes[pos..]
        .iter()
        .position(|&byte| byte == b'\n' || byte == b'\r')
        .map_or(bytes.len(), |len| pos + len)
}

/// Returns the end of the token that starts at `pos`: the first byte from
/// there on that is not a regular character.
fn token_end(bytes: &[u8], pos: usize) -> usize {
    bytes[pos..]
        .iter()
        .position(|&byte| !is_regular(byte))
        .map_or(bytes.len(), |len| pos + len)
}

/// Whether a token ends at `pos`: the bytes end there, or white space or a
/// delimiter follows.
fn ends_token(bytes: &[u8], pos: usize) -> bool {
    !bytes.get(pos).copied().is_some_and(is_regular)
}

/// Reads the unsigned integer at `pos`, a token of decimal digits: its
/// value, and the position after it.
pub(super) fn unsigned(bytes: &[u8], pos: usize) -> Option<(u64, usize)> {
    let digits = bytes
        .get(pos..)?
        .iter()
        .take_while(|byte| byte.is_ascii_digit())
        .count();
    let end = pos + digits;
    if digits == 0 || !ends_token(bytes, end) {
        return None;
    }
    let value = bytes[pos..end].iter().try_fold(0_u64, |value, &digit| {
        value.checked_mul(10)?.checked_add(u64::from(digit - b'0'))
    })?;
    Some((value, end))
}

/// Returns the position after `word` when the token at `pos` is that
/// keyword.
pub(super) fn keyword(bytes: &[u8], pos: usize, word: &[u8]) -> Option<usize> {
    let end = pos.checked_add(word.len())?;
    (bytes.get(pos..end)? == word && ends_token(bytes, end)).then_some(end)
}

/// An object header, such as `12 0 obj`.
#[derive(Clone, Copy, Debug)]
pub(super) struct Header {
    pub(super) number: u32,
    pub(super) generation: u16,
    /// The position after the keyword `obj`.
    pub(super) end: usize,
    /// Where the object's value begins: after the header and the white
    /// space and comments that follow it.
    pub(super) body: usize,
}

/// Reads the object header at `pos`: an object number, a generation, and
/// the keyword `obj`, separated by white space and comments as hayro-syntax
/// separates them (ISO 32000-2 section 7.3.10).
///
/// The header names an object only with a number from 0 to `u32::MAX` and a
/// generation from 0 to 65535.
pub(super) fn header(bytes: &[u8], pos: usize) -> Option<Header> {
    let (number, end) = unsigned(bytes, pos)?;
    let (generation, end) = unsigned(bytes, skip_space(bytes, end))?;
    let end = keyword(bytes, skip_space(bytes, end), b"obj")?;
    Some(Header {
        number: u32::try_from(number).ok()?,
        generation: u16::try_from(generation).ok()?,
        end,
        body: skip_space(bytes, end),
    })
}

/// A `startxref` line (ISO 32000-2 section 7.5.5).
#[derive(Clone, Copy, Debug)]
pub(super) struct Startxref {
    /// The offset of the cross-reference section that the line names.
    pub(super) offset: u64,
    /// The position after the offset.
    pub(super) value_end: usize,
    /// The position after the `%%EOF` marker that follows the offset, and
    /// after the end-of-line marker that follows it, if the marker is there.
    pub(super) eof_end: Option<usize>,
}

/// Reads the `startxref` line at `pos`: the keyword, an offset, and the
/// `%%EOF` marker that should follow it.
pub(super) fn startxref(bytes: &[u8], pos: usize) -> Option<Startxref> {
    let end = keyword(bytes, pos, b"startxref")?;
    let (offset, value_end) = unsigned(bytes, skip_space(bytes, end))?;
    let marker = skip_white(bytes, value_end);
    let eof_end = bytes[marker..]
        .starts_with(b"%%EOF")
        .then(|| skip_eol(bytes, marker + b"%%EOF".len()));
    Some(Startxref {
        offset,
        value_end,
        eof_end,
    })
}

/// Returns the position of the last `startxref` keyword of `bytes`.
pub(super) fn last_startxref(bytes: &[u8]) -> Option<usize> {
    const WORD: &[u8] = b"startxref";
    let mut end = bytes.len();
    while let Some(pos) = bytes[..end]
        .windows(WORD.len())
        .rposition(|window| window == WORD)
    {
        if keyword(bytes, pos, WORD).is_some() && (pos == 0 || !is_regular(bytes[pos - 1])) {
            return Some(pos);
        }
        end = pos;
    }
    None
}

/// The starts of the tokens of regular characters from a position on,
/// skipping white space, comments, and delimiters.
///
/// Every byte is examined once, so the tokens of any input take linear
/// time. The iterator does not follow strings, so a token inside a string
/// or inside a stream's data is a token too.
pub(super) struct Tokens<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Tokens<'a> {
    /// Returns the tokens of `bytes` from `pos` on.
    pub(super) const fn new(bytes: &'a [u8], pos: usize) -> Self {
        Self { bytes, pos }
    }

    /// Continues from `pos`, when that is past the current position.
    pub(super) fn skip_to(&mut self, pos: usize) {
        self.pos = self.pos.max(pos);
    }
}

impl Iterator for Tokens<'_> {
    type Item = usize;

    fn next(&mut self) -> Option<usize> {
        loop {
            let byte = *self.bytes.get(self.pos)?;
            if byte == b'%' {
                self.pos = comment_end(self.bytes, self.pos);
            } else if is_regular(byte) {
                let start = self.pos;
                self.pos = token_end(self.bytes, start);
                return Some(start);
            } else {
                self.pos += 1;
            }
        }
    }
}
