//! The documents that `python/pyropdf/_document.py` wraps: opening, the
//! complete checks, probing, and saving, with the GIL released while the
//! store works.

use std::cell::Cell;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::sync::{Arc, PoisonError, RwLock};
use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use pyropdf_engine::extract::{PageContent, PageLayout, TextPeak, read_page};
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};
use pyropdf_store::memo::Memo;
use pyropdf_store::pages::{Page, PageIndex, PageTree};
use pyropdf_store::save::{self, FileBase};
use pyropdf_store::security::Encryption;
use pyropdf_store::value::{Bytes, Dict, Name, Reference, Value};
use pyropdf_store::version::{Document, MemoryBase, Version};
use pyropdf_store::{Error, write};

use crate::errors::{DocumentClosedError, MalformedPdfError, save_error, store_error};
use crate::history::DEFAULT_HISTORY_LIMIT;
use crate::source;

/// The metadata text that `probe` returns, in UTF-8 bytes at most.
const MAX_PROBE_METADATA_SIZE: usize = 1024 * 1024;

/// A document of the store, which `close` releases.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct _Document {
    document: RwLock<Option<Arc<Document>>>,
    limits: DocumentLimits,
    /// The page index, memoized by what its walk read.
    pub(crate) pages: Memo<PageTree, PageIndex>,
    /// The page index of what an open transaction has written, kept apart
    /// so that the snapshots of transactions never displace the index of
    /// committed versions.
    pub(crate) draft_pages: Memo<PageTree, PageIndex>,
    /// The text, layouts, and content of pages, memoized with what
    /// interpreting each page may read.
    results: Memo<PageKey, PageResult>,
    /// The results of what an open transaction has written, kept apart as
    /// its page index is.
    draft_results: Memo<PageKey, PageResult>,
    /// hayro's reading of the latest version rendered.
    pub(crate) bridge: pyropdf_engine::bridge::Cache,
    /// The open transaction and the names of the history's steps.
    pub(crate) history: crate::history::State,
}

impl _Document {
    fn new(document: Document, limits: DocumentLimits) -> Self {
        Self {
            document: RwLock::new(Some(Arc::new(document))),
            limits,
            pages: Memo::new(1),
            draft_pages: Memo::new(1),
            results: Memo::new(RESULT_CAPACITY),
            draft_results: Memo::new(RESULT_CAPACITY),
            bridge: pyropdf_engine::bridge::Cache::new(),
            history: crate::history::State::default(),
        }
    }

    /// Returns the store's document, or refuses a closed one.
    pub(crate) fn document(&self) -> PyResult<Arc<Document>> {
        self.document
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
            .ok_or_else(|| DocumentClosedError::new_err("document closed"))
    }

    pub(crate) fn current(&self) -> PyResult<Arc<Version>> {
        Ok(self.document()?.current())
    }

    /// Replaces the store's document, as unlocking does, and forgets what
    /// was derived from the old one.
    pub(crate) fn replace(&self, document: Document) {
        *self
            .document
            .write()
            .unwrap_or_else(PoisonError::into_inner) = Some(Arc::new(document));
        self.pages.clear();
        self.draft_pages.clear();
        self.results.clear();
        self.draft_results.clear();
        self.history.clear();
    }

    /// Returns a budget of one operation from the document's limits.
    pub(crate) fn budget(&self) -> Budget {
        Budget::new(&self.limits)
    }
}

#[pymethods]
impl _Document {
    /// Returns an empty document: a catalog and a page tree without pages.
    #[staticmethod]
    fn empty(limits: &Bound<'_, PyAny>) -> PyResult<Self> {
        let mut base = MemoryBase::new(
            [
                (Name::from("Root"), Value::from(Reference::new(1, 0))),
                (Name::from("Size"), Value::from(3)),
            ]
            .into_iter()
            .collect::<Dict>(),
        );
        base.insert(
            Reference::new(1, 0),
            Bytes::new(&b"<</Type/Catalog/Pages 2 0 R>>"[..]),
        );
        base.insert(
            Reference::new(2, 0),
            Bytes::new(&b"<</Type/Pages/Kids[]/Count 0>>"[..]),
        );
        Ok(Self::new(
            Document::new(Arc::new(base), Some(DEFAULT_HISTORY_LIMIT)),
            document_limits(limits)?,
        ))
    }

