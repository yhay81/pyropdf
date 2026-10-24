//! Whatever the writer writes, hayro-syntax reads back as the same value, and
//! an unchanged value is written as its lexeme (DESIGN.md F3 and section
//! 4.2).

use std::fs;
use std::path::Path;

use hayro_syntax::Pdf;
use hayro_syntax::object::{self as syntax, MaybeRef, Object};
use proptest::prelude::*;

use super::*;
use crate::budget::Budget;
use crate::value::{Array, Bytes, Dict, Reference};
use crate::{ErrorKind, LimitExceeded};

/// Writes `value` in canonical syntax with the default budget.
fn canonical_bytes(value: &Value) -> Vec<u8> {
    let budget = Budget::default();
    let mut out = BudgetVec::new(&budget);
    canonical(&mut out, value).unwrap_or_else(|error| panic!("{value:?}: {error}"));
    out.into_vec()
}

/// Writes `value`, keeping its lexemes, with the default budget.
fn lexeme_bytes(value: &Value) -> Vec<u8> {
    let budget = Budget::default();
    let mut out = BudgetVec::new(&budget);
    super::value(&mut out, value).unwrap_or_else(|error| panic!("{value:?}: {error}"));
    out.into_vec()
}

/// Returns the error of writing `value` with `budget`.
fn write_error(value: &Value, budget: &Budget) -> Error {
    let mut out = BudgetVec::new(budget);
    canonical(&mut out, value).unwrap_err()
}

/// Parses `bytes` with the default budget.
fn parse(bytes: Vec<u8>) -> Value {
    Value::parse(&Bytes::from(bytes), &Budget::default()).unwrap_or_else(|error| panic!("{error}"))
}

fn name(name: &str) -> Name {
    Name::from(name)
}

fn real(value: f64) -> Value {
    Value::from(value)
}

fn literal(bytes: &[u8]) -> Value {
    Value::String(PdfString::literal(bytes))
}

fn hexadecimal(bytes: &[u8]) -> Value {
    Value::String(PdfString::hexadecimal(bytes))
}

fn array(items: impl IntoIterator<Item = Value>) -> Value {
    Value::Array(items.into_iter().collect())
}

fn dict<'a>(entries: impl IntoIterator<Item = (&'a str, Value)>) -> Value {
    Value::Dict(
        entries
            .into_iter()
            .map(|(key, value)| (name(key), value))
            .collect(),
    )
}

fn stream(entries: Dict, data: &'static [u8]) -> Value {
    Value::from(Stream::new(entries, Bytes::from(data)))
}

/// Asserts that `value` is written as `text` and reads back as itself.
#[track_caller]
fn assert_writes(value: &Value, text: &[u8]) {
    let written = canonical_bytes(value);
    assert_eq!(
        written.escape_ascii().to_string(),
        text.escape_ascii().to_string()
    );
    assert_eq!(&parse(written), value);
}

#[test]
fn scalars_have_one_canonical_form() {
    let cases: &[(Value, &[u8])] = &[
        (Value::Null, b"null"),
        (Value::Boolean(true), b"true"),
        (Value::Boolean(false), b"false"),
        (Value::Integer(0), b"0"),
        (Value::Integer(-42), b"-42"),
        (Value::Integer(i64::MIN), b"-9223372036854775808"),
        (Value::Integer(i64::MAX), b"9223372036854775807"),
        (Value::Reference(Reference::new(12, 0)), b"12 0 R"),
        (Value::Reference(Reference::new(1, 65_535)), b"1 65535 R"),
    ];
    for (value, text) in cases {
        assert_writes(value, text);
    }
}

#[test]
fn reals_have_the_shortest_decimal_that_reads_back() {
    let cases: &[(f64, &[u8])] = &[
        (35.681_234_5, b"35.6812345"),
        (123_456.789, b"123456.789"),
        (3.0, b"3.0"),
        (-0.0, b"-0.0"),
        (0.0, b"0.0"),
        (0.5, b"0.5"),
        (-0.002, b"-0.002"),
        (0.1 + 0.2, b"0.30000000000000004"),
        (1e-7, b"0.0000001"),
        (1e20, b"100000000000000000000.0"),
        (9_007_199_254_740_993.0, b"9007199254740992.0"),
    ];
    for &(value, text) in cases {
        assert_writes(&real(value), text);
    }
}

