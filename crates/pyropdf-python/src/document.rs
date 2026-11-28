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
use pyropdf_engine::annotations::AppearanceSettler;
use pyropdf_engine::extract::{PageContent, PageLayout, TextPeak, read_page};
use pyropdf_engine::fonts::settle::FontSubsetSettler;
pub(crate) use pyropdf_engine::forms::text_string;
use pyropdf_engine::forms::{FillCache, Form};
use pyropdf_engine::navigation::CountCache;
use pyropdf_engine::render::Fallback;
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode, SharedBudget};
use pyropdf_store::memo::Memo;
use pyropdf_store::pages::{Page, PageIndex, PageTree};
use pyropdf_store::save::{self, FileBase};
use pyropdf_store::security::Encryption;
use pyropdf_store::settle::{Settlers, Structure};
use pyropdf_store::value::{Bytes, Dict, Name, Reference, Value};
use pyropdf_store::version::{Document, MemoryBase, Version};
use pyropdf_store::{Error, write};

use crate::annotations::links::NamedDestinations;
use crate::errors::{DocumentClosedError, MalformedPdfError, limit_error, save_error, store_error};
use crate::history::DEFAULT_HISTORY_LIMIT;
use crate::outline::Outline;
use crate::source;

/// The metadata text that `probe` returns, in UTF-8 bytes at most.
const MAX_PROBE_METADATA_SIZE: usize = 1024 * 1024;

/// A document of the store, which `close` releases.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct _Document {
    document: RwLock<Option<Arc<Document>>>,
    limits: DocumentLimits,
    /// Whether opening requested parsing every object up front (`check_at_open`).
    check_at_open: bool,
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
    /// The named destinations, memoized by what indexing them read.
    pub(crate) destinations: Memo<(), NamedDestinations>,
    /// The named destinations of what an open transaction has written.
    pub(crate) draft_destinations: Memo<(), NamedDestinations>,
    /// The items of the outline, memoized by what walking them read.
    pub(crate) outline: Memo<(), Outline>,
    /// The outline items of what an open transaction has written.
    pub(crate) draft_outline: Memo<(), Outline>,
    /// Verified outline counts, with the dependencies of their recount.
    pub(crate) outline_counts: CountCache,
    /// The terminal fields of the form, memoized by what walking its field
    /// tree read.
    pub(crate) form: Memo<(), Form>,
    /// The form fields of what an open transaction has written.
    pub(crate) draft_form: Memo<(), Form>,
    /// The fill state of committed and transaction versions, independently.
    pub(crate) fill_index: FillCache,
    pub(crate) draft_fill_index: FillCache,
    /// hayro's reading of the latest version rendered.
    pub(crate) bridge: pyropdf_engine::bridge::Cache,
    /// F4-memoized per-page pre-flight nesting guard before hayro (#1241).
    pub(crate) preflight: pyropdf_engine::bridge::PreflightCache,
    /// Pre-flight results for what an open transaction has written.
    pub(crate) draft_preflight: pyropdf_engine::bridge::PreflightCache,
    /// The faces that render the CJK fonts that the document does not
    /// embed, which `python/pyropdf/_fonts.py` sets; a setting of this
    /// object, not of the document, so unlocking keeps it.
    fallback: RwLock<Fallback>,
    /// The reverse-reference index of the latest version that a structural
    /// edit read.
    pub(crate) references: pyropdf_store::refs::Cache,
    /// The open transaction and the names of the history's steps.
    pub(crate) history: Arc<crate::history::State>,
}

/// Registers the settlers that pyropdf's engines provide (DESIGN.md
/// section 7).
fn with_settlers(document: Document) -> Document {
    document.set_settlers(
        Settlers::default()
            .with(Structure::Appearance, Arc::new(AppearanceSettler))
            .with(Structure::FontSubset, Arc::new(FontSubsetSettler)),
    );
    document
}

