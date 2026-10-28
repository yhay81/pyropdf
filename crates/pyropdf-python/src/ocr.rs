//! The core of `OcrEngine`: a PP-OCR model set of `pyropdf_engine::ocr`.

use std::path::PathBuf;

use pyo3::exceptions::{PyFileNotFoundError, PyValueError};
use pyo3::prelude::*;
use pyropdf_engine::ocr::{Engine, Error, LayerWord, OcrTuple, Options, layer};
use pyropdf_store::budget::{Budget, BudgetVec, DocumentLimits, LimitCode};
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::{
    Array, Bytes, Dict, Name, PdfString, Reference, Stream, StringForm, Value,
};
use pyropdf_store::version::Transaction;

use crate::document::_Document;
use crate::errors::{OcrError, limit_error, store_error};
use crate::objects::set_data;
use crate::pages::{display_box, resolve, rotation};
use crate::pixmap::Pixmap;

/// The resource name of a layer's font, followed by a number when the page
/// already names a font so.
const LAYER_FONT: &str = "PyroOCR";
/// The `/BaseFont` of a layer's font, which no font program backs.
const LAYER_BASE_FONT: &str = "PyroOCR-Gothic";

/// Maps an OCR error to `FileNotFoundError`, `LimitError`, or `OcrError`.
fn ocr_error(error: Error) -> PyErr {
    match error {
        Error::NotFound(path) => PyFileNotFoundError::new_err((
            2,
            "No such file or directory",
            path.display().to_string(),
        )),
        Error::Limit(refusal) => limit_error(&refusal),
        Error::Failed(message) => OcrError::new_err(message),
    }
}

/// A loaded OCR model set. Python owns rendering and the conversion of boxes
/// to page coordinates.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct _OcrEngine {
    engine: Engine,
}

#[pymethods]
impl _OcrEngine {
    /// Loads the model set at the paths with the GIL released.
    /// `max_model_size` bounds the bytes of the three files with
    /// `ocr_model_size`; `None` lifts it.
    #[new]
    fn new(
        py: Python<'_>,
        detector_path: PathBuf,
        recognizer_path: PathBuf,
        dictionary_path: PathBuf,
        threads: usize,
        max_model_size: Option<u64>,
    ) -> PyResult<Self> {
        if max_model_size == Some(0) {
            return Err(PyValueError::new_err(
                "max_model_size must be a positive integer or None",
            ));
        }
        let budget = Budget::new(&DocumentLimits::default())
            .with_limit(LimitCode::OcrModelSize, max_model_size);
        py.detach(|| {
            Engine::load(
                &detector_path,
                &recognizer_path,
                &dictionary_path,
                threads,
                &budget,
            )
        })
        .map(|engine| Self { engine })
        .map_err(ocr_error)
    }

    /// Recognizes the words of `pixmap` with the GIL released, as boxes in
    /// its pixel coordinates with their text and confidence.
    #[pyo3(signature = (pixmap, *, tile_size=1408, overlap=192, min_confidence=0.5, rotation=0, document=None))]
    #[allow(clippy::too_many_arguments)]
    fn recognize_pixmap(
        &self,
        py: Python<'_>,
        pixmap: PyRef<'_, Pixmap>,
        tile_size: usize,
        overlap: usize,
        min_confidence: f32,
        rotation: usize,
        document: Option<PyRef<'_, _Document>>,
    ) -> PyResult<Vec<OcrTuple>> {
        let budget = document
            .as_ref()
            .map_or_else(Budget::default, |document| document.budget());
        drop(document);
        let (pixels, layout) = pixmap.ocr_raster();
        drop(pixmap);
        let options = Options {
            tile_size,
            overlap,
            min_confidence,
            rotation,
        };
        py.detach(|| self.engine.recognize(&pixels, layout, &options, &budget))
            .map_err(ocr_error)
    }

    fn __repr__(&self) -> &'static str {
        "<pyropdf.OcrEngine native models>"
    }
}

