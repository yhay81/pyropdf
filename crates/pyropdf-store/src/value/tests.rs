//! Values convert from what hayro-syntax parses, with their key order and
//! the lexemes that hayro-syntax 0.8 reports (DESIGN.md section 4.2).

use super::*;
use crate::budget::{Budget, BudgetVec, CancellationToken, LimitCode};
use crate::{ErrorKind, LimitExceeded};

/// Parses `bytes` with the default budget.
fn parse(bytes: &'static [u8]) -> Value {
    Value::parse(&Bytes::from(bytes), &Budget::default())
        .unwrap_or_else(|error| panic!("{}: {error}", bytes.escape_ascii()))
}

fn real(value: f64) -> Value {
    Value::Real(Real::new(value))
}

fn name(name: &str) -> Name {
    Name::from(name)
}

fn dict(value: &Value) -> &Dict {
    match value {
        Value::Dict(dict) => dict,
        other => panic!("not a dictionary: {other:?}"),
    }
}

fn array(value: &Value) -> &Array {
    match value {
        Value::Array(array) => array,
        other => panic!("not an array: {other:?}"),
    }
}

fn stream(value: &Value) -> &Stream {
    match value {
        Value::Stream(stream) => stream,
        other => panic!("not a stream: {other:?}"),
    }
}

fn keys(dict: &Dict) -> Vec<&[u8]> {
    dict.keys().map(Name::as_bytes).collect()
}

fn lexeme(value: &Value) -> Option<&[u8]> {
    value.lexeme().map(Bytes::as_slice)
}

#[test]
fn scalars_convert_to_their_values() {
    let cases: &[(&[u8], Value)] = &[
        (b"null", Value::Null),
        (b"true", Value::Boolean(true)),
        (b"false", Value::Boolean(false)),
        (b"42", Value::Integer(42)),
        (b"-17", Value::Integer(-17)),
        (b"+5", Value::Integer(5)),
        (b"9223372036854775807", Value::Integer(i64::MAX)),
        (b"-9223372036854775808", Value::Integer(i64::MIN)),
        (b"3.0", real(3.0)),
        (b".5", real(0.5)),
        (b"-.002", real(-0.002)),
        (b"4.", real(4.0)),
        (b"/Type", Value::Name(name("Type"))),
        (b"/", Value::Name(name(""))),
        (b"/A#20B#23", Value::Name(name("A B#"))),
        (b"(Hello)", Value::String(PdfString::literal(*b"Hello"))),
        (
            b"<48656C6C6F>",
            Value::String(PdfString::literal(*b"Hello")),
        ),
        (b"<00ff>", Value::String(PdfString::hexadecimal([0, 255]))),
        (b"12 0 R", Value::Reference(Reference::new(12, 0))),
        (
            b"2147483647 65535 R",
            Value::Reference(Reference::new(2_147_483_647, 65_535)),
        ),
    ];
    for (bytes, expected) in cases {
        let value = Value::parse(&Bytes::from(*bytes), &Budget::default()).unwrap();
        assert_eq!(&value, expected, "{}", bytes.escape_ascii());
    }
}

#[test]
fn integers_and_reals_stay_apart() {
    assert_eq!(parse(b"3"), Value::Integer(3));
    assert_eq!(parse(b"3.0"), real(3.0));
    assert_ne!(parse(b"3"), parse(b"3.0"));
    // hayro-syntax reads an integer beyond `i64` as a real.
    assert_eq!(
        parse(b"12345678901234567890"),
        real(12_345_678_901_234_567_890.0)
    );
    // A real keeps the sign of zero, which an integer cannot have.
    let Value::Real(zero) = parse(b"-0.0") else {
        panic!("-0.0 is not a real");
    };
    assert_eq!(zero.value().to_bits(), (-0.0_f64).to_bits());
    assert_eq!(parse(b"-0"), Value::Integer(0));
}

