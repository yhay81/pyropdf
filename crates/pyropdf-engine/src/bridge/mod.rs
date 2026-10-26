//! The bridge that hands hayro a version of the store until hayro reads
//! versions directly (DESIGN.md section 6, I1 in yhay81/pyropdf#14).
//!
//! hayro reads the original bytes followed by an in-memory update: every
//! object that the version wrote, already serialized, and one complete
//! cross-reference stream. The stream lists every object of the version at
//! the store's locations and has no `/Prev`, so hayro never reads the
//! file's own cross-reference data, which it can read differently from the
//! store (a prefix before `%PDF-`, short table entries, free entries,
//! mismatched object-stream members, unknown entry types), and never
//! repairs on its own. An object that the store reads as absent is listed
//! free, which hayro reads as null. Unchanged objects are neither
//! serialized nor parsed; the bytes are copied once.
//!
//! For an encrypted base, the written objects are encrypted with the
//! base's security handler, as a save that keeps the encryption writes
//! them, and hayro opens the bytes with the password that unlocked the
//! base (yhay81/pyropdf#13).
//!
//! The bridge checks that hayro's page list is the store's page index, so
//! that a page number names the same page in both.

#[cfg(test)]
mod tests;

use std::fmt;
use std::sync::{Arc, Mutex, PoisonError, Weak};
use std::time::{Duration, Instant};

use hayro::hayro_syntax::Pdf;
use pyropdf_store::Password;
use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::index::Location;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::security::{self, Handler};
use pyropdf_store::value::{Dict, Name, Reference, Value};
use pyropdf_store::version::{Slot, Version};
use pyropdf_store::write;

/// Why the bridge or a render refused.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Error {
    /// The store refused: a malformed object, a limit, or a password.
    Store(pyropdf_store::Error),
    /// An encrypted version whose `/Encrypt` is not the base's, or that
    /// wrote the `/Encrypt` dictionary, which a save would encrypt anew.
    EncryptionChanged,
    /// A password that is not UTF-8, which hayro cannot take.
    PasswordNotUtf8,
    /// hayro could not read the bridged version.
    Unreadable,
    /// hayro's page list differs from the store's page index.
    PageMismatch {
        /// The pages of the store's index.
        store: usize,
        /// The pages that hayro lists.
        hayro: usize,
    },
    /// A page index beyond the last page.
    NoPage(usize),
}

/// The result of the bridge and of rendering.
pub type Result<T, E = Error> = std::result::Result<T, E>;

impl From<pyropdf_store::Error> for Error {
    fn from(error: pyropdf_store::Error) -> Self {
        Self::Store(error)
    }
}

impl From<pyropdf_store::LimitExceeded> for Error {
    fn from(error: pyropdf_store::LimitExceeded) -> Self {
        Self::Store(error.into())
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Store(error) => error.fmt(f),
            Self::EncryptionChanged => f.write_str(
                "a version whose encryption differs from the file's cannot be interpreted before it is saved",
            ),
            Self::PasswordNotUtf8 => {
                f.write_str("a document opened with a password that is not UTF-8 cannot be interpreted")
            }
            Self::Unreadable => f.write_str("hayro cannot read the document"),
            Self::PageMismatch { store, hayro } => write!(
                f,
                "hayro lists different pages ({hayro}) than the page tree ({store})"
            ),
            Self::NoPage(index) => write!(f, "the document has no page {index}"),
        }
    }
}

impl std::error::Error for Error {}

/// Where a version's cost went: the store's work, building the bytes, and
/// hayro's re-read of them.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    /// The time to build the bytes that hayro reads.
    pub store: Duration,
    /// The time that hayro took to read them, and the page-list check.
    pub hayro: Duration,
    /// The bytes that hayro reads.
    pub bytes: usize,
    /// The bytes of the update after the original bytes.
    pub update: usize,
}

/// A version as hayro reads it.
pub struct Bridged {
    pdf: Pdf,
    stats: Stats,
}

impl fmt::Debug for Bridged {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Bridged")
            .field("stats", &self.stats)
            .finish_non_exhaustive()
    }
}

impl Bridged {
    /// Builds hayro's reading of `version`, whose page index is `pages`.
    ///
    /// An unedited encrypted document goes to hayro as its original bytes,
    /// and an edited one with its written objects encrypted; hayro opens
    /// both with the password that unlocked the base.
    pub fn new(version: &Version, pages: &PageIndex, budget: &Budget) -> Result<Self> {
        let started = Instant::now();
        let handler = handler(version)?;
        let password = match handler {
            Some(handler) => {
                std::str::from_utf8(handler.password()).map_err(|_| Error::PasswordNotUtf8)?
            }
            None => "",
        };
        let (bytes, update) = match (handler, version.base().file()) {
            (Some(_), Some(file)) if !version.has_changes() => (copy(file.bytes(), budget)?, 0),
            _ => update(version, handler, budget)?,
        };
        let store = started.elapsed();
        let started = Instant::now();
        let len = bytes.len();
        let pdf = Pdf::new_with_password(bytes, password).map_err(|_| Error::Unreadable)?;
        check_pages(&pdf, pages)?;
        Ok(Self {
            pdf,
            stats: Stats {
                store,
                hayro: started.elapsed(),
                bytes: len,
                update,
            },
        })
    }

