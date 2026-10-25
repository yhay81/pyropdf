//! The store's properties over the whole corpus (DESIGN.md section 12), which
//! every store change must keep:
//!
//! - without edits, an incremental save reproduces the file byte for byte,
//!   and a full save reproduces every value, as the store and hayro-syntax
//!   each read both files;
//! - after random transactions, both saves keep the bytes of every untouched
//!   object and read every written one back, and an incremental save keeps
//!   the original as its prefix.

#![allow(
    clippy::disallowed_methods,
    reason = "tests grow standard collections freely; F6 binds the store's own code"
)]

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::fs;
use std::path::Path;
use std::sync::Arc;

use hayro_syntax::object::{MaybeRef, Object, ObjectIdentifier};
use proptest::prelude::*;
use proptest::test_runner::{Config, TestRunner};
use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::save::{self, FileBase};
use pyropdf_store::value::{Array, Bytes, Dict, Name, PdfString, Real, Reference, Value};
use pyropdf_store::version::{Document, Version};
use pyropdf_store::write;

/// The corpus files, by name.
fn corpus() -> Vec<(String, Vec<u8>)> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
    let mut files: Vec<_> = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
        .collect();
    files.sort();
    assert_eq!(files.len(), 14, "the corpus has 14 files");
    files
        .into_iter()
        .map(|path| {
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            (name, fs::read(&path).unwrap())
        })
        .collect()
}

fn open(bytes: &[u8]) -> Document {
    let budget = Budget::default();
    let base = FileBase::open(Bytes::new(bytes.to_vec()), &budget).unwrap();
    Document::new(Arc::new(base), None)
}

fn full(version: &Version) -> Vec<u8> {
    let mut out = Vec::new();
    save::full(version, &mut out, &Budget::default()).unwrap();
    out
}

fn incremental(version: &Version) -> Vec<u8> {
    let mut out = Vec::new();
    save::incremental(version, &mut out, &Budget::default()).unwrap();
    out
}

/// Returns the bytes that the store writes for `value`, keeping its lexemes:
/// equal bytes mean that the object kept its bytes.
fn bytes_of(value: &Value) -> Vec<u8> {
    let budget = Budget::default();
    let mut out = BudgetVec::new(&budget);
    write::value(&mut out, value).unwrap();
    out.into_vec()
}

/// Returns every object that the index of `version`'s base lists, by
/// number, with its generation and its bytes as the store writes them.
fn objects(version: &Version) -> BTreeMap<u32, (u16, Vec<u8>)> {
    let budget = Budget::default();
    let numbers: Vec<u32> = version
        .base()
        .file()
        .unwrap()
        .entries()
        .map(|(number, _)| number)
        .collect();
    numbers
        .into_iter()
        .filter_map(|number| {
            let object = version.get(number, &budget).unwrap()?;
            Some((number, (object.generation(), bytes_of(object.value()))))
        })
        .collect()
}

/// Writes hayro-syntax's reading of `object` canonically: references as
/// references, and a stream's dictionary and raw data.
fn canonical(object: &Object<'_>, out: &mut String, depth: usize) {
    assert!(depth < 128, "hayro-syntax reads 64 levels at most");
    match object {
        Object::Null(_) => out.push_str("null"),
        Object::Boolean(b) => write!(out, "{b}").unwrap(),
        Object::Number(n) => write!(out, "{:?}", n.as_f64()).unwrap(),
        Object::String(s) => write!(out, "<{}>", hex(s.as_bytes())).unwrap(),
        Object::Name(n) => write!(out, "/{}", hex(n.as_ref())).unwrap(),
        Object::Array(array) => {
            out.push('[');
            for item in array.raw_iter() {
                maybe(&item, out, depth + 1);
                out.push(' ');
            }
            out.push(']');
        }
        Object::Dict(dict) => dictionary(dict, out, depth),
        Object::Stream(stream) => {
            dictionary(stream.dict(), out, depth);
            write!(out, "stream<{}>", hex(&stream.raw_data())).unwrap();
        }
    }
}