#[test]
fn reals_keep_their_f64_exactly() {
    // lopdf keeps reals as `f32`, which writes these back as 35.681236 and
    // 123456.79 (yhay81/pylopdf#370).
    let cases: [(&'static [u8], f64); 2] =
        [(b"35.6812345", 35.681_234_5), (b"123456.789", 123_456.789)];
    for (bytes, expected) in cases {
        let Value::Real(read) = parse(bytes) else {
            panic!("{} is not a real", bytes.escape_ascii());
        };
        assert_eq!(read.value().to_bits(), expected.to_bits());
        assert_ne!(f64::from(expected as f32), expected);
        assert_eq!(read.lexeme(), None);
    }
}

#[test]
fn references_that_name_no_object_read_as_null() {
    assert_eq!(parse(b"[-1 0 R 1 65536 R 0 0 R]"), {
        Value::Array(Array::from(vec![
            Value::Null,
            Value::Null,
            Value::Reference(Reference::new(0, 0)),
        ]))
    });
}

#[test]
fn arrays_keep_their_elements_and_their_lexeme() {
    let source: &[u8] = b"[1 (two) /Three [4]<< /Five 5 >> 6 0 R null]";
    let value = parse(source);
    let expected = Array::from(vec![
        Value::Integer(1),
        Value::String(PdfString::literal(*b"two")),
        Value::Name(name("Three")),
        Value::Array(Array::from(vec![Value::Integer(4)])),
        Value::Dict(Dict::from_iter([(name("Five"), Value::Integer(5))])),
        Value::Reference(Reference::new(6, 0)),
        Value::Null,
    ]);
    assert_eq!(array(&value), &expected);
    assert_eq!(lexeme(&value), Some(source));
    let items = array(&value).as_slice();
    assert_eq!(lexeme(&items[3]), Some(&b"[4]"[..]));
    assert_eq!(lexeme(&items[4]), Some(&b"<< /Five 5 >>"[..]));
    assert_eq!(lexeme(&parse(b"[]")), Some(&b"[]"[..]));
}

#[test]
fn dictionaries_keep_their_key_order_and_their_lexeme() {
    let source: &[u8] = b"<< /Zebra 1 /Apple [2] /Mango << /Kiwi 3 >> >>";
    let value = parse(source);
    assert_eq!(keys(dict(&value)), [&b"Zebra"[..], b"Apple", b"Mango"]);
    assert_eq!(lexeme(&value), Some(source));
    let mango = dict(&value).get("Mango").unwrap();
    assert_eq!(lexeme(mango), Some(&b"<< /Kiwi 3 >>"[..]));
    assert_eq!(
        lexeme(dict(&value).get("Apple").unwrap()),
        Some(&b"[2]"[..])
    );
}

#[test]
fn keys_written_with_escapes_keep_their_place() {
    // hayro-syntax 0.8 does not borrow a key with a `#` escape.
    let value = parse(b"<< /Z 1 /A#20B 2 /M (3) /#41 4 /N#23 [5] >>");
    assert_eq!(keys(dict(&value)), [&b"Z"[..], b"A B", b"M", b"A", b"N#"]);
    assert_eq!(dict(&value).get("A B"), Some(&Value::Integer(2)));
    let value = parse(b"<< /#43 1 /#42 2 /#41 3 >>");
    assert_eq!(keys(dict(&value)), [&b"C"[..], b"B", b"A"]);
}

#[test]
fn a_repeated_key_keeps_its_first_place_and_its_last_value() {
    for source in [&b"<< /A 1 /B 2 /A 3 >>"[..], b"<< /A#20 1 /B 2 /A#20 3 >>"] {
        let value = parse(source);
        let first = &dict(&value).entries()[0];
        assert_eq!(dict(&value).len(), 2, "{}", source.escape_ascii());
        assert_eq!(first.1, Value::Integer(3), "{}", source.escape_ascii());
        assert_eq!(dict(&value).get("B"), Some(&Value::Integer(2)));
    }
}

#[test]
fn what_hayro_syntax_skips_between_entries_stays_in_the_lexeme() {
    let source: &[u8] = b"<< /A 1 2000 (x) /B 2 % comment\n>>";
    let value = parse(source);
    assert_eq!(
        dict(&value),
        &Dict::from_iter([
            (name("A"), Value::Integer(1)),
            (name("B"), Value::Integer(2))
        ])
    );
    assert_eq!(lexeme(&value), Some(source));
    // The same with a key whose place needs the entries read again.
    let value = parse(b"<< /A#41 1 2000 (x) /B 2 >>");
    assert_eq!(keys(dict(&value)), [&b"AA"[..], b"B"]);
}

#[test]
fn a_value_that_hayro_syntax_cannot_read_is_null() {
    // hayro-syntax skips a lone `+` as a number but does not read it.
    let value = parse(b"<< /A + /B 2 >>");
    assert_eq!(dict(&value).get("A"), Some(&Value::Null));
    assert_eq!(dict(&value).get("B"), Some(&Value::Integer(2)));
}

#[test]
fn a_panic_in_hayro_syntax_is_a_malformed_input_error() {
    // hayro-syntax 0.8 skips this name but panics reading it (I3,
    // yhay81/pyropdf#19). Its next release reads it without panicking, and
    // this test then changes with the adoption.
    let error = Value::parse(&Bytes::from(&b"[/AB#FG]"[..]), &Budget::default()).unwrap_err();
    assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
    // The order of a dictionary's keys falls back to their bytes when
    // reading its entries again panics.
    let value = parse(b"<< /Z 1 /B#20 2 /AB#FG 3 /C 4 >>");
    assert_eq!(keys(dict(&value)), [&b"Z"[..], b"C", b"B "]);
    assert_eq!(dict(&value).get("B "), Some(&Value::Integer(2)));
}

#[test]
fn bytes_that_begin_no_value_are_malformed_input() {
    for source in [&b""[..], b")", b">>", b" 1"] {
        let error = Value::parse(&Bytes::from(source), &Budget::default()).unwrap_err();
        assert!(
            matches!(error.kind(), ErrorKind::Malformed(_)),
            "{}: {error}",
            source.escape_ascii()
        );
    }
}

#[test]
fn strings_keep_the_form_that_hayro_syntax_shows() {
    let form = |source: &'static [u8]| match parse(source) {
        Value::String(string) => string.form(),
        other => panic!("not a string: {other:?}"),
    };
    // A literal string that needs no decoding is borrowed, so its form is
    // known; hayro-syntax decodes every other string, and its form follows
    // its bytes.
    assert_eq!(form(b"(\xFE\xFF)"), StringForm::Literal);
    assert_eq!(form(b"(two\\nlines)"), StringForm::Literal);
    assert_eq!(form(b"<414243>"), StringForm::Literal);
    assert_eq!(form(b"(\\376\\377)"), StringForm::Hexadecimal);
    assert_eq!(form(b"<FEFF>"), StringForm::Hexadecimal);
}

#[test]
fn a_stream_shares_its_data_and_keeps_its_lexeme_when_its_length_is_right() {
    let source = Bytes::from(&b"<< /Length 3 /Filter /X >>\nstream\nabc\nendstream"[..]);
    let value = Value::parse(&source, &Budget::default()).unwrap();
    let stream = stream(&value);
    assert_eq!(stream.data().as_slice(), b"abc");
    let at = source.windows(3).position(|window| window == b"abc");
    assert_eq!(source.offset_of(stream.data()), at);
    assert_eq!(keys(stream.dict()), [&b"Length"[..], b"Filter"]);
    assert_eq!(
        stream.dict().lexeme().map(Bytes::as_slice),
        Some(&b"<< /Length 3 /Filter /X >>"[..])
    );
}

#[test]
fn a_stream_whose_length_is_wrong_or_missing_gets_the_length_of_its_data() {
    let cases: [(&'static [u8], &[&[u8]]); 3] = [
        (
            b"<< /Length 99 /X 1 >>\nstream\nabcdef\nendstream",
            &[b"Length", b"X"],
        ),
        (
            b"<< /X 1 /Length 5 0 R >>\nstream\nabcdef\nendstream",
            &[b"X", b"Length"],
        ),
        (b"<< /X 1 >>\nstream\nabcdef\nendstream", &[b"X", b"Length"]),
    ];
    for (source, keys_read) in cases {
        let value = parse(source);
        let stream = stream(&value);
        assert_eq!(stream.data().as_slice(), b"abcdef");
        assert_eq!(stream.dict().get("Length"), Some(&Value::Integer(6)));
        assert_eq!(keys(stream.dict()), keys_read, "{}", source.escape_ascii());
        assert_eq!(stream.dict().lexeme(), None, "{}", source.escape_ascii());
    }
}

#[test]
fn changing_a_container_forgets_its_lexeme_and_no_other() {
    let mut value = parse(b"<< /A [1 2] /B << /C 3 >> /D 4 >>");
    let Value::Dict(outer) = &mut value else {
        panic!("not a dictionary");
    };
    outer.insert(name("D"), Value::Integer(5));
    assert_eq!(outer.lexeme(), None);
    assert_eq!(lexeme(outer.get("A").unwrap()), Some(&b"[1 2]"[..]));
    assert_eq!(keys(outer), [&b"A"[..], b"B", b"D"]);

    let Some(Value::Array(inner)) = outer.get_mut("A") else {
        panic!("not an array");
    };
    inner.push(Value::Integer(3));
    assert_eq!(inner.lexeme(), None);
    assert_eq!(lexeme(outer.get("B").unwrap()), Some(&b"<< /C 3 >>"[..]));

    let mut array = parse(b"[1 [2] 3]");
    let Value::Array(items) = &mut array else {
        panic!("not an array");
    };
    assert_eq!(items.remove(0), Some(Value::Integer(1)));
    assert_eq!(items.lexeme(), None);
    assert_eq!(lexeme(&items.as_slice()[0]), Some(&b"[2]"[..]));
    assert_eq!(items.insert(5, Value::Null), Err(Value::Null));
    assert_eq!(items.remove(5), None);

    let mut dict = Dict::from_iter([
        (name("A"), Value::Integer(1)),
        (name("B"), Value::Integer(2)),
    ]);
    assert_eq!(
        dict.insert(name("A"), Value::Integer(3)),
        Some(Value::Integer(1))
    );
    assert_eq!(dict.remove("Z"), None);
    assert_eq!(dict.remove("A"), Some(Value::Integer(3)));
    assert_eq!(keys(&dict), [&b"B"[..]]);
}

#[test]
fn setting_a_stream_s_data_sets_its_length() {
    let mut value = parse(b"<< /Length 3 >>\nstream\nabc\nendstream");
    let Value::Stream(stream) = &mut value else {
        panic!("not a stream");
    };
    // Data of the same length leaves the dictionary as it was read.
    stream.set_data(Bytes::from(&b"xyz"[..]));
    assert!(stream.dict().lexeme().is_some());
    stream.set_data(Bytes::from(&b"wxyz"[..]));
    assert_eq!(stream.dict().get("Length"), Some(&Value::Integer(4)));
    assert_eq!(stream.dict().lexeme(), None);
    let made = Stream::new(
        Dict::from_iter([(name("Length"), Value::Null)]),
        Bytes::default(),
    );
    assert_eq!(made.dict().get("Length"), Some(&Value::Integer(0)));
}

#[test]
fn the_budget_bounds_the_conversion() {
    let source = Bytes::from(&b"[[[1]]]"[..]);
    let deep = Budget::default().with_limit(LimitCode::ObjectDepth, Some(2));
    assert_eq!(
        Value::parse(&source, &deep).unwrap_err().limit(),
        Some(&LimitExceeded::over(LimitCode::ObjectDepth, 2, 3))
    );
    let shallow = Bytes::from(&b"[[1]]"[..]);
    assert!(Value::parse(&shallow, &deep).is_ok());

    let memory = Budget::default().with_limit(LimitCode::MemorySize, Some(64));
    let long = Bytes::from(b"[1 2 3 4 5 6 7 8 9]".to_vec());
    assert_eq!(
        Value::parse(&long, &memory)
            .unwrap_err()
            .limit()
            .map(LimitExceeded::code),
        Some(LimitCode::MemorySize)
    );

    let token = CancellationToken::new();
    token.cancel();
    let cancelled = Budget::default().with_cancellation(token);
    assert_eq!(
        Value::parse(&shallow, &cancelled).unwrap_err().limit(),
        Some(&LimitExceeded::new(LimitCode::Cancelled))
    );
}

#[test]
fn equality_ignores_how_values_are_written() {
    assert_eq!(PdfString::literal(*b"AB"), PdfString::hexadecimal(*b"AB"));
    assert_ne!(Real::new(0.0), Real::new(-0.0));
    assert_eq!(parse(b"[1  2]"), parse(b"[1 2]"));
    assert_ne!(
        Dict::from_iter([(name("A"), Value::Null), (name("B"), Value::Null)]),
        Dict::from_iter([(name("B"), Value::Null), (name("A"), Value::Null)])
    );
    assert_eq!(format!("{:?}", name("A B")), "/A B");
    assert_eq!(
        format!("{:?}", PdfString::hexadecimal([0, b'a'])),
        "Hexadecimal(\"\\x00a\")"
    );
}

#[test]
fn a_real_with_a_lexeme_is_written_as_it() {
    // hayro-syntax 0.8 reports no lexeme for a number, until I7.
    let source = Bytes::from(&b"35.68123450"[..]);
    let kept = Value::Real(Real {
        lexeme: Some(source.clone()),
        ..Real::new(35.681_234_5)
    });
    let budget = Budget::default();
    let mut out = crate::budget::BudgetVec::new(&budget);
    crate::write::value(&mut out, &kept).unwrap();
    assert_eq!(out.as_slice(), source.as_slice());
    let mut out = crate::budget::BudgetVec::new(&budget);
    crate::write::canonical(&mut out, &kept).unwrap();
    assert_eq!(out.as_slice(), b"35.6812345");
}

#[test]
fn the_deepest_object_that_hayro_syntax_parses_converts_and_writes_back() {
    let nested = |depth: usize| format!("{}0{}", "[".repeat(depth), "]".repeat(depth)).into_bytes();
    let budget = Budget::default().with_limit(LimitCode::ObjectValueDepth, None);
    let deepest = Value::parse(&Bytes::from(nested(64)), &budget).unwrap();
    let mut out = crate::budget::BudgetVec::new(&budget);
    crate::write::canonical(&mut out, &deepest).unwrap();
    assert_eq!(out.as_slice(), nested(64));
    let error = Value::parse(&Bytes::from(nested(65)), &budget).unwrap_err();
    assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
}

#[test]
fn a_real_keeps_the_lexeme_it_is_made_from() {
    let real = Real::from_lexeme(Bytes::from(b"35.68123456789012345".to_vec())).unwrap();
    assert_eq!(real.value(), 35.681_234_567_890_12);
    let budget = Budget::default();
    let mut out = BudgetVec::new(&budget);
    crate::write::value(&mut out, &Value::Real(real)).unwrap();
    assert_eq!(out.as_slice(), b"35.68123456789012345");
    for lexeme in [&b"-.5"[..], b"+1.", b"0.000"] {
        assert!(
            Real::from_lexeme(Bytes::from(lexeme.to_vec())).is_some(),
            "{lexeme:?}"
        );
    }
    for lexeme in [&b"1"[..], b"1e5", b".", b"1.2.3", b"--1.0", b"", b"1.0 "] {
        assert!(
            Real::from_lexeme(Bytes::from(lexeme.to_vec())).is_none(),
            "{lexeme:?}"
        );
    }
}

#[test]
fn streams_decode_through_their_filters_within_the_budget() {
    let value =
        parse(b"<</Length 15/Filter/ASCIIHexDecode>>stream\n68656C6C6F20776F726C64>\nendstream");
    assert_eq!(
        stream(&value).decoded(&Budget::default()).unwrap(),
        b"hello world"
    );
    let budget = Budget::default().with_limit(LimitCode::DecompressedSize, Some(4));
    let error = stream(&value).decoded(&budget).unwrap_err();
    assert_eq!(
        error.limit().map(LimitExceeded::code),
        Some(LimitCode::DecompressedSize)
    );
    let plain = parse(b"<</Length 2>>stream\nab\nendstream");
    assert_eq!(stream(&plain).decoded(&Budget::default()).unwrap(), b"ab");
    let image = parse(b"<</Length 2/Filter/JBIG2Decode>>stream\nab\nendstream");
    let error = stream(&image).decoded(&Budget::default()).unwrap_err();
    assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
}