impl _Document {
    pub(crate) fn new(document: Document, limits: DocumentLimits) -> Self {
        Self::with_history(document, limits, false, crate::history::State::default())
    }

    pub(crate) fn new_checked(
        document: Document,
        limits: DocumentLimits,
        check_at_open: bool,
    ) -> Self {
        Self::with_history(
            document,
            limits,
            check_at_open,
            crate::history::State::default(),
        )
    }

    pub(crate) const fn check_at_open(&self) -> bool {
        self.check_at_open
    }

    /// Returns the document's configured operational limits.
    pub(crate) const fn limits(&self) -> &DocumentLimits {
        &self.limits
    }

    fn with_history(
        document: Document,
        limits: DocumentLimits,
        check_at_open: bool,
        history: crate::history::State,
    ) -> Self {
        let document = with_settlers(document);
        Self {
            document: RwLock::new(Some(Arc::new(document))),
            limits,
            check_at_open,
            pages: Memo::new(1),
            draft_pages: Memo::new(1),
            results: Memo::new(RESULT_CAPACITY),
            draft_results: Memo::new(RESULT_CAPACITY),
            destinations: Memo::new(1),
            draft_destinations: Memo::new(1),
            outline: Memo::new(1),
            draft_outline: Memo::new(1),
            outline_counts: CountCache::default(),
            form: Memo::new(1),
            draft_form: Memo::new(1),
            fill_index: FillCache::new(),
            draft_fill_index: FillCache::new(),
            bridge: pyropdf_engine::bridge::Cache::new(),
            preflight: pyropdf_engine::bridge::PreflightCache::default(),
            draft_preflight: pyropdf_engine::bridge::PreflightCache::default(),
            fallback: RwLock::new(Fallback::default()),
            references: pyropdf_store::refs::Cache::new(),
            history: Arc::new(history),
        }
    }

    /// A private view for catalog reads after a page import, sharing exactly
    /// the version that supplied the graph without retaining source history.
    pub(crate) fn import_source(&self, version: Arc<Version>, operation: SharedBudget) -> Self {
        Self::with_history(
            Document::from_version(version, Some(0)),
            self.limits.clone(),
            false,
            crate::history::State::import_view(operation),
        )
    }