    /// Opens the file at `path`, unlocking it with `password`.
    #[staticmethod]
    #[pyo3(signature = (path, limits, password=None))]
    fn open_path(
        py: Python<'_>,
        path: PathBuf,
        limits: &Bound<'_, PyAny>,
        password: Option<Vec<u8>>,
    ) -> PyResult<Self> {
        let (limits, complete) = (document_limits(limits)?, complete_checks(limits)?);
        py.detach(|| {
            let budget = Budget::new(&limits);
            let bytes = source::file(&path, &budget)?;
            open(
                bytes,
                password.as_deref(),
                limits.clone(),
                complete,
                &budget,
            )
        })
    }

    /// Opens the bytes of a buffer: `bytes` without a copy, other buffers
    /// with one.
    #[staticmethod]
    #[pyo3(signature = (data, limits, password=None))]
    fn open_buffer(
        py: Python<'_>,
        data: &Bound<'_, PyAny>,
        limits: &Bound<'_, PyAny>,
        password: Option<Vec<u8>>,
    ) -> PyResult<Self> {
        let (limits, complete) = (document_limits(limits)?, complete_checks(limits)?);
        let bytes = source::buffer(data, &Budget::new(&limits))?;
        py.detach(|| {
            open(
                bytes,
                password.as_deref(),
                limits.clone(),
                complete,
                &Budget::new(&limits),
            )
        })
    }

    /// Returns whether opening recovered damaged cross-reference data.
    fn is_repaired(&self) -> PyResult<bool> {
        Ok(self
            .current()?
            .base()
            .file()
            .is_some_and(|index| index.is_repaired()))
    }

    /// Writes the document to the file at `path`, created or truncated; the
    /// caller replaces the target with it.
    #[pyo3(signature = (path, incremental, encryption=None, prune=false, compress=None, decompress=false, renew_id=false, cross_reference_stream=None, object_streams=false, renumber=false, dedupe=false))]
    #[expect(clippy::too_many_arguments, reason = "the options of Document.save")]
    fn save_path(
        &self,
        py: Python<'_>,
        path: PathBuf,
        incremental: bool,
        encryption: Option<&Bound<'_, PyAny>>,
        prune: bool,
        compress: Option<u8>,
        decompress: bool,
        renew_id: bool,
        cross_reference_stream: Option<bool>,
        object_streams: bool,
        renumber: bool,
        dedupe: bool,
    ) -> PyResult<()> {
        let options = save_options(
            prune,
            compress,
            decompress,
            renew_id,
            cross_reference_stream,
            object_streams,
            renumber,
            dedupe,
        );
        let (version, encryption) = crate::security::prepare(py, self, incremental, encryption)?;
        let budget = Budget::new(&self.limits).with_limit(LimitCode::PdfOutputSize, None);
        py.detach(|| {
            let target = path.display().to_string();
            let result = (|| {
                let mut out = BufWriter::new(File::create(&path)?);
                write_version(
                    &version,
                    &mut out,
                    incremental,
                    encryption.as_ref(),
                    options,
                    &budget,
                )?;
                let file = out.into_inner().map_err(|e| e.into_error())?;
                file.sync_all()?;
                Ok(())
            })();
            result.map_err(|error: save::Error| save_error(&error, &target))
        })
    }

