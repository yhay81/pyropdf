//! Sparse Type 1 subsets. Keep the clear dictionary, all hints and subroutines,
//! and the original glyph order. Only length-prefixed unused outlines change.

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::ops::Range;

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, LimitCode};
use read_fonts::model::Error;
use read_fonts::ps::PredefinedEncoding;
use read_fonts::ps::charstring::{self, CharstringContext, CharstringKind, NullSink};
use read_fonts::ps::type1::Type1Font;
use read_fonts::types::Fixed;

use super::Subset;

const EEXEC: u16 = 55665;
const CHARSTRING: u16 = 4330;

fn cipher(
    bytes: impl Iterator<Item = u8>,
    mut seed: u16,
    encrypt: bool,
) -> impl Iterator<Item = u8> {
    bytes.map(move |byte| {
        let result = byte ^ (seed >> 8) as u8;
        seed = seed
            .wrapping_add(u16::from(if encrypt { result } else { byte }))
            .wrapping_mul(52845)
            .wrapping_add(22719);
        result
    })
}

struct Tokens<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Tokens<'a> {
    fn next(&mut self) -> Option<&'a [u8]> {
        loop {
            while self.data.get(self.pos)?.is_ascii_whitespace() {
                self.pos += 1;
            }
            if self.data[self.pos] != b'%' {
                break;
            }
            self.pos += self.data[self.pos..]
                .iter()
                .position(|byte| matches!(byte, b'\r' | b'\n'))?;
        }
        let start = self.pos;
        while self
            .data
            .get(self.pos)
            .is_some_and(|b| !b.is_ascii_whitespace())
        {
            self.pos += 1;
        }
        Some(&self.data[start..self.pos])
    }

    fn integer(&mut self) -> Option<i32> {
        std::str::from_utf8(self.next()?).ok()?.parse().ok()
    }
}

struct Entry {
    length: Range<usize>,
    data: Range<usize>,
    id: usize,
}

/// Admit only a single literal definition, without rewriting a computed dictionary.
fn definition(bytes: &[u8], name: &[u8]) -> Option<Option<usize>> {
    let mut results = bytes.windows(name.len()).enumerate().filter(|(i, word)| {
        *word == name
            && bytes
                .get(i + name.len())
                .is_some_and(u8::is_ascii_whitespace)
    });
    let first = results.next().map(|(i, _)| i + name.len());
    results.next().is_none().then_some(first)
}

#[expect(
    clippy::disallowed_methods,
    reason = "one range per parsed glyph, whose declared count is bounded against the input"
)]
fn entries(plain: &[u8], font: &Type1Font, budget: &Budget) -> Option<(i32, Vec<Entry>)> {
    let start = definition(plain, b"/CharStrings")??;
    let iv = match definition(&plain[..start], b"/lenIV")? {
        Some(pos) => Tokens { data: plain, pos }.integer()?,
        None => 4,
    };
    if iv < -1 || usize::try_from(iv).is_ok_and(|iv| iv > plain.len()) {
        return None;
    }
    let mut tokens = Tokens {
        data: plain,
        pos: start,
    };
    let capacity = usize::try_from(tokens.integer()?).ok()?;
    let count = font.num_glyphs() as usize;
    if count == 0 || capacity < count || count > plain.len() / 5 {
        return None;
    }
    if tokens.next()? != b"dict" {
        return None;
    }
    match tokens.next()? {
        b"begin" => (),
        b"dup" if tokens.next()? == b"begin" => (),
        _ => return None,
    }
    let mut names: BTreeMap<_, _> = font
        .glyph_names()
        .map(|(id, name)| (name, id.to_u32() as usize))
        .collect();
    let mut entries = Vec::with_capacity(count);
    for _ in 0..count {
        budget.poll().ok()?;
        let name = std::str::from_utf8(tokens.next()?.strip_prefix(b"/")?).ok()?;
        let id = names.remove(name)?;
        let length = tokens.next()?;
        let size: usize = std::str::from_utf8(length).ok()?.parse().ok()?;
        let length = tokens.pos - length.len()..tokens.pos;
        if !matches!(tokens.next()?, b"RD" | b"-|") {
            return None;
        }
        let data_start = tokens.pos.checked_add(1)?;
        let data_end = data_start.checked_add(size)?;
        plain.get(data_start..data_end)?;
        tokens.pos = data_end;
        match tokens.next()? {
            b"ND" | b"|-" | b"def" => (),
            b"noaccess" if tokens.next()? == b"def" => (),
            _ => return None,
        }
        entries.push(Entry {
            length,
            data: data_start..data_end,
            id,
        });
    }
    (tokens.next()? == b"end").then_some((iv, entries))
}

/// Fontations resolves subroutines, seac, and the selected MM weight vector.
struct Components<'a> {
    font: &'a Type1Font,
    codes: RefCell<[bool; 256]>,
}

