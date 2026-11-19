//! Width ranges keep their glyphs without work proportional to their overlap.

use std::collections::BTreeSet;
use std::sync::Arc;

use proptest::prelude::*;
use pyropdf_store::budget::Budget;
use pyropdf_store::index::Index as FileIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::settle::{Scope, Settlers, Structure};
use pyropdf_store::value::{Array, Bytes, Dict, Name, Stream, Value};
use pyropdf_store::version::Document;

use super::super::embed::Face;
use super::{FontSubsetSettler, cid_set, widths_glyphs};

fn array(values: Vec<Value>) -> Value {
    Value::Array(Array::from(values))
}

fn integers(values: &[i64]) -> Value {
    array(values.iter().copied().map(Value::Integer).collect())
}

#[test]
fn a_hundred_thousand_full_ranges_expand_each_glyph_once() {
    let values = (0..100_000)
        .flat_map(|_| [0, 65535, 500].map(Value::Integer))
        .collect();
    assert_eq!(
        widths_glyphs(&array(values)).unwrap(),
        (0..=u32::from(u16::MAX)).collect::<Vec<_>>()
    );
}

#[test]
fn full_coverage_does_not_skip_a_malformed_suffix() {
    assert!(widths_glyphs(&integers(&[0, 65535, 500, -1, 2, 500])).is_none());
    assert!(widths_glyphs(&integers(&[0, 65535, 500, 1])).is_none());
}

#[test]
fn mixed_ranges_keep_word_boundaries_gaps_and_sorted_unique_glyphs() {
    let widths = array(vec![
        Value::Integer(65535),
        integers(&[500, 500]), // Clip the array form to the 16-bit glyph domain.
        Value::Integer(128),
        Value::Integer(255),
        Value::Integer(500),
        Value::Integer(63),
        integers(&[500, 500]),
        Value::Integer(254),
        Value::Integer(257),
        Value::Integer(500),
        Value::Integer(192),
        Value::Integer(193),
        Value::Integer(500),
        Value::Integer(0),
        Value::Integer(0),
        Value::Integer(500),
        Value::Integer(10),
        integers(&[]),
        Value::Integer(9),
        Value::Integer(8),
        Value::Integer(500),
    ]);
    let expected: Vec<_> = [0, 63, 64]
        .into_iter()
        .chain(128..=257)
        .chain([65535])
        .collect();
    assert_eq!(widths_glyphs(&widths), Some(expected));
}

#[test]
fn empty_invalid_and_ignored_width_values_keep_the_existing_behavior() {
    assert_eq!(widths_glyphs(&integers(&[])), Some(Vec::new()));
    for value in [
        Value::Null,
        integers(&[0]),
        integers(&[-1, 2, 500]),
        integers(&[65536, 65536, 500]),
        integers(&[0, 65536, 500]),
        array(vec![Value::Integer(0), Value::Null, Value::Integer(500)]),
    ] {
        assert_eq!(widths_glyphs(&value), None);
    }
    // This helper extracts glyphs; it does not validate width values or
    // require the ignored third value of a range. Preserve that behavior.
    assert_eq!(widths_glyphs(&integers(&[1, 2])), Some(vec![1, 2]));
    assert_eq!(
        widths_glyphs(&array(vec![Value::Integer(1), array(vec![Value::Null])])),
        Some(vec![1])
    );
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(128))]
    #[test]
    fn ranges_match_individual_insertion(
        ranges in prop::collection::vec((any::<u16>(), 0_u16..512, any::<bool>(), any::<bool>()), 0..32)
    ) {
        let mut values = Vec::new();
        let mut expected = BTreeSet::new();
        for (first, count, listed, reversed) in ranges {
            let first = u32::from(first);
            let end = (first + u32::from(count)).min(u32::from(u16::MAX) + 1);
            if listed {
                values.push(Value::Integer(i64::from(first)));
                values.push(array(vec![Value::Integer(500); usize::from(count)]));
                expected.extend(first..end);
            } else {
                let last = end.saturating_sub(1).max(first);
                let (first, last) = if reversed { (last, first) } else { (first, last) };
                values.extend([i64::from(first), i64::from(last), 500].map(Value::Integer));
                expected.extend(first..=last);
            }
        }
        prop_assert_eq!(widths_glyphs(&array(values)), Some(expected.into_iter().collect()));
    }

    #[test]
    fn cid_set_sets_exact_big_endian_bits(cids in prop::collection::btree_set(any::<u16>(), 0..64)) {
        let list: Vec<u16> = cids.iter().copied().collect();
        let bitmap = cid_set(&list);
        let expected_len = list.last().map_or(0, |&last| usize::from(last) / 8 + 1);
        prop_assert_eq!(bitmap.len(), expected_len);
        for cid in 0_usize..expected_len * 8 {
            let present = bitmap[cid / 8] & (0x80 >> (cid % 8)) != 0;
            let wanted = u16::try_from(cid).is_ok_and(|id| cids.contains(&id));
            prop_assert_eq!(present, wanted, "CID {}", cid);
        }
    }
}