    /// Returns the faces that render the CJK fonts that the document does
    /// not embed.
    pub(crate) fn fallback(&self) -> Fallback {
        self.fallback
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    /// Replaces the faces that render the CJK fonts that the document does
    /// not embed, unless the document has already been closed.
    pub(crate) fn set_fallback(&self, fallback: Fallback) {
        let document = self.document.read().unwrap_or_else(PoisonError::into_inner);
        if document.is_none() {
            return;
        }
        *self
            .fallback
            .write()
            .unwrap_or_else(PoisonError::into_inner) = fallback;
    }

    /// Returns the store's document, or refuses a closed one.
    pub(crate) fn document(&self) -> PyResult<Arc<Document>> {
        self.document
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
            .ok_or_else(|| DocumentClosedError::new_err("document closed"))
    }

    /// Runs `work` with a shared borrow of the current store under the
    /// document read lock so concurrent `replace()` and `close()` cannot
    /// swap or clear the store's history metadata mid-call.
    pub(crate) fn with_store<T>(&self, work: impl FnOnce(&Document) -> T) -> PyResult<T> {
        let guard = self.document.read().unwrap_or_else(PoisonError::into_inner);
        let Some(store) = guard.as_deref() else {
            return Err(DocumentClosedError::new_err("document closed"));
        };
        Ok(work(store))
    }

    pub(crate) fn current(&self) -> PyResult<Arc<Version>> {
        Ok(self.document()?.current())
    }

    fn clear_caches(&self) {
        self.pages.clear();
        self.draft_pages.clear();
        self.results.clear();
        self.draft_results.clear();
        self.destinations.clear();
        self.draft_destinations.clear();
        self.outline.clear();
        self.draft_outline.clear();
        self.outline_counts.clear();
        self.form.clear();
        self.draft_form.clear();
        self.fill_index.clear();
        self.draft_fill_index.clear();
        self.bridge.clear();
        self.preflight.clear();
        self.draft_preflight.clear();
        self.references.clear();
    }

    /// Replaces the store's document, as unlocking or signing does, and
    /// forgets what was derived from the old one. Refuses if the document
    /// has already been closed.
    pub(crate) fn replace(&self, document: Document) -> PyResult<()> {
        let document = with_settlers(document);
        {
            let mut guard = self
                .document
                .write()
                .unwrap_or_else(PoisonError::into_inner);
            if guard.is_none() {
                return Err(DocumentClosedError::new_err("document closed"));
            }
            *guard = Some(Arc::new(document));
            self.history.clear_steps();
        }
        self.clear_caches();
        self.history.clear_open();
        Ok(())
    }

    /// Returns the budget of what this thread runs now: its innermost
    /// canvas operation, so that the canvas's reads and writes charge one
    /// operation, or a new operation from the document's limits.
    pub(crate) fn budget(&self) -> SharedBudget {
        self.history
            .operation()
            .unwrap_or_else(|| self.fresh_budget().share())
    }

    /// Returns the budget of one call whose `max_*` options set `limits`.
    ///
    /// Inside a canvas operation the call charges the operation, and an
    /// option can only lower its limits; `None` keeps the operation's.
    /// Otherwise each option replaces the document's limit, and `None`
    /// removes it.
    pub(crate) fn call_budget(
        &self,
        limits: &[(LimitCode, Option<u64>)],
    ) -> PyResult<SharedBudget> {
        let Some(mut operation) = self.history.operation() else {
            let budget = limits
                .iter()
                .fold(self.fresh_budget(), |budget, &(code, limit)| {
                    budget.with_limit(code, limit)
                });
            return Ok(budget.share());
        };
        for &(code, limit) in limits {
            if let Some(limit) = limit {
                operation = operation
                    .tightened(code, limit)
                    .map_err(|error| limit_error(&error))?;
            }
        }
        Ok(operation)
    }

    /// Returns a new operation from the document's limits, apart from any
    /// canvas operation.
    pub(crate) fn fresh_budget(&self) -> Budget {
        Budget::new(&self.limits)
    }
}

#[pymethods]
impl _Document {
    /// Private regression hook for the pymethods boundary.
    fn _test_panic(&self) -> PyResult<()> {
        crate::errors::boundary(|| panic!("pyropdf injected method panic"))
    }

