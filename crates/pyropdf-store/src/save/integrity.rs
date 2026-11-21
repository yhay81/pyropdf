//! Stream the covered prefix to the caller. Only the tail following the MAC
//! placeholder (e.g. a cross-reference stream) waits for the final length.

use sha2::{Digest, Sha256};
use std::io::{self, Write};

use super::{Error, Mode, Output, Result, StoreError};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::security::integrity::{self, Writer};
use crate::value::Value;
use crate::version::Version;

pub(super) fn plan(
    version: &Version,
    mode: &Mode<'_>,
    full: bool,
    linearize: bool,
    budget: &Budget,
) -> Result<Option<Writer>> {
    let original = version
        .base()
        .file()
        .map(|file| integrity::original_salt(file, budget))
        .transpose()?
        .flatten();
    if !full && !version.has_changes() {
        return Ok(None);
    }
    if full
        && original.is_some()
        && let Some(file) = version.base().file()
    {
        for revision in file.revisions() {
            budget.poll()?;
            if revision.sections().first().is_some_and(|s| matches!(s.trailer().get("AuthCode"),
                Some(Value::Dict(d)) if matches!(d.get("MACLocation"), Some(Value::Name(n)) if n.as_bytes() == b"AttachedToSig"))) {
                return Err(Error::Refused(super::Refusal::SignedMacRevision));
            }
        }
    }
    let key = match mode {
        Mode::New { handler, dict, .. } => {
            let salt = match dict.get("KDFSalt") {
                Some(Value::String(s)) => Some(
                    s.as_bytes()
                        .try_into()
                        .map_err(|_| StoreError::malformed("invalid new PDF MAC salt"))?,
                ),
                _ => None,
            };
            if original.is_some() && salt.is_none() {
                return Err(Error::Refused(super::Refusal::IntegrityProtection));
            }
            if salt.is_some() {
                check_declaration(version, budget)?;
            }
            salt.map(|s| (handler, s))
        }
        Mode::Keep {
            handler, encrypt, ..
        } => {
            if original.is_some() && encrypt.is_some_and(|n| version.slot(n).is_some()) {
                return Err(StoreError::malformed(
                    "cannot change the encryption dictionary of a PDF MAC revision in place",
                )
                .into());
            }
            if original.is_some() && version.has_changes() {
                check_declaration(version, budget)?;
            }
            original.map(|s| (handler.as_ref(), s))
        }
        Mode::Plain if original.is_some() => {
            return Err(Error::Refused(super::Refusal::IntegrityProtection));
        }
        Mode::Plain => None,
    };
    if key.is_some() && linearize {
        return Err(Error::Refused(super::Refusal::MacLinearization));
    }
    key.map(|(handler, salt)| Writer::new(handler, salt))
        .transpose()
        .map_err(Into::into)
}

// The extension revision is a PDF text string; these values contain ASCII
// only, but PDF 2.0 also permits UTF-16BE and UTF-8 with a byte-order mark.
fn text_equals(bytes: &[u8], expected: &[u8]) -> bool {
    match bytes {
        [0xfe, 0xff, rest @ ..] => {
            rest.len() == 2 * expected.len()
                && rest
                    .as_chunks::<2>()
                    .0
                    .iter()
                    .zip(expected)
                    .all(|(pair, byte)| *pair == [0, *byte])
        }
        [0xef, 0xbb, 0xbf, rest @ ..] => rest == expected,
        _ => bytes == expected,
    }
}

/// An ordinary edit may replace the catalog. Keep the required developer
/// extension on every new protected revision or refuse before emitting bytes.
fn check_declaration(version: &Version, budget: &Budget) -> Result<()> {
    use crate::tree::{catalog_of, resolve_in};
    let (_, catalog) = catalog_of(version, budget)?;
    let header_is_pdf2 = version.base().file().is_some_and(|file| {
        file.bytes()
            .get(file.origin()..)
            .is_some_and(|bytes| bytes.starts_with(b"%PDF-2.0"))
    });
    let catalog_is_pdf2 =
        matches!(catalog.get("Version"), Some(Value::Name(n)) if n.as_bytes() == b"2.0");
    if !header_is_pdf2 && !catalog_is_pdf2 {
        return Err(StoreError::malformed("PDF MAC requires PDF 2.0").into());
    }
    if let Some(Value::Dict(extensions)) = resolve_in(version, catalog.get("Extensions"), budget)?
        && let Some(Value::Array(entries)) = resolve_in(version, extensions.get("ISO_"), budget)?
    {
        for item in entries.iter() {
            budget.poll()?;
            if let Some(Value::Dict(entry)) = resolve_in(version, Some(item), budget)?
                && entry.get("ExtensionLevel") == Some(&Value::Integer(32004))
                && matches!(entry.get("BaseVersion"), Some(Value::Name(n)) if n.as_bytes() == b"2.0")
                && matches!(entry.get("ExtensionRevision"), Some(Value::String(s)) if text_equals(s.as_bytes(), b":2024"))
                && matches!(entry.get("URL"), Some(Value::String(s)) if s.as_bytes() == b"https://www.iso.org/standard/45877.html")
            {
                return Ok(());
            }
        }
    }
    Err(StoreError::malformed(
        "a protected revision must retain its ISO/TS 32004:2024 catalog extension",
    )
    .into())
}