    /// Returns the document as PDF bytes, at most `max_size` of them.
    #[pyo3(signature = (incremental, max_size, encryption=None, prune=false, compress=None, decompress=false, renew_id=false, cross_reference_stream=None, object_streams=false, renumber=false, dedupe=false))]
    #[expect(clippy::too_many_arguments, reason = "the options of Document.save")]
    fn to_bytes<'py>(
        &self,
        py: Python<'py>,
        incremental: bool,
        max_size: Option<u64>,
        encryption: Option<&Bound<'_, PyAny>>,
        prune: bool,
        compress: Option<u8>,
        decompress: bool,
        renew_id: bool,
        cross_reference_stream: Option<bool>,
        object_streams: bool,
        renumber: bool,
        dedupe: bool,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let options = save_options(
            prune,
            compress,
            decompress,
            renew_id,
            cross_reference_stream,
            object_streams,
            renumber,
            dedupe,
        );
        let (version, encryption) = crate::security::prepare(py, self, incremental, encryption)?;
        let budget = Budget::new(&self.limits).with_limit(LimitCode::PdfOutputSize, max_size);
        let out = py.detach(|| {
            let mut out = Vec::new();
            write_version(
                &version,
                &mut out,
                incremental,
                encryption.as_ref(),
                options,
                &budget,
            )
            .map_err(|error| save_error(&error, "PDF bytes"))?;
            Ok::<_, PyErr>(out)
        })?;
        Ok(PyBytes::new(py, &out))
    }

    /// Returns each object of the current version that the cross-reference
    /// data lists, as its number, generation, and canonical syntax. Tests
    /// compare documents in value with it.
    fn _objects<'py>(&self, py: Python<'py>) -> PyResult<Vec<(u32, u16, Bound<'py, PyBytes>)>> {
        let version = self.reading()?;
        let budget = Budget::new(&self.limits);
        let mut objects = Vec::new();
        let numbers: Vec<u32> = match version.base().file() {
            Some(index) => index.entries().map(|(number, _)| number).collect(),
            None => Vec::new(),
        };
        for number in numbers {
            let Some(object) = version.get(number, &budget).map_err(|e| store_error(&e))? else {
                continue;
            };
            let mut out = pyropdf_store::budget::BudgetVec::new(&budget);
            write::value(&mut out, object.value()).map_err(|e| store_error(&e))?;
            objects.push((
                number,
                object.generation(),
                PyBytes::new(py, &out.into_vec()),
            ));
        }
        Ok(objects)
    }

    /// Releases the document. Later operations raise `DocumentClosedError`.
    fn close(&self) {
        self.document
            .write()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        self.pages.clear();
        self.draft_pages.clear();
        self.results.clear();
        self.draft_results.clear();
        self.history.clear();
    }
}

/// Reads the index of `bytes`, and with `complete` every object, within
/// `budget`.
fn open(
    bytes: Bytes,
    password: Option<&[u8]>,
    limits: DocumentLimits,
    complete: bool,
    budget: &Budget,
) -> PyResult<_Document> {
    let base = open_base(bytes, password, budget).map_err(|e| store_error(&e))?;
    // A locked document's objects cannot be read until it is unlocked.
    let locked = base.index().is_encrypted() && base.security().is_none();
    let document = Document::new(Arc::new(base), Some(DEFAULT_HISTORY_LIMIT));
    if complete && !locked {
        check_every_object(&document.current(), budget).map_err(|e| store_error(&e))?;
    }
    Ok(_Document::new(document, limits))
}

/// Opens `bytes` with `password`. A wrong password still opens a document
/// whose user password is empty, as MuPDF and pylopdf open it.
pub(crate) fn open_base(
    bytes: Bytes,
    password: Option<&[u8]>,
    budget: &Budget,
) -> Result<FileBase, Error> {
    match FileBase::open_with_password(bytes.clone(), password, budget) {
        Err(error)
            if error.kind()
                == &pyropdf_store::ErrorKind::Password(pyropdf_store::Password::Incorrect) =>
        {
            match FileBase::open(bytes, budget) {
                Ok(base) if base.security().is_some() => Ok(base),
                _ => Err(error),
            }
        }
        result => result,
    }
}

/// Parses every object that the index lists, which surfaces damage and the
/// limits of objects up front: what `DocumentLimits.web()` asks at open.
fn check_every_object(version: &Version, budget: &Budget) -> Result<(), Error> {
    let Some(index) = version.base().file() else {
        return Ok(());
    };
    for (number, _) in index.entries() {
        budget.poll()?;
        version.get(number, budget)?;
    }
    Ok(())
}

fn write_version(
    version: &Version,
    out: &mut dyn Write,
    incremental: bool,
    encryption: Option<&Encryption>,
    options: save::Options,
    budget: &Budget,
) -> save::Result<()> {
    if incremental {
        save::incremental_with(version, out, budget, options)?;
    } else {
        save::full_with(version, out, budget, encryption, options)?;
    }
    Ok(())
}

/// Returns the options of a save that prunes when `prune`, compresses at the
/// level `compress` when it is given, decompresses when `decompress`, renews
/// the file identifier when `renew_id`, writes a cross-reference stream or a
/// table as `cross_reference_stream` asks or else the base's form, packs
/// objects into object streams when `object_streams`, renumbers the objects
/// when `renumber`, and merges identical ones when `dedupe`.
#[expect(clippy::too_many_arguments, reason = "the options of Document.save")]
const fn save_options(
    prune: bool,
    compress: Option<u8>,
    decompress: bool,
    renew_id: bool,
    cross_reference_stream: Option<bool>,
    object_streams: bool,
    renumber: bool,
    dedupe: bool,
) -> save::Options {
    let streams = match (compress, decompress) {
        (Some(level), _) => save::Streams::Compress(level),
        (None, true) => save::Streams::Decompress,
        (None, false) => save::Streams::Keep,
    };
    save::Options {
        prune,
        streams,
        renew_id,
        cross_reference: match cross_reference_stream {
            None => save::CrossReference::Base,
            Some(false) => save::CrossReference::Table,
            Some(true) => save::CrossReference::Stream,
        },
        object_streams,
        renumber,
        dedupe,
    }
}