    /// Returns an empty document: a catalog and a page tree without pages.
    #[staticmethod]
    #[expect(
        clippy::disallowed_methods,
        reason = "the initial trailer contains the two fixed keys Root and Size"
    )]
    #[pyo3(signature = (limits, version = "1.7"))]
    fn empty(limits: &Bound<'_, PyAny>, version: &str) -> PyResult<Self> {
        crate::errors::boundary(|| {
            let mut base = MemoryBase::new(
                [
                    (Name::from("Root"), Value::from(Reference::new(1, 0))),
                    (Name::from("Size"), Value::from(3)),
                ]
                .into_iter()
                .collect::<Dict>(),
            )
            .with_version(version)
            .ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("version must be 1.0 through 1.7, or 2.0")
            })?;
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
        })?
    }

    /// Opens the file at `path`, unlocking it with `password`.
    #[staticmethod]
    #[pyo3(signature = (path, limits, password=None, recipient=None))]
    fn open_path(
        py: Python<'_>,
        path: PathBuf,
        limits: &Bound<'_, PyAny>,
        password: Option<Vec<u8>>,
        recipient: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        crate::errors::boundary(|| {
            let (limits, complete) = (document_limits(limits)?, complete_checks(limits)?);
            py.detach(|| {
                let budget = Budget::new(&limits);
                let bytes = source::file(&path, &budget)?;
                open(
                    bytes,
                    password.as_deref(),
                    recipient.as_ref(),
                    limits.clone(),
                    complete,
                    &budget,
                )
            })
        })?
    }

    /// Opens the bytes of a buffer: `bytes` without a copy, other buffers
    /// with one.
    #[staticmethod]
    #[pyo3(signature = (data, limits, password=None, recipient=None))]
    fn open_buffer(
        py: Python<'_>,
        data: &Bound<'_, PyAny>,
        limits: &Bound<'_, PyAny>,
        password: Option<Vec<u8>>,
        recipient: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        crate::errors::boundary(|| {
            let (limits, complete) = (document_limits(limits)?, complete_checks(limits)?);
            let budget = Budget::new(&limits);
            let bytes = source::buffer(data, &budget)?;
            py.detach(|| {
                open(
                    bytes,
                    password.as_deref(),
                    recipient.as_ref(),
                    limits.clone(),
                    complete,
                    &budget,
                )
            })
        })?
    }

    /// Returns whether the opened file is linearized.
    fn is_linearized(&self) -> PyResult<bool> {
        crate::errors::boundary(|| {
            let version = self.current()?;
            let budget = Budget::new(&self.limits);
            save::is_linearized(&version, &budget)
                .map_err(|error| crate::errors::store_error(&error))
        })?
    }

    /// Returns whether opening recovered damaged cross-reference data.
    fn is_repaired(&self) -> PyResult<bool> {
        crate::errors::boundary(|| {
            Ok(self
                .current()?
                .base()
                .file()
                .is_some_and(|index| index.is_repaired()))
        })?
    }

    /// Returns an isolated view over the version that reads currently see,
    /// with zero retained history, so pre-save cleaning can modify and save
    /// it without touching this document or its history.
    fn save_view(&self) -> PyResult<Self> {
        crate::errors::boundary(|| {
            let version = self.reading()?;
            Ok(Self::new(
                Document::from_version(version, Some(0)),
                self.limits.clone(),
            ))
        })?
    }

    /// Writes the document to the file at `path`, created or truncated; the
    /// caller replaces the target with it.
    #[pyo3(signature = (path, incremental, encryption=None, prune=false, compress=None, decompress=false, renew_id=false, cross_reference_stream=None, object_streams=false, renumber=false, dedupe=false, linearize=false, hybrid_reference=false, ascii=false, pretty=false, stream_filter=None, stream_filters=None, filter_level=6))]
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
        linearize: bool,
        hybrid_reference: bool,
        ascii: bool,
        pretty: bool,
        stream_filter: Option<String>,
        stream_filters: Option<Vec<(u32, String)>>,
        filter_level: u8,
    ) -> PyResult<()> {
        crate::errors::boundary(|| {
            let budget = Budget::new(&self.limits).with_limit(LimitCode::PdfOutputSize, None);
            let options = save_options(
                prune,
                compress,
                decompress,
                renew_id,
                cross_reference_stream,
                object_streams,
                renumber,
                dedupe,
                linearize,
                hybrid_reference,
                ascii,
                pretty,
                stream_filter.as_deref(),
                stream_filters.as_deref(),
                filter_level,
                &budget,
            )?;
            let (version, encryption) =
                crate::security::prepare(py, self, incremental, encryption)?;
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
        })?
    }

    /// Returns the document as PDF bytes, at most `max_size` of them.
    #[pyo3(signature = (incremental, max_size, encryption=None, prune=false, compress=None, decompress=false, renew_id=false, cross_reference_stream=None, object_streams=false, renumber=false, dedupe=false, linearize=false, hybrid_reference=false, ascii=false, pretty=false, stream_filter=None, stream_filters=None, filter_level=6))]
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
        linearize: bool,
        hybrid_reference: bool,
        ascii: bool,
        pretty: bool,
        stream_filter: Option<String>,
        stream_filters: Option<Vec<(u32, String)>>,
        filter_level: u8,
    ) -> PyResult<Bound<'py, PyBytes>> {
        crate::errors::boundary(|| {
            let budget = Budget::new(&self.limits).with_limit(LimitCode::PdfOutputSize, max_size);
            let options = save_options(
                prune,
                compress,
                decompress,
                renew_id,
                cross_reference_stream,
                object_streams,
                renumber,
                dedupe,
                linearize,
                hybrid_reference,
                ascii,
                pretty,
                stream_filter.as_deref(),
                stream_filters.as_deref(),
                filter_level,
                &budget,
            )?;
            let (version, encryption) =
                crate::security::prepare(py, self, incremental, encryption)?;
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
        })?
    }

    /// Returns each object of the current version that the cross-reference
    /// data lists, as its number, generation, and canonical syntax. Tests
    /// compare documents in value with it.
    #[expect(
        clippy::disallowed_methods,
        reason = "one number and result per budgeted index entry; each canonical syntax buffer also charges the budget"
    )]
    fn _objects<'py>(&self, py: Python<'py>) -> PyResult<Vec<(u32, u16, Bound<'py, PyBytes>)>> {
        crate::errors::boundary(|| {
            let version = self.reading()?;
            let budget = Budget::new(&self.limits);
            let mut objects = Vec::new();
            let numbers: Vec<u32> = match version.base().file() {
                Some(index) => index.entries().map(|(number, _)| number).collect(),
                None => Vec::new(),
            };
            for number in numbers {
                let Some(object) = version.get(number, &budget).map_err(|e| store_error(&e))?
                else {
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
        })?
    }

    /// Releases the document. Later operations raise `DocumentClosedError`.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        crate::errors::boundary(|| {
            let _write = if self.in_transaction() {
                None
            } else {
                Some(py.detach(|| self.history.write_lock()))
            };
            {
                let mut guard = self
                    .document
                    .write()
                    .unwrap_or_else(PoisonError::into_inner);
                guard.take();
                *self
                    .fallback
                    .write()
                    .unwrap_or_else(PoisonError::into_inner) = Fallback::default();
                self.history.clear_steps();
            }
            self.clear_caches();
            self.history.clear_open();
        })
    }
}