    /// Returns hayro's own reading of `bytes`, for tests that compare the
    /// bridge with it.
    #[cfg(test)]
    pub(crate) fn of_bytes(bytes: Vec<u8>) -> Self {
        Self {
            pdf: Pdf::new(bytes).expect("hayro reads the test file"),
            stats: Stats::default(),
        }
    }

    /// Returns hayro's document.
    #[must_use]
    pub const fn pdf(&self) -> &Pdf {
        &self.pdf
    }

    /// Returns where building it went.
    #[must_use]
    pub const fn stats(&self) -> Stats {
        self.stats
    }
}

/// The bridge of one version at a time: the latest version asked for. It
/// remembers the version by a weak reference, so it keeps no version alive
/// (DESIGN.md section 10).
#[derive(Default)]
pub struct Cache {
    entry: Mutex<Option<(Weak<Version>, Arc<Bridged>)>>,
}

impl Cache {
    /// Returns an empty cache.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Returns the bridge of `version`, building it when the cache holds
    /// another version's.
    pub fn get(
        &self,
        version: &Arc<Version>,
        pages: &PageIndex,
        budget: &Budget,
    ) -> Result<Arc<Bridged>> {
        if let Some((held, bridged)) = &*self.lock()
            && held.as_ptr() == Arc::as_ptr(version)
            && held.strong_count() > 0
        {
            return Ok(Arc::clone(bridged));
        }
        let bridged = Arc::new(Bridged::new(version, pages, budget)?);
        *self.lock() = Some((Arc::downgrade(version), Arc::clone(&bridged)));
        Ok(bridged)
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Option<(Weak<Version>, Arc<Bridged>)>> {
        self.entry.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Checks that hayro lists the pages of the store's index, by object.
fn check_pages(pdf: &Pdf, pages: &PageIndex) -> Result<()> {
    let theirs = pdf.pages();
    let mismatch = || Error::PageMismatch {
        store: pages.len(),
        hayro: theirs.len(),
    };
    if theirs.len() != pages.len() {
        return Err(mismatch());
    }
    for (page, ours) in theirs.iter().zip(pages.pages()) {
        let id = page.raw().obj_id().and_then(|id| {
            Some(Reference::new(
                u32::try_from(id.obj_number).ok()?,
                u16::try_from(id.gen_number).ok()?,
            ))
        });
        // A page in an object stream is generation 0 to hayro, as it is.
        if id.map(|r| r.number()) != ours.object().map(|r| r.number()) {
            return Err(mismatch());
        }
    }
    Ok(())
}

/// Returns the security handler of an encrypted version: the base's, when
/// the version keeps the base's `/Encrypt`.
fn handler(version: &Version) -> Result<Option<&Handler>> {
    let Some(encrypt) = version.trailer().get("Encrypt") else {
        return Ok(None);
    };
    let base = version.base();
    let file = base.file().ok_or(Error::EncryptionChanged)?;
    if base.trailer().get("Encrypt") != Some(encrypt)
        || matches!(encrypt, Value::Reference(r) if matches!(version.slot(r.number()), Some(Slot::Written(_))))
    {
        return Err(Error::EncryptionChanged);
    }
    file.security()
        .map(Some)
        .ok_or_else(|| pyropdf_store::Error::from(Password::Required).into())
}

fn copy(bytes: &[u8], budget: &Budget) -> Result<Vec<u8>> {
    let mut out = BudgetVec::new(budget);
    out.try_extend_from_slice(bytes)?;
    Ok(out.into_vec())
}

/// The widths of the fields of the cross-reference stream: the type, an
/// offset or object-stream number, and a generation or member index.
const WIDTHS: [usize; 3] = [1, 8, 4];

/// Returns the original bytes, or a header for a document without a file,
/// followed by the written objects and the complete cross-reference
/// stream; and the length of what follows the original bytes.
fn update(
    version: &Version,
    handler: Option<&Handler>,
    budget: &Budget,
) -> Result<(Vec<u8>, usize)> {
    let file = version.base().file();
    let mut out = BudgetVec::new(budget);
    match file {
        Some(file) => out.try_extend_from_slice(file.bytes())?,
        None => out.try_extend_from_slice(b"%PDF-1.7\n%\xE2\xE3\xCF\xD3\n")?,
    }
    let original = out.len();
    out.try_extend_from_slice(b"\n")?;
    let size = u32::try_from(version.next_number()).unwrap_or(u32::MAX);
    let mut rows: BudgetVec<'_, [u8; 13]> = BudgetVec::new(budget);
    rows.try_reserve_exact(size as usize)?;
    rows.try_push(row(0, 0, 65535))?;
    for number in 1..size {
        if number % 4096 == 0 {
            budget.poll()?;
        }
        let entry = match version.slot(number) {
            Some(Slot::Written(written)) => {
                let offset = out.len() as u64;
                header(&mut out, number, written.generation())?;
                match handler {
                    Some(handler) => {
                        let reference = Reference::new(number, written.generation());
                        let bytes =
                            security::encrypt_bytes(handler, reference, written.bytes(), budget)?;
                        out.try_extend_from_slice(&bytes)?;
                    }
                    None => out.try_extend_from_slice(written.bytes())?,
                }
                out.try_extend_from_slice(b"\nendobj\n")?;
                row(1, offset, u32::from(written.generation()))
            }
            Some(Slot::Free(_)) => row(0, 0, 0),
            None => base_row(version, number, &mut out, budget)?,
        };
        rows.try_push(entry)?;
    }
    let offset = out.len() as u64;
    let mut trailer = Dict::new();
    for (key, value) in version.trailer().iter() {
        if !matches!(
            key.as_bytes(),
            b"Prev"
                | b"XRefStm"
                | b"Type"
                | b"W"
                | b"Index"
                | b"Size"
                | b"Length"
                | b"Filter"
                | b"DecodeParms"
        ) {
            insert(&mut trailer, key.clone(), value.clone());
        }
    }
    insert(
        &mut trailer,
        Name::from("Type"),
        Value::Name(Name::from("XRef")),
    );
    insert(
        &mut trailer,
        Name::from("Size"),
        Value::Integer(i64::from(size)),
    );
    let widths = WIDTHS.map(|w| Value::Integer(w as i64));
    insert(
        &mut trailer,
        Name::from("W"),
        Value::Array(Vec::from(widths).into()),
    );
    let data_len = rows.len() * 13;
    insert(
        &mut trailer,
        Name::from("Length"),
        Value::Integer(data_len as i64),
    );
    // The stream's own object number is the version's next, which no
    // object uses; it is not listed, as ISO 32000-2 section 7.5.8.4 allows.
    header(&mut out, size, 0)?;
    write::value(&mut out, &Value::Dict(trailer))?;
    out.try_extend_from_slice(b"\nstream\n")?;
    for row in rows.iter() {
        out.try_extend_from_slice(row)?;
    }
    out.try_extend_from_slice(b"\nendstream\nendobj\nstartxref\n")?;
    out.try_extend_from_slice(offset.to_string().as_bytes())?;
    out.try_extend_from_slice(b"\n%%EOF\n")?;
    let update = out.len() - original;
    Ok((out.into_vec(), update))
}

/// Returns the row of object `number` of the base: its place in the file,
/// or, for a base without a file, its bytes written after the original.
fn base_row(
    version: &Version,
    number: u32,
    out: &mut BudgetVec<'_, u8>,
    budget: &Budget,
) -> Result<[u8; 13]> {
    let base = version.base();
    let Some(generation) = base.generation(number) else {
        return Ok(row(0, 0, 0));
    };
    let Some(file) = base.file() else {
        let Some(bytes) = base.object(Reference::new(number, generation), budget)? else {
            return Ok(row(0, 0, 0));
        };
        let offset = out.len() as u64;
        header(out, number, generation)?;
        out.try_extend_from_slice(&bytes)?;
        out.try_extend_from_slice(b"\nendobj\n")?;
        return Ok(row(1, offset, u32::from(generation)));
    };
    let Some(entry) = file.get(number) else {
        return Ok(row(0, 0, 0));
    };
    Ok(match entry.location() {
        Location::Offset(offset) => row(1, offset as u64, u32::from(generation)),
        Location::Member { stream, index } => {
            // A member that the stream's table does not confirm, or a
            // stream that does not decode, is absent to the store.
            match file.object_stream(stream, budget) {
                Ok(Some(objects)) if objects.mismatched().binary_search(&number).is_err() => {
                    row(2, u64::from(stream), index)
                }
                Err(error) if error.limit().is_some() => return Err(error.into()),
                _ => row(0, 0, 0),
            }
        }
    })
}

fn header(out: &mut BudgetVec<'_, u8>, number: u32, generation: u16) -> Result<()> {
    out.try_extend_from_slice(format!("{number} {generation} obj\n").as_bytes())?;
    Ok(())
}

/// Returns one row of the cross-reference stream, big-endian.
fn row(kind: u8, second: u64, third: u32) -> [u8; 13] {
    let mut row = [0; 13];
    row[0] = kind;
    row[1..9].copy_from_slice(&second.to_be_bytes());
    row[9..].copy_from_slice(&third.to_be_bytes());
    row
}

fn insert(dict: &mut Dict, key: Name, value: Value) {
    dict.insert(key, value);
}