/// Adds the invisible text layer of `words`, boxes in display space, to the
/// page whose object is `number` and `generation`, in one transaction.
/// `rotation` is the clockwise correction that read the words. Returns
/// `false` for a stale page.
///
/// The layer shows its words in a non-embedded Identity-H font with a
/// ToUnicode CMap, which the page's own `/Resources` names; inherited
/// resources are copied into the page first. The page's earlier content is
/// wrapped in `q` and `Q`, so its graphics state does not reach the layer.
#[pyfunction]
pub fn insert_ocr_layer(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    words: Vec<LayerWord>,
    rotation_correction: u16,
) -> PyResult<bool> {
    document.edit(py, |transaction, budget| {
        // Read the page after acquiring the writer lock. A concurrent edit
        // may have changed its content or resources while this call waited.
        let version = transaction.snapshot();
        let index = PageIndex::memoized(&document.draft_pages, &version, budget)
            .map_err(|error| store_error(&error))?;
        let Some(at) = index.index_of(Reference::new(number, generation)) else {
            return Ok(false);
        };
        let page = &index.pages()[at];
        let crop = display_box(page);
        let page_rotation = rotation(page.rotate());
        let mut resources = match resolve(&version, page.resources(), budget)? {
            Some(Value::Dict(resources)) => resources,
            _ => Dict::new(),
        };
        let mut fonts = match resolve(&version, resources.get("Font"), budget)? {
            Some(Value::Dict(fonts)) => fonts,
            _ => Dict::new(),
        };
        let mut name = LAYER_FONT.to_owned();
        for suffix in 0..=fonts.len() {
            budget.poll().map_err(|error| limit_error(&error))?;
            name = if suffix == 0 {
                LAYER_FONT.to_owned()
            } else {
                format!("{LAYER_FONT}{suffix}")
            };
            if fonts.get(&name).is_none() {
                break;
            }
        }
        let layer = layer(
            &words,
            crop,
            page_rotation,
            rotation_correction,
            &name,
            budget,
        )
        .map_err(ocr_error)?;
        let mut page_dict = page.dict().clone();
        let mut contents = BudgetVec::new(budget);
        match resolve(&version, page_dict.get("Contents"), budget)? {
            None | Some(Value::Null) => {}
            Some(Value::Array(array)) => contents
                .try_extend(array.iter().cloned())
                .map_err(|error| limit_error(&error))?,
            Some(_) => {
                if let Some(value) = page_dict.get("Contents") {
                    contents
                        .try_push(value.clone())
                        .map_err(|error| limit_error(&error))?;
                }
            }
        }
        let font = add_font(transaction, layer.to_unicode, budget)?;
        fonts.insert(Name::from(name.as_str()), Value::Reference(font));
        resources.insert(Name::from("Font"), Value::Dict(fonts));
        let mut array = BudgetVec::new(budget);
        if !contents.is_empty() {
            array
                .try_push(add_stream(transaction, b"q\n", budget)?)
                .map_err(|error| limit_error(&error))?;
            array
                .try_extend(contents.iter().cloned())
                .map_err(|error| limit_error(&error))?;
            array
                .try_push(add_stream(transaction, b"\nQ\n", budget)?)
                .map_err(|error| limit_error(&error))?;
        }
        array
            .try_push(add_stream(transaction, &layer.content, budget)?)
            .map_err(|error| limit_error(&error))?;
        page_dict.insert(
            Name::from("Contents"),
            Value::Array(Array::from(array.into_vec())),
        );
        page_dict.insert(Name::from("Resources"), Value::Dict(resources));
        transaction
            .write(number, &Value::Dict(page_dict), budget)
            .map_err(|error| store_error(&error))?;
        Ok(true)
    })
}

/// Writes `value` as a new indirect object and returns its reference.
fn add(transaction: &mut Transaction, value: Value, budget: &Budget) -> PyResult<Reference> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let reference = transaction.allocate().map_err(store)?;
    transaction
        .write(reference.number(), &value, budget)
        .map_err(store)
}

/// Writes an uncompressed stream of `data` and returns a reference to it.
fn add_stream(transaction: &mut Transaction, data: &[u8], budget: &Budget) -> PyResult<Value> {
    let mut stream = Stream::new(Dict::new(), Bytes::default());
    set_data(&mut stream, data, false)?;
    add(transaction, Value::Stream(Box::new(stream)), budget).map(Value::Reference)
}

/// Builds a dictionary of `entries`.
fn dict<const N: usize>(entries: [(&str, Value); N]) -> Value {
    let mut dict = Dict::new();
    for (key, value) in entries {
        dict.insert(Name::from(key), value);
    }
    Value::Dict(dict)
}

fn name(name: &str) -> Value {
    Value::Name(Name::from(name))
}

fn integers<const N: usize>(values: [i64; N]) -> Value {
    Value::Array(Array::from(values.map(Value::Integer).to_vec()))
}

/// Writes the Type0 font of a layer, with its descendant CIDFont, font
/// descriptor, and ToUnicode CMap, and returns a reference to it.
///
/// No font program is embedded. Every CID is one em wide (`/DW 1000`); the
/// text matrix fits each word to its box.
fn add_font(
    transaction: &mut Transaction,
    to_unicode: Vec<u8>,
    budget: &Budget,
) -> PyResult<Reference> {
    let Value::Reference(to_unicode) = add_stream(transaction, &to_unicode, budget)? else {
        unreachable!("add_stream returns a reference");
    };
    let descriptor = dict([
        ("Type", name("FontDescriptor")),
        ("FontName", name(LAYER_BASE_FONT)),
        ("Flags", Value::Integer(4)),
        ("FontBBox", integers([0, -200, 1000, 800])),
        ("ItalicAngle", Value::Integer(0)),
        ("Ascent", Value::Integer(800)),
        ("Descent", Value::Integer(-200)),
        ("CapHeight", Value::Integer(800)),
        ("StemV", Value::Integer(80)),
    ]);
    let descriptor = add(transaction, descriptor, budget)?;
    let string = |text: &str| Value::String(PdfString::new(text.as_bytes(), StringForm::Literal));
    let cid_font = dict([
        ("Type", name("Font")),
        ("Subtype", name("CIDFontType2")),
        ("BaseFont", name(LAYER_BASE_FONT)),
        (
            "CIDSystemInfo",
            dict([
                ("Registry", string("Adobe")),
                ("Ordering", string("Identity")),
                ("Supplement", Value::Integer(0)),
            ]),
        ),
        ("FontDescriptor", Value::Reference(descriptor)),
        ("DW", Value::Integer(1000)),
        ("CIDToGIDMap", name("Identity")),
    ]);
    let cid_font = add(transaction, cid_font, budget)?;
    let font = dict([
        ("Type", name("Font")),
        ("Subtype", name("Type0")),
        ("BaseFont", name(LAYER_BASE_FONT)),
        ("Encoding", name("Identity-H")),
        (
            "DescendantFonts",
            Value::Array(Array::from(vec![Value::Reference(cid_font)])),
        ),
        ("ToUnicode", Value::Reference(to_unicode)),
    ]);
    add(transaction, font, budget)
}