/// Reads the fields of a Python `DocumentLimits`.
fn document_limits(limits: &Bound<'_, PyAny>) -> PyResult<DocumentLimits> {
    let field = |name: &str| limits.getattr(name)?.extract::<Option<u64>>();
    let duration = limits.getattr("max_duration")?.extract::<Option<f64>>()?;
    Ok(DocumentLimits {
        max_file_size: field("max_file_size")?,
        max_pages: field("max_pages")?,
        max_objects: field("max_objects")?,
        max_decompressed_size: field("max_decompressed_size")?,
        max_page_content_size: field("max_page_content_size")?,
        max_total_decompressed_size: field("max_total_decompressed_size")?,
        max_object_depth: field("max_object_depth")?,
        max_interpretation_size: field("max_interpretation_size")?,
        max_duration: duration
            .map(Duration::try_from_secs_f64)
            .transpose()
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?,
        cancellation: limits
            .getattr("cancellation")?
            .extract::<Option<PyRef<'_, crate::cancel::CancellationToken>>>()?
            .map(|token| token.0.clone()),
    })
}

fn complete_checks(limits: &Bound<'_, PyAny>) -> PyResult<bool> {
    limits.getattr("check_at_open")?.extract()
}

/// Returns the fields of `DocumentLimits.web()` by name, so that Python and
/// Rust share one profile.
#[pyfunction]
pub fn web_limits(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let web = DocumentLimits::web();
    let fields = PyDict::new(py);
    for (name, value) in [
        ("max_file_size", web.max_file_size),
        ("max_pages", web.max_pages),
        ("max_objects", web.max_objects),
        ("max_decompressed_size", web.max_decompressed_size),
        ("max_page_content_size", web.max_page_content_size),
        (
            "max_total_decompressed_size",
            web.max_total_decompressed_size,
        ),
        ("max_object_depth", web.max_object_depth),
        ("max_interpretation_size", web.max_interpretation_size),
    ] {
        fields.set_item(name, value)?;
    }
    fields.set_item("max_duration", web.max_duration.map(|d| d.as_secs_f64()))?;
    Ok(fields)
}

/// What `probe` reads: the page count, the header's version, whether the
/// document is encrypted, whether its index was repaired, and the text
/// entries of its Info dictionary.
type Probe<'py> = (u64, String, bool, bool, Bound<'py, PyDict>);

/// Reads the index of the file at `path` or of the buffer `data`, and the
/// few objects that a probe needs.
#[pyfunction]
#[pyo3(signature = (path, data, max_file_size, password=None))]
pub fn probe<'py>(
    py: Python<'py>,
    path: Option<PathBuf>,
    data: Option<&Bound<'py, PyAny>>,
    max_file_size: Option<u64>,
    password: Option<Vec<u8>>,
) -> PyResult<Probe<'py>> {
    let limits = DocumentLimits {
        max_file_size,
        ..DocumentLimits::default()
    };
    let budget = Budget::new(&limits);
    let bytes = match (path, data) {
        (_, Some(data)) => source::buffer(data, &budget)?,
        (Some(path), None) => py.detach(|| source::file(&path, &budget))?,
        (None, None) => return Err(pyo3::exceptions::PyTypeError::new_err("no source")),
    };
    let (count, version, encrypted, repaired, metadata) =
        py.detach(|| read_probe(bytes, password.as_deref(), &budget))?;
    let dict = PyDict::new(py);
    for (key, value) in metadata {
        dict.set_item(key, value)?;
    }
    Ok((count, version, encrypted, repaired, dict))
}

/// The Info dictionary entries that `probe` reports.
const PROBE_KEYS: [&str; 8] = [
    "Title",
    "Author",
    "Subject",
    "Keywords",
    "Creator",
    "Producer",
    "CreationDate",
    "ModDate",
];