#[test]
fn reals_that_hayro_syntax_would_round_twice_are_padded_with_zeros() {
    // hayro-syntax 0.8 reads the shortest decimal of these as an integer
    // divided by a power of ten, which rounds twice and misses by one unit
    // in the last place.
    let cases: &[(f64, &[u8])] = &[
        (519_188.221_071_372_04, b"519188.221071372040000"),
        (-917_330.175_664_149_7, b"-917330.175664149700000"),
        (1e-25, b"0.000000000000000000000000100000000000000000000"),
    ];
    for &(value, text) in cases {
        let shortest = value.to_string();
        let hayro = Number::from_bytes(shortest.as_bytes()).map(|number| number.as_f64());
        assert_ne!(hayro, Some(value), "{shortest}");
        assert_writes(&real(value), text);
    }
    // The smallest subnormal has one significant digit after 323 zeros.
    let tiny = canonical_bytes(&real(f64::from_bits(1)));
    assert_eq!(significant_digits(&tiny), EXACT_DIGITS);
    assert_eq!(parse(tiny), real(f64::from_bits(1)));
}

#[test]
fn reals_keep_their_f64_exactly() {
    // lopdf keeps reals as `f32`, which writes these back as 35.681236 and
    // 123456.79 (yhay81/pylopdf#370).
    for value in [35.681_234_5_f64, 123_456.789, -35.681_234_5, -123_456.789] {
        let source = array([real(value), dict([("X", real(value))])]);
        let Value::Array(read) = parse(canonical_bytes(&source)) else {
            panic!("not an array");
        };
        let Value::Real(item) = &read.as_slice()[0] else {
            panic!("not a real");
        };
        assert_eq!(item.value().to_bits(), value.to_bits());
        assert_ne!(f64::from(value as f32), value);
        assert_eq!(Value::Array(read), source);
    }
}

#[test]
fn names_escape_what_iso_32000_2_requires() {
    let cases: &[(&[u8], &[u8])] = &[
        (b"Type", b"/Type"),
        (b"", b"/"),
        (b"A B", b"/A#20B"),
        (b"#", b"/#23"),
        (b"()<>[]{}/%", b"/#28#29#3C#3E#5B#5D#7B#7D#2F#25"),
        (b"\x00\t\n\x0C\r", b"/#00#09#0A#0C#0D"),
        (b"\x7F\x80\xFF", b"/#7F#80#FF"),
        (b"!~.;*-_", b"/!~.;*-_"),
    ];
    for (bytes, text) in cases {
        assert_writes(&Value::Name(Name::new(*bytes)), text);
    }
}

#[test]
fn strings_escape_what_iso_32000_2_requires() {
    let cases: &[(Value, &[u8])] = &[
        (literal(b"Hello"), b"(Hello)"),
        (literal(b""), b"()"),
        (literal(b"(a) \\"), b"(\\(a\\) \\\\)"),
        (literal(b"\n\r\t\x08\x0C"), b"(\\n\\r\\t\\b\\f)"),
        (
            literal(b"\x00\x1F\x7F\x80\xFE\xFF"),
            b"(\\000\\037\\177\\200\\376\\377)",
        ),
        (literal(b"\r\n\n"), b"(\\r\\n\\n)"),
        (hexadecimal(b""), b"<>"),
        (hexadecimal(b"\x00\xAB\xFF"), b"<00ABFF>"),
    ];
    for (value, text) in cases {
        assert_writes(value, text);
    }
}

#[test]
fn names_and_strings_hold_every_byte_value() {
    let every: Vec<u8> = (0..=u8::MAX).collect();
    for value in [
        Value::Name(Name::new(every.clone())),
        literal(&every),
        hexadecimal(&every),
        dict([("Every", literal(&every))]),
    ] {
        assert_eq!(parse(canonical_bytes(&value)), value);
    }
    let mut keys = Dict::new();
    for byte in 0..=u8::MAX {
        keys.insert(Name::new([byte, byte]), Value::Integer(byte.into()));
    }
    assert_eq!(
        parse(canonical_bytes(&Value::Dict(keys.clone()))),
        Value::Dict(keys)
    );
}