/// Reads the index of `bytes`, and with `complete` every object, within
/// `budget`.
fn open(
    bytes: Bytes,
    password: Option<&[u8]>,
    recipient: Option<&Py<PyAny>>,
    limits: DocumentLimits,
    complete: bool,
    budget: &Budget,
) -> PyResult<_Document> {
    let base = credential_base(bytes, password, recipient, budget)?;
    // A locked document's objects cannot be read until it is unlocked.
    let locked = base.index().is_encrypted() && base.security().is_none();
    let document = Document::new(Arc::new(base), Some(DEFAULT_HISTORY_LIMIT));
    if complete && !locked {
        check_every_object(&document.current(), budget).map_err(|e| store_error(&e))?;
    }
    Ok(_Document::new_checked(document, limits, complete))
}

fn credential_base(
    bytes: Bytes,
    password: Option<&[u8]>,
    recipient: Option<&Py<PyAny>>,
    budget: &Budget,
) -> PyResult<FileBase> {
    match recipient {
        Some(decrypt) => crate::security::recipient_base(bytes, decrypt, budget),
        None => open_base(bytes, password, budget).map_err(|e| store_error(&e)),
    }
}

/// Opens `bytes` with `password`. When `password` is rejected, falls back to
/// the empty user password so documents with an empty user password still open.
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
pub(crate) fn check_every_object(version: &Version, budget: &Budget) -> Result<(), Error> {
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

fn parse_stream_filter(name: &str, level: u8) -> PyResult<save::StreamFilter> {
    match name {
        "keep" => Ok(save::StreamFilter::Keep),
        "none" => Ok(save::StreamFilter::None),
        "FlateDecode" => Ok(save::StreamFilter::Flate(level)),
        "ASCII85Decode" => Ok(save::StreamFilter::Ascii85),
        "ASCIIHexDecode" => Ok(save::StreamFilter::AsciiHex),
        "RunLengthDecode" => Ok(save::StreamFilter::RunLength),
        _ => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "unsupported stream_filter: {name:?}"
        ))),
    }
}