#[test]
fn cid_set_packs_ava_office_cids_msb_first() {
    assert_eq!(cid_set(&[]), Vec::<u8>::new());
    assert_eq!(
        cid_set(&[0, 1, 6, 8, 9, 11, 15, 49]),
        vec![0b1100_0010, 0b1101_0001, 0, 0, 0, 0, 0b0100_0000]
    );
}

fn minimal_pdf(header: &str, catalog_version: Option<&str>, xmp: Option<&str>) -> Vec<u8> {
    let mut objects = Vec::new();
    let mut catalog = String::from("<< /Type /Catalog /Pages 2 0 R");
    if let Some(version) = catalog_version {
        catalog.push_str(" /Version /");
        catalog.push_str(version);
    }
    if xmp.is_some() {
        catalog.push_str(" /Metadata 3 0 R");
    }
    catalog.push_str(" >>");
    objects.push(catalog.into_bytes());
    objects.push(b"<< /Type /Pages /Kids [] /Count 0 >>".to_vec());
    if let Some(xmp) = xmp {
        let mut meta = format!(
            "<< /Type /Metadata /Subtype /XML /Length {} >>\nstream\n",
            xmp.len()
        )
        .into_bytes();
        meta.extend_from_slice(xmp.as_bytes());
        meta.extend_from_slice(b"\nendstream");
        objects.push(meta);
    }
    let mut out = format!("%PDF-{header}\n").into_bytes();
    let mut offsets = Vec::new();
    for (index, body) in objects.iter().enumerate() {
        offsets.push(out.len());
        out.extend_from_slice(format!("{} 0 obj\n", index + 1).as_bytes());
        out.extend_from_slice(body);
        out.extend_from_slice(b"\nendobj\n");
    }
    let xref_at = out.len();
    out.extend_from_slice(
        format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
    );
    for offset in offsets {
        out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
    }
    out.extend_from_slice(
        format!(
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n",
            objects.len() + 1
        )
        .as_bytes(),
    );
    out
}