#[test]
fn containers_separate_their_tokens_with_one_space() {
    assert_writes(&array([]), b"[]");
    assert_writes(&dict([]), b"<<>>");
    assert_writes(
        &array([0, 0, 612, 792].map(Value::Integer)),
        b"[0 0 612 792]",
    );
    assert_writes(
        &dict([
            ("Type", Value::Name(name("Page"))),
            ("Parent", Value::Reference(Reference::new(3, 0))),
            ("Kids", array([])),
        ]),
        b"<</Type /Page /Parent 3 0 R /Kids []>>",
    );
    assert_writes(
        &array([
            array([Value::Null]),
            dict([("A", array([Value::Integer(1), literal(b"x")]))]),
            hexadecimal(b"\x01"),
            Value::Name(name("")),
            Value::Name(name("")),
        ]),
        b"[[null] <</A [1 (x)]>> <01> / /]",
    );
}

#[test]
fn a_stream_writes_its_length_and_its_data_after_its_dictionary() {
    let entries = Dict::from_iter([(name("Filter"), Value::Name(name("X")))]);
    assert_writes(
        &stream(entries, b"abc"),
        b"<</Filter /X /Length 3>>\nstream\nabc\nendstream",
    );
    // A dictionary's /Length keeps its place, and the writer writes the
    // length of the data whatever the dictionary says.
    let mut made = Stream::new(
        Dict::from_iter([
            (name("Length"), Value::Null),
            (name("Filter"), Value::Name(name("X"))),
        ]),
        Bytes::from(&b"\nab \r\n"[..]),
    );
    made.dict_mut().insert(name("Length"), Value::Integer(99));
    let written = canonical_bytes(&Value::from(made));
    assert_eq!(
        written,
        b"<</Length 6 /Filter /X>>\nstream\n\nab \r\n\nendstream"
    );
    let Value::Stream(read) = parse(written) else {
        panic!("not a stream");
    };
    assert_eq!(read.data().as_slice(), b"\nab \r\n");
    assert_writes(
        &stream(Dict::new(), b""),
        b"<</Length 0>>\nstream\n\nendstream",
    );
}

#[test]
fn values_that_kept_their_lexemes_are_written_as_read() {
    let sources: &[&[u8]] = &[
        b"<< /B  [1   2] % a comment\n /A (x)>>",
        b"[ 1.50 +3 /N#41me <414243> << >> ]",
        b"<</Length 3/Filter/X>>stream\r\nabc\r\nendstream",
    ];
    let expected: &[&[u8]] = &[
        b"<< /B  [1   2] % a comment\n /A (x)>>",
        b"[ 1.50 +3 /N#41me <414243> << >> ]",
        b"<</Length 3/Filter/X>>\nstream\nabc\nendstream",
    ];
    for (source, expected) in sources.iter().zip(expected) {
        let value = parse(source.to_vec());
        assert_eq!(
            lexeme_bytes(&value).escape_ascii().to_string(),
            expected.escape_ascii().to_string()
        );
        assert_eq!(parse(lexeme_bytes(&value)), value);
        assert_eq!(parse(canonical_bytes(&value)), value);
    }
    assert_eq!(
        canonical_bytes(&parse(sources[0].to_vec())),
        b"<</B [1 2] /A (x)>>"
    );
}