/// Returns the options of a save from the validated Python arguments.
#[expect(clippy::too_many_arguments, reason = "the options of Document.save")]
fn save_options(
    prune: bool,
    compress: Option<u8>,
    decompress: bool,
    renew_id: bool,
    cross_reference_stream: Option<bool>,
    object_streams: bool,
    renumber: bool,
    dedupe: bool,
    linearize: bool,
    hybrid_reference: bool,
    ascii: bool,
    pretty: bool,
    stream_filter: Option<&str>,
    stream_filters: Option<&[(u32, String)]>,
    filter_level: u8,
    budget: &Budget,
) -> PyResult<save::Options> {
    let streams = if let Some(filter) = stream_filter {
        save::Streams::Filter(parse_stream_filter(filter, filter_level)?)
    } else {
        match (compress, decompress) {
            (Some(level), _) => save::Streams::Compress(level),
            (None, true) => save::Streams::Decompress,
            (None, false) => save::Streams::Keep,
        }
    };
    let mut parsed_filters = pyropdf_store::budget::BudgetVec::new(budget);
    if let Some(entries) = stream_filters {
        budget
            .check(LimitCode::ObjectCount, entries.len() as u64)
            .map_err(|e| limit_error(&e))?;
        parsed_filters
            .try_reserve(entries.len())
            .map_err(|e| limit_error(&e))?;
        for (xref, name) in entries {
            budget.poll().map_err(|e| limit_error(&e))?;
            parsed_filters
                .try_push((*xref, parse_stream_filter(name, filter_level)?))
                .map_err(|e| limit_error(&e))?;
        }
    }
    Ok(save::Options {
        prune,
        streams,
        stream_filters: parsed_filters.into_vec(),
        renew_id,
        cross_reference: match (hybrid_reference, cross_reference_stream) {
            (true, _) => save::CrossReference::Hybrid,
            (false, None) => save::CrossReference::Base,
            (false, Some(false)) => save::CrossReference::Table,
            (false, Some(true)) => save::CrossReference::Stream,
        },
        object_streams,
        renumber,
        dedupe,
        linearize,
        ascii,
        pretty,
    })
}

/// Reads the fields of a Python `DocumentLimits`.
pub(crate) fn document_limits(limits: &Bound<'_, PyAny>) -> PyResult<DocumentLimits> {
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
        max_memory_size: field("max_memory_size")?,
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

pub(crate) fn complete_checks(limits: &Bound<'_, PyAny>) -> PyResult<bool> {
    limits.getattr("check_at_open")?.extract()
}

/// Returns the fields of `DocumentLimits.web()` by name, so that Python and
/// Rust share one profile.
#[pyfunction]
pub fn web_limits(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    crate::errors::boundary(|| {
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
            ("max_memory_size", web.max_memory_size),
        ] {
            fields.set_item(name, value)?;
        }
        fields.set_item("max_duration", web.max_duration.map(|d| d.as_secs_f64()))?;
        Ok(fields)
    })?
}

/// What `probe` reads: the page count, the header's version, whether the
/// document is encrypted, whether its index was repaired, and the text
/// entries of its Info dictionary.
type Probe<'py> = (u64, String, bool, bool, Bound<'py, PyDict>);

/// Reads the index of the file at `path` or of the buffer `data`, and the
/// few objects that a probe needs.
#[pyfunction]
#[pyo3(signature = (path, data, max_file_size, password=None, recipient=None))]
pub fn probe<'py>(
    py: Python<'py>,
    path: Option<PathBuf>,
    data: Option<&Bound<'py, PyAny>>,
    max_file_size: Option<u64>,
    password: Option<Vec<u8>>,
    recipient: Option<Py<PyAny>>,
) -> PyResult<Probe<'py>> {
    crate::errors::boundary(|| {
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
            py.detach(|| read_probe(bytes, password.as_deref(), recipient.as_ref(), &budget))?;
        let dict = PyDict::new(py);
        for (key, value) in metadata {
            dict.set_item(key, value)?;
        }
        Ok((count, version, encrypted, repaired, dict))
    })?
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