fn dictionary(dict: &hayro_syntax::object::Dict<'_>, out: &mut String, depth: usize) {
    let mut entries: Vec<_> = dict
        .entries()
        .filter(|(key, _)| key.as_ref() != b"Length")
        .map(|(key, value)| {
            let mut text = String::new();
            maybe(&value, &mut text, depth + 1);
            (hex(key.as_ref()), text)
        })
        .collect();
    entries.sort();
    out.push_str("<<");
    for (key, value) in entries {
        write!(out, "/{key} {value} ").unwrap();
    }
    out.push_str(">>");
}

fn maybe(value: &MaybeRef<Object<'_>>, out: &mut String, depth: usize) {
    match value {
        MaybeRef::Ref(r) => write!(out, "{} {} R", r.obj_number, r.gen_number).unwrap(),
        MaybeRef::NotRef(object) => canonical(object, out, depth),
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().fold(String::new(), |mut out, b| {
        write!(out, "{b:02x}").unwrap();
        out
    })
}

/// Returns hayro-syntax's reading of each of `numbers` in `bytes`.
fn hayro_objects(bytes: &[u8], numbers: &BTreeMap<u32, (u16, Vec<u8>)>) -> BTreeMap<u32, String> {
    let pdf = hayro_syntax::Pdf::new(bytes.to_vec()).unwrap();
    numbers
        .iter()
        .filter_map(|(&number, &(generation, _))| {
            let id = ObjectIdentifier::new(number as i32, i32::from(generation));
            let object = pdf.xref().get::<Object<'_>>(id)?;
            let mut text = String::new();
            canonical(&object, &mut text, 0);
            Some((number, text))
        })
        .collect()
}

#[test]
fn saves_without_edits_reproduce_the_corpus() {
    for (name, bytes) in corpus() {
        let document = open(&bytes);
        let version = document.current();
        let repaired = version.base().file().unwrap().is_repaired();
        if !repaired {
            assert_eq!(incremental(&version), bytes, "{name}: incremental");
        }
        let saved = full(&version);
        let reopened = open(&saved);
        let before = objects(&version);
        let after = objects(&reopened.current());
        for (number, object) in &before {
            assert_eq!(
                after.get(number),
                Some(object),
                "{name}: object {number} in the store"
            );
        }
        let theirs_before = hayro_objects(&bytes, &before);
        let theirs_after = hayro_objects(&saved, &before);
        assert!(!theirs_before.is_empty(), "{name}");
        for (number, object) in &theirs_before {
            assert_eq!(
                theirs_after.get(number),
                Some(object),
                "{name}: object {number} in hayro-syntax"
            );
        }
    }
}

/// One write of a random edit sequence: an object number, counted among the
/// file's objects or past them, and the value written there.
#[derive(Clone, Debug)]
enum Edit {
    Write(usize, Value),
    Free(usize),
}

fn value() -> impl Strategy<Value = Value> {
    let leaf = prop_oneof![
        Just(Value::Null),
        any::<bool>().prop_map(Value::Boolean),
        any::<i32>().prop_map(|n| Value::Integer(n.into())),
        // Reals that f32 cannot hold, so that a writer that narrows them fails.
        (-1.0e6..1.0e6f64).prop_map(|r| Value::Real(Real::new((r * 1e6).round() / 1e6 + 1e-7))),
        proptest::collection::vec(any::<u8>(), 0..12)
            .prop_map(|b| Value::String(PdfString::literal(b))),
        "[A-Za-z#]{1,8}".prop_map(|s| Value::Name(Name::from(s.as_str()))),
        (1u32..50, 0u16..3).prop_map(|(n, g)| Value::Reference(Reference::new(n, g))),
    ];
    leaf.prop_recursive(3, 16, 4, |inner| {
        prop_oneof![
            proptest::collection::vec(inner.clone(), 0..4)
                .prop_map(|v| Value::Array(Array::from(v))),
            proptest::collection::vec(("[A-Z][a-z]{0,5}", inner), 0..4).prop_map(|entries| {
                Value::Dict(
                    entries
                        .into_iter()
                        .map(|(k, v)| (Name::from(k.as_str()), v))
                        .collect::<Dict>(),
                )
            }),
        ]
    })
}

fn edit() -> impl Strategy<Value = Edit> {
    prop_oneof![
        4 => (any::<usize>(), value()).prop_map(|(at, v)| Edit::Write(at, v)),
        1 => any::<usize>().prop_map(Edit::Free),
    ]
}

/// Applies `steps`, each a transaction of edits, and returns what each
/// touched number holds afterwards: a value, or `None` when freed.
fn apply(document: &Document, steps: &[Vec<Edit>]) -> BTreeMap<u32, Option<Value>> {
    let budget = Budget::default();
    let numbers: Vec<u32> = document
        .current()
        .base()
        .file()
        .unwrap()
        .entries()
        .map(|(number, _)| number)
        .collect();
    let past = u32::try_from(document.current().next_number()).unwrap();
    let pick = |at: usize| -> u32 {
        // Most edits rewrite an object of the file; some write new numbers.
        let slot = at % (numbers.len() + 4);
        numbers
            .get(slot)
            .copied()
            .unwrap_or_else(|| past + (slot - numbers.len()) as u32)
    };
    let mut touched = BTreeMap::new();
    for step in steps {
        let mut transaction = document.begin();
        for edit in step {
            match edit {
                Edit::Write(at, value) => {
                    let number = pick(*at);
                    if transaction.write(number, value, &budget).is_ok() {
                        touched.insert(number, Some(value.clone()));
                    }
                }
                Edit::Free(at) => {
                    let number = pick(*at);
                    if transaction.free(number, &budget).unwrap_or(false) {
                        touched.insert(number, None);
                    }
                }
            }
        }
        transaction.commit(&budget).unwrap();
    }
    touched
}

#[test]
fn random_edits_keep_untouched_bytes_and_read_back_written_values() {
    let mut runner = TestRunner::new(Config {
        cases: 48,
        failure_persistence: None,
        ..Config::default()
    });
    let steps = proptest::collection::vec(proptest::collection::vec(edit(), 1..6), 1..4);
    for (name, bytes) in corpus() {
        let original = open(&bytes);
        let before = objects(&original.current());
        let repaired = original.current().base().file().unwrap().is_repaired();
        runner
            .run(&steps, |steps| {
                let document = open(&bytes);
                let touched = apply(&document, &steps);
                let version = document.current();
                let mut saves = vec![("full", full(&version))];
                if !repaired {
                    let update = incremental(&version);
                    prop_assert!(
                        update.starts_with(&bytes),
                        "{}: the update keeps the original",
                        name
                    );
                    saves.push(("incremental", update));
                }
                for (mode, saved) in saves {
                    let reopened = open(&saved);
                    let reread = reopened.current();
                    let budget = Budget::default();
                    for (number, object) in &before {
                        if touched.contains_key(number) {
                            continue;
                        }
                        let kept = reread
                            .get(*number, &budget)
                            .unwrap()
                            .map(|o| (o.generation(), bytes_of(o.value())));
                        prop_assert_eq!(
                            kept.as_ref(),
                            Some(object),
                            "{} {}: untouched object {}",
                            name,
                            mode,
                            number
                        );
                    }
                    for (number, written) in &touched {
                        let read = reread
                            .get(*number, &budget)
                            .unwrap()
                            .map(|o| (**o.value()).clone());
                        prop_assert_eq!(
                            &read,
                            written,
                            "{} {}: written object {}",
                            name,
                            mode,
                            number
                        );
                    }
                }
                Ok(())
            })
            .unwrap_or_else(|error| panic!("{name}: {error}"));
    }
}