type ProbeData = (u64, String, bool, bool, Vec<(&'static str, String)>);

fn read_probe(bytes: Bytes, password: Option<&[u8]>, budget: &Budget) -> PyResult<ProbeData> {
    use pyropdf_store::version::Base as _;
    let store = |e: Error| store_error(&e);
    let base = open_base(bytes, password, budget).map_err(store)?;
    let index = base.index();
    let header = header_version(&index.bytes()[index.origin()..])
        .ok_or_else(|| MalformedPdfError::new_err("the file has no %PDF- header"))?;
    let encrypted = index.is_encrypted() && base.security().is_none();
    let repaired = index.is_repaired();
    // A locked document's catalog and page tree's /Count read as they are
    // in the file, so that it reports its pages too; an unlocked one reads
    // decrypted.
    let resolve = |value: Option<&Value>| -> Result<Option<Arc<Value>>, Error> {
        let read = |reference| {
            if encrypted {
                index.object(reference, budget)
            } else {
                base.object(reference, budget)
            }
        };
        match value {
            Some(Value::Reference(reference)) => Ok(read(*reference)?
                .map(|bytes| Value::parse(&bytes, budget))
                .transpose()?
                .map(Arc::new)),
            Some(value) => Ok(Some(Arc::new(value.clone()))),
            None => Ok(None),
        }
    };
    let malformed = || MalformedPdfError::new_err("the document has no page tree with a /Count");
    let root = resolve(index.trailer().get("Root")).map_err(store)?;
    let Some(Value::Dict(root)) = root.as_deref() else {
        return Err(malformed());
    };
    let pages = resolve(root.get("Pages")).map_err(store)?;
    let Some(Value::Dict(pages)) = pages.as_deref() else {
        return Err(malformed());
    };
    let count = match pages.get("Count") {
        Some(&Value::Integer(count)) => u64::try_from(count).map_err(|_| malformed())?,
        _ => return Err(malformed()),
    };
    let mut metadata = Vec::new();
    // The strings of an encrypted document's Info dictionary are encrypted;
    // the security handler (issue #28) decrypts them.
    if !encrypted {
        let info = resolve(index.trailer().get("Info")).map_err(store)?;
        if let Some(Value::Dict(info)) = info.as_deref() {
            let mut size = 0;
            for key in PROBE_KEYS {
                let Some(Value::String(text)) = info.get(key) else {
                    continue;
                };
                let text = text_string(text.as_bytes());
                size += text.len();
                if size > MAX_PROBE_METADATA_SIZE {
                    break;
                }
                metadata.push((key, text));
            }
        }
    }
    Ok((count, header, encrypted, repaired, metadata))
}

/// Returns the version of the document's `%PDF-x.y` header, such as
/// `"1.4"`, or `None` for a document that was not read from a file or whose
/// header names no version.
#[pyfunction]
#[pyo3(name = "header_version")]
pub fn document_header_version(document: &_Document) -> PyResult<Option<String>> {
    let version = document.reading()?;
    Ok(version
        .base()
        .file()
        .and_then(|index| header_version(&index.bytes()[index.origin()..])))
}

/// Returns the version of a `%PDF-x.y` header, such as `"1.7"`.
pub(crate) fn header_version(bytes: &[u8]) -> Option<String> {
    let rest = bytes.strip_prefix(b"%PDF-")?;
    let end = rest
        .iter()
        .position(|&b| !(b.is_ascii_digit() || b == b'.'))
        .unwrap_or(rest.len());
    let version = std::str::from_utf8(&rest[..end]).ok()?;
    let (major, minor) = version.split_once('.')?;
    (!major.is_empty() && !minor.is_empty() && !minor.contains('.')).then(|| version.to_owned())
}

/// Decodes a text string: UTF-16BE or UTF-8 after their byte order marks,
/// and PDFDocEncoding otherwise (ISO 32000-2 section 7.9.2.2).
pub(crate) fn text_string(bytes: &[u8]) -> String {
    if let Some(rest) = bytes.strip_prefix(&[0xFE, 0xFF]) {
        let units = rest
            .as_chunks::<2>()
            .0
            .iter()
            .map(|&pair| u16::from_be_bytes(pair));
        return char::decode_utf16(units)
            .map(|c| c.unwrap_or(char::REPLACEMENT_CHARACTER))
            .collect();
    }
    if let Some(rest) = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]) {
        return String::from_utf8_lossy(rest).into_owned();
    }
    bytes.iter().map(|&b| pdf_doc_char(b)).collect()
}