impl CharstringContext for Components<'_> {
    fn kind(&self) -> CharstringKind {
        self.font.kind()
    }
    fn subr(&self, i: i32) -> Result<&[u8], Error> {
        self.font.subr(i)
    }
    fn global_subr(&self, i: i32) -> Result<&[u8], Error> {
        self.font.global_subr(i)
    }
    fn weight_vector(&self) -> &[Fixed] {
        self.font.weight_vector()
    }
    fn seac_components(&self, base: i32, accent: i32) -> Result<[&[u8]; 2], Error> {
        let outlines = self.font.seac_components(base, accent)?;
        let mut codes = self.codes.borrow_mut();
        codes[usize::from(u8::try_from(base).map_err(|_| Error::InvalidOperation)?)] = true;
        codes[usize::from(u8::try_from(accent).map_err(|_| Error::InvalidOperation)?)] = true;
        Ok(outlines)
    }
}

#[expect(
    clippy::disallowed_methods,
    reason = "one bit per validated glyph entry, and at most one decrypted copy of a single outline"
)]
fn referenced(
    font: &Type1Font,
    plain: &[u8],
    rows: &[Entry],
    iv: i32,
    keep: &[u32],
    budget: &Budget,
) -> Option<Vec<bool>> {
    let mut used = vec![false; rows.len()];
    used[0] = true;
    for &id in keep {
        *used.get_mut(id as usize)? = true;
    }
    let components = Components {
        font,
        codes: RefCell::new([false; 256]),
    };
    for row in rows.iter().filter(|row| used[row.id]) {
        budget.poll().ok()?;
        let raw = &plain[row.data.clone()];
        let decoded;
        let program = if iv < 0 {
            raw
        } else {
            {
                decoded = cipher(raw.iter().copied(), CHARSTRING, false).collect::<Vec<_>>();
            }
            decoded.get(iv as usize..)?
        };
        charstring::evaluate(&components, None, program, &mut NullSink).ok()?;
    }
    let codes = components.codes.into_inner();
    for (i, _) in codes.into_iter().enumerate().filter(|(_, needed)| *needed) {
        let name = PredefinedEncoding::Standard.name(i as u8);
        let (id, _) = font.glyph_names().find(|(_, n)| *n == name)?;
        used[id.to_u32() as usize] = true;
    }
    Some(used)
}

/// Returns a subset of a PDF FontFile with a binary eexec segment. Unrecognized
/// layouts and invalid lengths leave the original intact.
pub fn subset(
    data: &[u8],
    clear: usize,
    encrypted: usize,
    keep: &[u32],
    budget: &Budget,
) -> Result<Option<Subset>, LimitExceeded> {
    let size = u64::try_from(data.len()).unwrap_or(u64::MAX);
    budget.check(LimitCode::FontInputSize, size)?;
    // Parsed Fontations data, decoded bytes, the per-glyph name/range indexes,
    // one temporary charstring and the re-encrypted result.
    let scratch = size.saturating_mul(32);
    budget.charge(LimitCode::MemorySize, scratch)?;
    let result = rewrite(data, clear, encrypted, keep, budget);
    budget.release(LimitCode::MemorySize, scratch);
    budget.poll()?;
    Ok(result)
}

#[expect(
    clippy::disallowed_methods,
    reason = "the result never exceeds the source; lenIV is checked against the source and each retained charstring"
)]
fn rewrite(
    data: &[u8],
    clear: usize,
    encrypted: usize,
    keep: &[u32],
    budget: &Budget,
) -> Option<Subset> {
    let end = clear.checked_add(encrypted)?;
    let header = data.get(..clear)?;
    if !header.trim_ascii_end().ends_with(b"eexec") {
        return None;
    }
    let body = data.get(clear..end)?;
    if body.len() < 4 || body[..4].iter().all(u8::is_ascii_hexdigit) {
        return None;
    }
    let font = Type1Font::new(data).ok()?;
    let plain: Vec<u8> = cipher(body.iter().copied(), EEXEC, false).collect();
    let (iv, rows) = entries(&plain, &font, budget)?;
    let used = referenced(&font, &plain, &rows, iv, keep, budget)?;
    let kept = used.iter().filter(|&&bit| bit).count();
    let mut empty = vec![0; iv.max(0) as usize];
    empty.extend_from_slice(&[139, 139, 13, 14]); // 0 0 hsbw endchar
    if iv >= 0 {
        {
            empty = cipher(empty.into_iter(), CHARSTRING, true).collect();
        }
    }
    let length = empty.len().to_string();
    let mut new = Vec::with_capacity(data.len());
    new.extend_from_slice(header);
    let mut seed = EEXEC;
    let mut write = |bytes: &[u8]| {
        for &b in bytes {
            let cipher = b ^ (seed >> 8) as u8;
            new.push(cipher);
            seed = seed
                .wrapping_add(u16::from(cipher))
                .wrapping_mul(52845)
                .wrapping_add(22719);
        }
    };
    let mut last = 0;
    for row in &rows {
        budget.poll().ok()?;
        if !used[row.id] && row.data.len() > empty.len() {
            write(&plain[last..row.length.start]);
            write(length.as_bytes());
            write(&plain[row.length.end..row.data.start]);
            write(&empty);
            last = row.data.end;
        }
    }
    write(&plain[last..]);
    new.extend_from_slice(&data[end..]);
    Some(Subset {
        data: new,
        glyphs: used.len(),
        kept,
    })
}