#[test]
fn an_edited_value_keeps_the_lexemes_of_what_the_edit_did_not_change() {
    let mut value = parse(b"<< /Kids [ 1 0 R  2 0 R ] /Count 2 /Box [0 0 612.0 792] >>".to_vec());
    let Value::Dict(page) = &mut value else {
        panic!("not a dictionary");
    };
    page.insert(name("Count"), Value::Integer(3));
    assert_eq!(
        lexeme_bytes(&value),
        b"<</Kids [ 1 0 R  2 0 R ] /Count 3 /Box [0 0 612.0 792]>>"
    );
    let Value::Dict(page) = &mut value else {
        panic!("not a dictionary");
    };
    if let Some(Value::Array(kids)) = page.get_mut("Kids") {
        kids.push(Value::Reference(Reference::new(4, 0)));
    }
    assert_eq!(
        lexeme_bytes(&value),
        b"<</Kids [1 0 R 2 0 R 4 0 R] /Count 3 /Box [0 0 612.0 792]>>"
    );
    assert_eq!(parse(lexeme_bytes(&value)), value);
}

#[test]
fn what_pdf_cannot_write_is_refused() {
    let budget = Budget::default();
    for value in [f64::INFINITY, f64::NEG_INFINITY, f64::NAN] {
        let error = write_error(&real(value), &budget);
        assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
    }
    let inner = stream(Dict::new(), b"x");
    for container in [array([inner.clone()]), dict([("S", inner)])] {
        let error = write_error(&container, &budget);
        assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
    }
}

#[test]
fn the_budget_bounds_the_writer() {
    let mut deep = Value::Null;
    for _ in 0..33 {
        deep = array([deep]);
    }
    assert_eq!(
        write_error(&deep, &Budget::default()).limit(),
        Some(&LimitExceeded::over(LimitCode::ObjectValueDepth, 32, 33))
    );
    let Value::Array(shallower) = &deep else {
        panic!("not an array");
    };
    assert!(canonical_bytes(&shallower.as_slice()[0]).starts_with(b"[[["));
    let unbounded = Budget::default().with_limit(LimitCode::ObjectValueDepth, None);
    let mut out = BudgetVec::new(&unbounded);
    assert!(canonical(&mut out, &deep).is_ok());

    let small = Budget::default().with_limit(LimitCode::MemorySize, Some(8));
    assert_eq!(
        write_error(&literal(b"more than eight bytes"), &small)
            .limit()
            .map(LimitExceeded::code),
        Some(LimitCode::MemorySize)
    );
}

/// Bytes of any value, up to `max` of them.
fn bytes(max: usize) -> impl Strategy<Value = Vec<u8>> {
    proptest::collection::vec(any::<u8>(), 0..max)
}

/// Any finite `f64`, and decimals of the kind that documents hold.
fn finite_real() -> impl Strategy<Value = f64> {
    prop_oneof![
        any::<u64>()
            .prop_map(f64::from_bits)
            .prop_filter("PDF has only finite reals", |value| value.is_finite()),
        (-10_000_000_i64..10_000_000, 0_i32..10)
            .prop_map(|(digits, scale)| digits as f64 / 10_f64.powi(scale)),
        Just(-0.0),
        Just(35.681_234_5),
        Just(123_456.789),
    ]
}

/// Any value that is not a container.
fn scalar() -> impl Strategy<Value = Value> {
    prop_oneof![
        Just(Value::Null),
        any::<bool>().prop_map(Value::Boolean),
        any::<i64>().prop_map(Value::Integer),
        finite_real().prop_map(real),
        bytes(12).prop_map(|bytes| Value::Name(Name::new(bytes))),
        (bytes(24), any::<bool>()).prop_map(|(bytes, hex)| {
            Value::String(if hex {
                PdfString::hexadecimal(bytes)
            } else {
                PdfString::literal(bytes)
            })
        }),
        // hayro-syntax 0.8 reads object numbers as `i32` (I8,
        // yhay81/pyropdf#18).
        (0..=i32::MAX.unsigned_abs(), any::<u16>())
            .prop_map(|(number, generation)| Value::Reference(Reference::new(number, generation))),
    ]
}

/// Any value but a stream, nesting arrays and dictionaries up to five deep.
fn direct_value() -> impl Strategy<Value = Value> {
    scalar().prop_recursive(4, 64, 6, |inner| {
        prop_oneof![
            proptest::collection::vec(inner.clone(), 0..6)
                .prop_map(|items| Value::Array(Array::from(items))),
            proptest::collection::vec((bytes(8), inner), 0..6).prop_map(|entries| {
                Value::Dict(
                    entries
                        .into_iter()
                        .map(|(key, value)| (Name::new(key), value))
                        .collect(),
                )
            }),
        ]
    })
}