/// The characters of PDFDocEncoding from 0x80 to 0xA0 (ISO 32000-2 table
/// D.2); 0x9F is undefined.
const PDF_DOC_HIGH: [char; 33] = [
    '\u{2022}', '\u{2020}', '\u{2021}', '\u{2026}', '\u{2014}', '\u{2013}', '\u{0192}', '\u{2044}',
    '\u{2039}', '\u{203A}', '\u{2212}', '\u{2030}', '\u{201E}', '\u{201C}', '\u{201D}', '\u{2018}',
    '\u{2019}', '\u{201A}', '\u{2122}', '\u{FB01}', '\u{FB02}', '\u{0141}', '\u{0152}', '\u{0160}',
    '\u{0178}', '\u{017D}', '\u{0131}', '\u{0142}', '\u{0153}', '\u{0161}', '\u{017E}', '\u{FFFD}',
    '\u{20AC}',
];

/// The characters of PDFDocEncoding from 0x18 to 0x1F.
const PDF_DOC_ACCENTS: [char; 8] = [
    '\u{02D8}', '\u{02C7}', '\u{02C6}', '\u{02D9}', '\u{02DD}', '\u{02DB}', '\u{02DA}', '\u{02DC}',
];

fn pdf_doc_char(byte: u8) -> char {
    match byte {
        0x18..=0x1F => PDF_DOC_ACCENTS[usize::from(byte - 0x18)],
        0x7F => char::REPLACEMENT_CHARACTER,
        0x80..=0xA0 => PDF_DOC_HIGH[usize::from(byte - 0x80)],
        _ => char::from(byte),
    }
}

/// The page results that a document memoizes at most, each as large as the
/// limits of the call that computed it allow.
const RESULT_CAPACITY: usize = 8;

/// What a memoized page result is: the page, and what is derived from it
/// with every option that changes it.
#[derive(Clone, PartialEq, Eq, Hash)]
pub(crate) struct PageKey {
    pub(crate) page: Reference,
    pub(crate) kind: PageKind,
}

/// What is derived from a page, with its options. A box compares by the
/// bits of its coordinates.
#[derive(Clone, PartialEq, Eq, Hash)]
pub(crate) enum PageKind {
    Text(crate::text::TextKey),
    Layout(crate::text::TextKey, bool),
    Content {
        kinds: [bool; 3],
        annotations: bool,
        clip: Option<[u64; 4]>,
    },
}

/// A page's derived result, with the most of the call's limits that
/// computing it held.
pub(crate) enum PageResult {
    Text {
        text: String,
        skipped: usize,
        peak: TextPeak,
    },
    Layout(PageLayout),
    Content {
        content: PageContent,
        elements: u64,
    },
}

/// Returns how many page results the document's memos reused and how many
/// they computed, for tests.
#[pyfunction]
pub fn page_result_counts(document: &_Document) -> (u64, u64) {
    let (results, drafts) = (document.results.stats(), document.draft_results.stats());
    (
        results.hits + drafts.hits,
        results.computations + drafts.computations,
    )
}

/// A failed computation of a page's result.
struct Failed(PyErr);

impl From<Error> for Failed {
    fn from(error: Error) -> Self {
        Self(store_error(&error))
    }
}

impl _Document {
    /// Returns the result `key` of `page` on `version`: one that the memo
    /// holds while the changes since miss what interpreting the page may
    /// read ([`read_page`]), or else what `compute` returns.
    ///
    /// A held result that `fits` refuses, because the call's limits are
    /// smaller than those it was computed under, is computed again, so that
    /// the call refuses as a fresh computation does. A result that this call
    /// computed fits by construction, and its charges stay with the budget.
    pub(crate) fn page_result(
        &self,
        key: &PageKey,
        version: &Arc<Version>,
        page: &Page,
        budget: &Budget,
        fits: impl Fn(&PageResult) -> bool,
        compute: impl Fn() -> PyResult<PageResult>,
    ) -> PyResult<Arc<PageResult>> {
        let memo = if self.in_transaction() {
            &self.draft_results
        } else {
            &self.results
        };
        let computed = Cell::new(false);
        let result = memo
            .get_or_compute(key, version, budget, |reads| {
                computed.set(true);
                read_page(reads, page)?;
                compute().map_err(Failed)
            })
            .map_err(|Failed(error)| error)?;
        if computed.get() || fits(&result) {
            Ok(result)
        } else {
            compute().map(Arc::new)
        }
    }
}