pub(super) struct MacOutput<'b> {
    key: Writer,
    covered: Sha256,
    tail: Option<BudgetVec<'b, u8>>,
    // Absolute file offset just before the fixed-width /ByteRange.
    at: u64,
    token_size: usize,
}

impl<'b> MacOutput<'b> {
    pub(super) fn new(key: Writer) -> Self {
        Self {
            key,
            covered: Sha256::new(),
            tail: None,
            at: 0,
            token_size: 0,
        }
    }

    pub(super) fn write(&mut self, bytes: &[u8], out: &mut dyn Write) -> Result<()> {
        if let Some(tail) = &mut self.tail {
            tail.try_extend_from_slice(bytes)?;
        } else {
            self.covered.update(bytes);
            out.write_all(bytes)?;
        }
        Ok(())
    }

    pub(super) fn reserve(out: &mut Output<'b>) -> Result<()> {
        if out.mac.is_none() {
            return Ok(());
        }
        out.bytes(b" /AuthCode << /MACLocation /Standalone /ByteRange ")?;
        let mac = out.mac.as_mut().expect("a MAC is planned");
        mac.at = out.position;
        mac.token_size = mac.key.token(&[0; 32])?.len();
        let reserved = Self::range(0, 0, 0).len() + 2 + 2 * mac.token_size;
        out.budget
            .charge(LimitCode::PdfOutputSize, reserved as u64)?;
        out.position = out
            .position
            .checked_add(reserved as u64)
            .ok_or_else(|| StoreError::malformed("PDF MAC output position overflow"))?;
        mac.tail = Some(BudgetVec::new(out.budget));
        out.bytes(b" >>")
    }

    fn range(first: u64, end: u64, last: u64) -> String {
        format!("[0 {first:20} {end:20} {last:20}] /MAC ")
    }

    /// Finish the classic portion of a hybrid and hash its actual token into the next revision.
    pub(super) fn next_revision(out: &mut Output<'b>) -> Result<()> {
        let Some(current) = out.mac.take() else {
            return Ok(());
        };
        let mut next = Self::new(current.key.next_revision()?);
        next.covered = current.covered.clone();
        let mut sink = HashedWriter {
            target: out.target,
            hash: &mut next.covered,
        };
        current.finish(&mut sink, out.budget)?;
        out.mac = Some(next);
        Ok(())
    }

    pub(super) fn finish(mut self, target: &mut dyn Write, budget: &Budget) -> Result<()> {
        let tail = self
            .tail
            .take()
            .ok_or_else(|| StoreError::malformed("no trailer for the planned PDF MAC"))?;
        let first = self.at + Self::range(0, 0, 0).len() as u64;
        let after = first + (2 + self.token_size * 2) as u64;
        let ranges = Self::range(first, after, tail.len() as u64);
        self.covered.update(ranges.as_bytes());
        for part in tail.chunks(64 * 1024) {
            budget.poll()?;
            self.covered.update(part);
        }
        let token = self.key.token(&self.covered.finalize().into())?;
        if token.len() != self.token_size {
            return Err(
                StoreError::malformed("PDF MAC changed size after reserving its range").into(),
            );
        }
        target.write_all(ranges.as_bytes())?;
        target.write_all(b"<")?;
        const HEX: &[u8; 16] = b"0123456789ABCDEF";
        for byte in token {
            target.write_all(&[HEX[usize::from(byte >> 4)], HEX[usize::from(byte & 0xf)]])?;
        }
        target.write_all(b">")?;
        for part in tail.chunks(64 * 1024) {
            budget.poll()?;
            target.write_all(part)?;
        }
        Ok(())
    }
}

struct HashedWriter<'a> {
    target: &'a mut dyn Write,
    hash: &'a mut Sha256,
}

impl Write for HashedWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let length = self.target.write(bytes)?;
        self.hash.update(&bytes[..length]);
        Ok(length)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.target.flush()
    }
}