/// Any value, a stream included.
fn any_value() -> impl Strategy<Value = Value> {
    // hayro-syntax 0.8 reads no stream whose dictionary has /F, which names
    // a file that holds the data.
    let key = bytes(8).prop_filter("hayro-syntax refuses /F", |key| key != b"F");
    let stream = (
        proptest::collection::vec((key, direct_value()), 0..4),
        bytes(64),
    )
        .prop_map(|(entries, data)| {
            let dict = entries
                .into_iter()
                .map(|(key, value)| (Name::new(key), value))
                .collect();
            Value::from(Stream::new(dict, Bytes::from(data)))
        });
    prop_oneof![4 => direct_value(), 1 => stream]
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(1024))]

    #[test]
    fn written_values_read_back_as_themselves(value in any_value()) {
        let written = canonical_bytes(&value);
        let read = Value::parse(&Bytes::from(written.clone()), &Budget::default());
        prop_assert_eq!(read, Ok(value), "{}", written.escape_ascii());
    }

    #[test]
    fn values_read_back_write_their_bytes_again(value in any_value()) {
        // A container read back keeps the bytes written as its lexeme. A
        // string keeps no lexeme, and hayro-syntax 0.8 does not report its
        // form, which the bytes of a decoded string decide (I7,
        // yhay81/pyropdf#16).
        let written = canonical_bytes(&value);
        let read = parse(written.clone());
        if !matches!(read, Value::String(_)) {
            prop_assert_eq!(&lexeme_bytes(&read), &written);
        }
        prop_assert_eq!(parse(canonical_bytes(&read)), read);
    }

    #[test]
    fn edits_keep_the_lexemes_of_the_rest(
        entries in proptest::collection::vec((bytes(8), direct_value()), 1..6),
        replacement in direct_value(),
        pick in any::<prop::sample::Index>(),
    ) {
        let source: Dict = entries.into_iter().map(|(key, value)| (Name::new(key), value)).collect();
        let mut value = parse(canonical_bytes(&Value::Dict(source)));
        let Value::Dict(read) = &mut value else {
            panic!("not a dictionary");
        };
        let key = pick.get(read.entries()).0.clone();
        let unchanged: Vec<Vec<u8>> = read
            .iter()
            .filter(|(other, _)| **other != key)
            .filter_map(|(_, value)| value.lexeme().map(|lexeme| lexeme.to_vec()))
            .collect();
        read.insert(key, replacement);
        let written = lexeme_bytes(&value);
        for lexeme in unchanged {
            prop_assert!(
                written.windows(lexeme.len()).any(|window| window == lexeme),
                "{} is not in {}", lexeme.escape_ascii(), written.escape_ascii()
            );
        }
        prop_assert_eq!(parse(written), value);
    }

    #[test]
    fn every_finite_real_reads_back_exactly(value in finite_real()) {
        let read = parse(canonical_bytes(&real(value)));
        let Value::Real(read) = read else {
            panic!("{value} does not read back as a real");
        };
        prop_assert_eq!(read.value().to_bits(), value.to_bits());
    }
}

/// The corpus of real documents (tests/assets/real_world).
fn corpus() -> Vec<std::path::PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
    let mut files: Vec<_> = fs::read_dir(&dir)
        .unwrap_or_else(|error| panic!("{}: {error}", dir.display()))
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|extension| extension == "pdf"))
        .collect();
    files.sort();
    files
}