fn settled_cid_set(
    header: &str,
    catalog_version: Option<&str>,
    xmp: Option<&str>,
    cff_face: bool,
) -> Option<Vec<u8>> {
    let budget = Budget::default();
    let pdf = minimal_pdf(header, catalog_version, xmp);
    let index = FileIndex::open(Bytes::new(pdf), &budget).unwrap();
    let settlers = Settlers::default().with(Structure::FontSubset, Arc::new(FontSubsetSettler));
    let doc = Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None);
    doc.set_settlers(settlers);
    let raw: &[u8] = if cff_face {
        include_bytes!("../testdata/NotoSansJP-subset.otf")
    } else {
        include_bytes!("../testdata/NotoSansHebrew-subset.ttf")
    };
    let face = Face::read(raw, 0).unwrap();

    let mut tx = doc.begin();
    let mut file_dict = Dict::new();
    if cff_face {
        file_dict.insert(Name::from("Subtype"), Value::Name(Name::from("OpenType")));
    }
    let prog_num = tx.allocate().unwrap().number();
    let prog_ref = tx
        .write(
            prog_num,
            &Value::from(Stream::new(
                file_dict,
                Bytes::from(face.program.as_ref().clone()),
            )),
            &budget,
        )
        .unwrap();
    let mut desc = Dict::new();
    desc.insert(
        Name::from("Type"),
        Value::Name(Name::from("FontDescriptor")),
    );
    desc.insert(
        Name::from("FontName"),
        Value::Name(Name::from(face.postscript_name.as_str())),
    );
    desc.insert(
        Name::from(if cff_face { "FontFile3" } else { "FontFile2" }),
        Value::Reference(prog_ref),
    );
    let desc_num = tx.allocate().unwrap().number();
    let desc_ref = tx.write(desc_num, &Value::Dict(desc), &budget).unwrap();

    let mut cid_font = Dict::new();
    cid_font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
    cid_font.insert(
        Name::from("Subtype"),
        Value::Name(Name::from(if cff_face {
            "CIDFontType0"
        } else {
            "CIDFontType2"
        })),
    );
    cid_font.insert(
        Name::from("BaseFont"),
        Value::Name(Name::from(face.postscript_name.as_str())),
    );
    cid_font.insert(Name::from("FontDescriptor"), Value::Reference(desc_ref));
    cid_font.insert(Name::from("W"), integers(&[1, 1, 224, 6, 6, 608]));
    if !cff_face {
        cid_font.insert(
            Name::from("CIDToGIDMap"),
            Value::Name(Name::from("Identity")),
        );
    }
    let cid_num = tx.allocate().unwrap().number();
    let cid_ref = tx.write(cid_num, &Value::Dict(cid_font), &budget).unwrap();

    let mut type0 = Dict::new();
    type0.insert(Name::from("Type"), Value::Name(Name::from("Font")));
    type0.insert(Name::from("Subtype"), Value::Name(Name::from("Type0")));
    type0.insert(
        Name::from("BaseFont"),
        Value::Name(Name::from(face.postscript_name.as_str())),
    );
    type0.insert(
        Name::from("Encoding"),
        Value::Name(Name::from("Identity-H")),
    );
    type0.insert(
        Name::from("DescendantFonts"),
        array(vec![Value::Reference(cid_ref)]),
    );
    let font_num = tx.allocate().unwrap().number();
    tx.write(font_num, &Value::Dict(type0), &budget).unwrap();
    tx.mark(Structure::FontSubset, font_num, &budget).unwrap();
    tx.commit(&budget).unwrap();

    let mut save = doc.begin();
    save.settle(Scope::Save, &budget).unwrap();
    let settled = save.snapshot();
    drop(save);
    let desc_obj = settled.get(desc_num, &budget).unwrap().unwrap();
    let Value::Dict(settled_desc) = &**desc_obj.value() else {
        panic!("expected descriptor dictionary");
    };
    let Some(&Value::Reference(cid_set_ref)) = settled_desc.get("CIDSet") else {
        return None;
    };
    let stream_obj = settled.get(cid_set_ref.number(), &budget).unwrap().unwrap();
    let Value::Stream(stream) = &**stream_obj.value() else {
        panic!("expected CIDSet stream");
    };
    Some(stream.decoded(&budget).unwrap())
}

#[test]
fn font_subset_settler_emits_cid_set_only_for_pdfa1_before_pdf_2_0() {
    let xmp_element = "<x:xmpmeta><rdf:RDF><rdf:Description><pdfaid:part>1</pdfaid:part></rdf:Description></rdf:RDF></x:xmpmeta>";
    let xmp_attr = "<x:xmpmeta><rdf:RDF><rdf:Description pdfaid:part=\"1\" pdfaid:conformance=\"B\"/></rdf:RDF></x:xmpmeta>";
    let xmp_part2 = "<x:xmpmeta><rdf:RDF><rdf:Description><pdfaid:part>2</pdfaid:part></rdf:Description></rdf:RDF></x:xmpmeta>";
    let xmp_part4 = "<x:xmpmeta><rdf:RDF><rdf:Description><pdfaid:part>4</pdfaid:part></rdf:Description></rdf:RDF></x:xmpmeta>";
    let xmp_ua1 = "<x:xmpmeta><rdf:RDF><rdf:Description><pdfuaid:part>1</pdfuaid:part></rdf:Description></rdf:RDF></x:xmpmeta>";

    for cff_face in [true, false] {
        assert_eq!(
            settled_cid_set("1.4", None, Some(xmp_element), cff_face),
            Some(cid_set(&[0, 1, 6]))
        );
        assert_eq!(
            settled_cid_set("1.4", None, Some(xmp_attr), cff_face),
            Some(cid_set(&[0, 1, 6]))
        );
        assert_eq!(
            settled_cid_set("1.7", None, Some(xmp_part2), cff_face),
            None
        );
        assert_eq!(
            settled_cid_set("2.0", None, Some(xmp_part4), cff_face),
            None
        );
        assert_eq!(settled_cid_set("1.4", None, Some(xmp_ua1), cff_face), None);
        assert_eq!(settled_cid_set("1.4", None, None, cff_face), None);
        // A Catalog `/Version /2.0` upgrade suppresses deprecated `/CIDSet`.
        assert_eq!(
            settled_cid_set("1.4", Some("2.0"), Some(xmp_element), cff_face),
            None
        );
    }
}