#[expect(
    clippy::disallowed_methods,
    reason = "PROBE_KEYS is fixed and returned text is capped at MAX_PROBE_METADATA_SIZE"
)]
fn read_probe(
    bytes: Bytes,
    password: Option<&[u8]>,
    recipient: Option<&Py<PyAny>>,
    budget: &Budget,
) -> PyResult<ProbeData> {
    use pyropdf_store::version::Base as _;
    let store = |e: Error| store_error(&e);
    let base = credential_base(bytes, password, recipient, budget)?;
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
/// `"1.4"`, or `None` when a source header names no version.
#[pyfunction]
#[pyo3(name = "header_version")]
pub fn document_header_version(document: &_Document) -> PyResult<Option<String>> {
    crate::errors::boundary(|| {
        let version = document.reading()?;
        Ok(version.base().header_bytes().and_then(header_version))
    })?
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
/// bits of its coordinates. Content with text runs holds the fallback faces
/// that describe them, and other content holds none.
#[derive(Clone, PartialEq, Eq, Hash)]
pub(crate) enum PageKind {
    Text(crate::text::TextKey),
    Layout(crate::text::TextKey, bool),
    Content {
        kinds: [bool; 3],
        annotations: bool,
        clip: Option<[u64; 4]>,
        fallback: Fallback,
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
pub fn page_result_counts(document: &_Document) -> PyResult<(u64, u64)> {
    crate::errors::boundary(|| {
        let (results, drafts) = (document.results.stats(), document.draft_results.stats());
        (
            results.hits + drafts.hits,
            results.computations + drafts.computations,
        )
    })
}

/// Returns how many pre-flight guard checks the document's F4 memos reused
/// and how many they computed, for tests (#1241).
#[pyfunction]
pub fn preflight_counts(document: &_Document) -> PyResult<(u64, u64)> {
    crate::errors::boundary(|| {
        let (committed, drafts) = (document.preflight.stats(), document.draft_preflight.stats());
        (
            committed.hits + drafts.hits,
            committed.computations + drafts.computations,
        )
    })
}

/// A failed computation of a page's result.
pub(crate) struct Failed(pub(crate) PyErr);

impl From<Error> for Failed {
    fn from(error: Error) -> Self {
        Self(store_error(&error))
    }
}

impl _Document {
    /// Returns the active pre-flight memo (committed or draft transaction).
    pub(crate) fn preflight_cache(&self) -> &pyropdf_engine::bridge::PreflightCache {
        if self.in_transaction() {
            &self.draft_preflight
        } else {
            &self.preflight
        }
    }

    /// Runs the F4-memoized pre-flight nesting guard on `page` before handing
    /// it to `hayro` (no-op when `max_object_depth` is `None`).
    pub(crate) fn preflight_page(
        &self,
        version: &Arc<Version>,
        page: &Page,
        budget: &Budget,
    ) -> PyResult<()> {
        self.preflight_cache()
            .validate_page(version, page, budget)
            .map_err(|error| store_error(&error))
    }

    /// Runs the F4-memoized pre-flight nesting guard on `indexes` before
    /// handing pages to `hayro` (no-op when `max_object_depth` is `None`).
    pub(crate) fn preflight_pages(
        &self,
        version: &Arc<Version>,
        pages: &PageIndex,
        indexes: &[usize],
        budget: &Budget,
    ) -> PyResult<()> {
        self.preflight_cache()
            .validate_pages(version, pages, indexes, budget)
            .map_err(|error| store_error(&error))
    }

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