/// Returns whether hayro-syntax read `a` and `b` as the same value: the
/// oracle of the corpus, which reads hayro-syntax's objects through its own
/// accessors rather than through the store's values.
///
/// A number compares by its `Display`, which tells an integer from a real
/// and writes the shortest decimal of a real's `f64`. A dictionary compares
/// its entries whatever their order, which hayro-syntax does not keep, and a
/// stream its data rather than its `/Length`. A reference that names no
/// object, and a value that hayro-syntax skips but cannot read, are null.
fn same(a: &MaybeRef<Object<'_>>, b: &MaybeRef<Object<'_>>) -> bool {
    let null = |object: &MaybeRef<Object<'_>>| match object {
        MaybeRef::NotRef(Object::Null(_)) => true,
        MaybeRef::Ref(reference) => {
            u32::try_from(reference.obj_number).is_err()
                || u16::try_from(reference.gen_number).is_err()
        }
        MaybeRef::NotRef(_) => false,
    };
    match (a, b) {
        (MaybeRef::Ref(x), MaybeRef::Ref(y)) => x == y,
        (MaybeRef::NotRef(x), MaybeRef::NotRef(y)) => match (x, y) {
            (Object::Null(_), Object::Null(_)) => true,
            (Object::Boolean(x), Object::Boolean(y)) => x == y,
            (Object::Number(x), Object::Number(y)) => x.to_string() == y.to_string(),
            (Object::Name(x), Object::Name(y)) => x.as_ref() == y.as_ref(),
            (Object::String(x), Object::String(y)) => x.as_bytes() == y.as_bytes(),
            (Object::Array(x), Object::Array(y)) => {
                let (x, y): (Vec<_>, Vec<_>) = (x.raw_iter().collect(), y.raw_iter().collect());
                x.len() == y.len() && x.iter().zip(&y).all(|(x, y)| same(x, y))
            }
            (Object::Dict(x), Object::Dict(y)) => same_entries(x, y, &[]),
            (Object::Stream(x), Object::Stream(y)) => {
                same_entries(x.dict(), y.dict(), &[b"Length"]) && x.raw_data() == y.raw_data()
            }
            _ => false,
        },
        _ => null(a) && null(b),
    }
}

/// Returns whether hayro-syntax read the same entries in `a` and `b`, but
/// for the keys in `except`.
fn same_entries(a: &syntax::Dict<'_>, b: &syntax::Dict<'_>, except: &[&[u8]]) -> bool {
    let keys = |dict: &syntax::Dict<'_>| {
        let mut keys: Vec<Vec<u8>> = dict
            .keys()
            .map(|key| key.to_vec())
            .filter(|key| !except.contains(&key.as_slice()))
            .collect();
        keys.sort();
        keys
    };
    let null = || MaybeRef::NotRef(Object::Null(syntax::Null));
    keys(a) == keys(b)
        && keys(a).iter().all(|key| {
            let x = a.get_raw::<Object<'_>>(key).unwrap_or_else(null);
            let y = b.get_raw::<Object<'_>>(key).unwrap_or_else(null);
            same(&x, &y)
        })
}

#[test]
fn every_object_of_the_corpus_reads_back_as_itself() {
    let files = corpus();
    assert!(!files.is_empty(), "the corpus is empty");
    for path in files {
        let file = path.file_name().unwrap().to_string_lossy().into_owned();
        let pdf =
            Pdf::new(fs::read(&path).unwrap()).unwrap_or_else(|error| panic!("{file}: {error:?}"));
        let source = Bytes::new(pdf.data().clone());
        let budget = Budget::default();
        let mut objects = 0;
        for object in pdf.objects() {
            objects += 1;
            let value = Value::from_object(&object, &source, &budget)
                .unwrap_or_else(|error| panic!("{file}, object {objects}: {error}"));
            let object = MaybeRef::NotRef(object);
            for written in [lexeme_bytes(&value), canonical_bytes(&value)] {
                // hayro-syntax reads the bytes as the object it read from the
                // file, and the store reads them as the same value, keys in
                // the same order.
                let reread = MaybeRef::<Object<'_>>::from_bytes(&written);
                assert!(
                    reread.is_some_and(|reread| same(&object, &reread)),
                    "{file}, object {objects}: {}",
                    written.escape_ascii()
                );
                let read = Value::parse(&Bytes::from(written), &budget)
                    .unwrap_or_else(|error| panic!("{file}, object {objects}: {error}"));
                assert_eq!(read, value, "{file}, object {objects}");
            }
        }
        assert!(objects > 0, "{file} has no objects");
    }
}
