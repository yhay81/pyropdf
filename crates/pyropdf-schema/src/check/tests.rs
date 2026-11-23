use std::cell::Cell;
use std::rc::Rc;

use super::{DefectKind, Graph, View, check};
use crate::Version;

/// A small document for the check's own tests.
#[derive(Clone, Debug)]
enum V {
    Null,
    Bool(bool),
    Int(i64),
    Name(&'static str),
    Str(&'static [u8]),
    Array(Vec<V>),
    Dict(Vec<(&'static str, V)>),
    Stream(Vec<(&'static str, V)>),
    Ref(u32),
}

struct Doc {
    version: Version,
    trailer: V,
    objects: Vec<Rc<V>>,
    visits: Cell<usize>,
    limit: usize,
}

impl Graph for Doc {
    type Value = V;
    type Owned = Rc<V>;
    type Error = ();

    fn version(&self) -> Version {
        self.version
    }

    fn trailer(&self) -> &V {
        &self.trailer
    }

    fn resolve(&self, number: u32) -> Result<Option<Rc<V>>, ()> {
        Ok(self.objects.get(number as usize).cloned())
    }

    fn view<'v>(&self, value: &'v V) -> View<'v> {
        match value {
            V::Null => View::Null,
            V::Bool(value) => View::Boolean(*value),
            V::Int(value) => View::Integer(*value),
            V::Name(name) => View::Name(name.as_bytes()),
            V::Str(bytes) => View::String(bytes),
            V::Array(items) => View::Array(items.len()),
            V::Dict(_) => View::Dictionary,
            V::Stream(_) => View::Stream,
            V::Ref(number) => View::Reference(*number),
        }
    }

    fn get<'v>(&self, dict: &'v V, key: &[u8]) -> Option<&'v V> {
        match dict {
            V::Dict(entries) | V::Stream(entries) => entries
                .iter()
                .find(|(name, _)| name.as_bytes() == key)
                .map(|(_, value)| value),
            _ => None,
        }
    }

    fn entries<'v>(&self, dict: &'v V) -> Vec<(&'v [u8], &'v V)> {
        match dict {
            V::Dict(entries) | V::Stream(entries) => entries
                .iter()
                .map(|(name, value)| (name.as_bytes(), value))
                .collect(),
            _ => Vec::new(),
        }
    }

    fn elements<'v>(&self, array: &'v V) -> &'v [V] {
        match array {
            V::Array(items) => items,
            _ => &[],
        }
    }

    fn visit(&self, _first: bool) -> Result<(), ()> {
        self.visits.set(self.visits.get() + 1);
        if self.visits.get() > self.limit {
            Err(())
        } else {
            Ok(())
        }
    }
}

fn doc(objects: Vec<V>) -> Doc {
    Doc {
        version: Version { major: 1, minor: 7 },
        trailer: V::Dict(vec![("Size", V::Int(9)), ("Root", V::Ref(1))]),
        objects: objects.into_iter().map(Rc::new).collect(),
        visits: Cell::new(0),
        limit: usize::MAX,
    }
}

fn valid() -> Vec<V> {
    vec![
        V::Int(0),
        V::Dict(vec![("Type", V::Name("Catalog")), ("Pages", V::Ref(2))]),
        V::Dict(vec![
            ("Type", V::Name("Pages")),
            ("Kids", V::Array(vec![V::Ref(3)])),
            ("Count", V::Int(1)),
        ]),
        V::Dict(vec![
            ("Type", V::Name("Page")),
            ("Parent", V::Ref(2)),
            (
                "MediaBox",
                V::Array(vec![V::Int(0), V::Int(0), V::Int(612), V::Int(792)]),
            ),
            ("Resources", V::Dict(vec![])),
        ]),
    ]
}

#[test]
fn a_valid_document_passes() {
    let report = check(&doc(valid())).unwrap();
    assert_eq!(report.defects, vec![]);
    assert_eq!(report.objects, 3);
}

#[test]
fn a_missing_key_names_object_and_clause() {
    let mut objects = valid();
    objects[1] = V::Dict(vec![("Type", V::Name("Catalog"))]);
    let report = check(&doc(objects)).unwrap();
    let [defect] = report.defects.as_slice() else {
        panic!("{:?}", report.defects)
    };
    assert_eq!(defect.kind, DefectKind::Required);
    assert_eq!(defect.object, Some(1));
    assert_eq!(defect.clause, "Catalog/Pages");
    assert_eq!(defect.spec, "Table 29");
}

#[test]
fn a_wrong_type_and_a_wrong_value_are_found() {
    let mut objects = valid();
    objects[2] = V::Dict(vec![
        ("Type", V::Name("Pages")),
        ("Kids", V::Array(vec![V::Ref(3)])),
        ("Count", V::Name("one")),
    ]);
    objects[1] = V::Dict(vec![
        ("Type", V::Name("Catalog")),
        ("Pages", V::Ref(2)),
        ("PageLayout", V::Name("Sideways")),
    ]);
    let report = check(&doc(objects)).unwrap();
    let kinds: Vec<_> = report
        .defects
        .iter()
        .map(|defect| (defect.kind, defect.clause.as_str()))
        .collect();
    assert!(
        kinds.contains(&(DefectKind::Type, "PageTreeNodeRoot/Count")),
        "{kinds:?}"
    );
    assert!(
        kinds.contains(&(DefectKind::Value, "Catalog/PageLayout")),
        "{kinds:?}"
    );
}

#[test]
fn a_direct_value_that_must_be_indirect_is_found() {
    let mut objects = valid();
    let pages = objects[2].clone();
    objects[1] = V::Dict(vec![("Type", V::Name("Catalog")), ("Pages", pages)]);
    let report = check(&doc(objects)).unwrap();
    assert!(
        report
            .defects
            .iter()
            .any(|defect| defect.kind == DefectKind::Indirect && defect.clause == "Catalog/Pages")
    );
}

#[test]
fn a_cycle_is_visited_once_and_a_deep_chain_meets_the_budget() {
    let mut objects = valid();
    // The page tree's kid is the tree itself.
    objects[2] = V::Dict(vec![
        ("Type", V::Name("Pages")),
        ("Kids", V::Array(vec![V::Ref(2)])),
        ("Count", V::Int(1)),
    ]);
    let report = check(&doc(objects)).unwrap();
    assert!(report.objects <= 3);

    let mut chain = vec![
        V::Int(0),
        V::Dict(vec![("Type", V::Name("Catalog")), ("Pages", V::Ref(2))]),
    ];
    for number in 2..10_002 {
        chain.push(V::Dict(vec![
            ("Type", V::Name("Pages")),
            ("Kids", V::Array(vec![V::Ref(number + 1)])),
            ("Count", V::Int(1)),
        ]));
    }
    let mut deep = doc(chain);
    deep.limit = 100;
    assert!(check(&deep).is_err());
    assert_eq!(deep.visits.get(), 101);
}

fn invariants(objects: Vec<V>) -> Vec<(Option<u32>, String, String)> {
    let report = check(&doc(objects)).unwrap();
    report
        .defects
        .into_iter()
        .filter(|defect| defect.kind == DefectKind::Invariant)
        .map(|defect| (defect.object, defect.path, defect.clause))
        .collect()
}

#[test]
fn a_page_count_that_differs_from_the_kids_is_found() {
    let mut objects = valid();
    objects[2] = V::Dict(vec![
        ("Type", V::Name("Pages")),
        ("Kids", V::Array(vec![V::Ref(3)])),
        ("Count", V::Int(2)),
    ]);
    assert_eq!(
        invariants(objects),
        vec![(Some(2), "Count".into(), "PageTreeNode/Count".into())]
    );
    assert_eq!(invariants(valid()), vec![]);
}

#[test]
fn a_kid_whose_parent_is_another_object_is_found() {
    let mut objects = valid();
    let V::Dict(entries) = &mut objects[3] else {
        unreachable!()
    };
    entries[1].1 = V::Ref(1);
    assert_eq!(
        invariants(objects),
        vec![(Some(3), "Parent".into(), "PageTreeNode/Kids".into())]
    );
}

#[test]
fn tree_keys_out_of_order_and_wrong_limits_are_found() {
    let label = || V::Dict(vec![("S", V::Name("D"))]);
    let mut objects = valid();
    objects[1] = V::Dict(vec![
        ("Type", V::Name("Catalog")),
        ("Pages", V::Ref(2)),
        (
            "PageLabels",
            V::Dict(vec![
                (
                    "Nums",
                    V::Array(vec![
                        V::Int(0),
                        label(),
                        V::Int(4),
                        label(),
                        V::Int(2),
                        label(),
                    ]),
                ),
                ("Limits", V::Array(vec![V::Int(0), V::Int(4)])),
            ]),
        ),
    ]);
    let found = invariants(objects);
    assert!(
        found.contains(&(
            Some(1),
            "PageLabels/Nums/4".into(),
            "NumberTree/Nums".into()
        )),
        "{found:?}"
    );
    assert!(
        found.contains(&(
            Some(1),
            "PageLabels/Limits".into(),
            "NumberTree/Limits".into()
        )),
        "{found:?}"
    );
}

fn tagged(element_parent: V, next: i64) -> Vec<V> {
    let mut objects = valid();
    objects[1] = V::Dict(vec![
        ("Type", V::Name("Catalog")),
        ("Pages", V::Ref(2)),
        ("StructTreeRoot", V::Ref(4)),
    ]);
    objects.push(V::Dict(vec![
        ("Type", V::Name("StructTreeRoot")),
        ("K", V::Ref(5)),
        (
            "ParentTree",
            V::Dict(vec![(
                "Nums",
                V::Array(vec![V::Int(0), V::Ref(5), V::Int(3), V::Ref(5)]),
            )]),
        ),
        ("ParentTreeNextKey", V::Int(next)),
    ]));
    objects.push(V::Dict(vec![
        ("Type", V::Name("StructElem")),
        ("S", V::Name("P")),
        ("P", element_parent),
    ]));
    objects
}

#[test]
fn a_structure_element_whose_parent_is_another_node_is_found() {
    assert_eq!(invariants(tagged(V::Ref(4), 4)), vec![]);
    assert_eq!(
        invariants(tagged(V::Ref(1), 4)),
        vec![(Some(5), "P".into(), "StructTreeRoot/K".into())]
    );
}

#[test]
fn a_parent_tree_next_key_within_the_tree_is_found() {
    assert_eq!(
        invariants(tagged(V::Ref(4), 3)),
        vec![(
            Some(4),
            "ParentTreeNextKey".into(),
            "StructTreeRoot/ParentTreeNextKey".into()
        )]
    );
}

#[test]
fn a_reference_beyond_the_trailer_size_is_found() {
    let mut objects = valid();
    objects[1] = V::Dict(vec![
        ("Type", V::Name("Catalog")),
        ("Pages", V::Ref(2)),
        ("Outlines", V::Ref(9)),
    ]);
    objects.resize(10, V::Int(0));
    objects[9] = V::Dict(vec![("Type", V::Name("Outlines"))]);
    assert_eq!(
        invariants(objects),
        vec![(Some(9), String::new(), "FileTrailer/Size".into())]
    );
}

#[test]
fn a_missing_appearance_state_is_found() {
    let annotated = |state: Option<&'static str>| {
        let mut objects = valid();
        let V::Dict(entries) = &mut objects[3] else {
            unreachable!()
        };
        entries.push(("Annots", V::Array(vec![V::Ref(4)])));
        let mut annotation = vec![
            ("Type", V::Name("Annot")),
            ("Subtype", V::Name("Square")),
            (
                "Rect",
                V::Array(vec![V::Int(0), V::Int(0), V::Int(9), V::Int(9)]),
            ),
            ("AP", V::Dict(vec![("N", V::Dict(vec![("On", V::Int(0))]))])),
        ];
        if let Some(state) = state {
            annotation.push(("AS", V::Name(state)));
        }
        objects.push(V::Dict(annotation));
        invariants(objects)
    };
    assert_eq!(annotated(Some("On")), vec![]);
    assert_eq!(annotated(Some("Off")), vec![]);
    assert_eq!(
        annotated(None),
        vec![(Some(4), "AS".into(), "AnnotSquare/AS".into())]
    );
}

#[test]
fn pdf20_navigator_skips_deprecated_extension_keys_and_pdf17_checks_adbe_extn3() {
    let mut pdf20_objs = valid();
    pdf20_objs[1] = V::Dict(vec![
        ("Type", V::Name("Catalog")),
        ("Pages", V::Ref(2)),
        (
            "Collection",
            V::Dict(vec![
                ("Type", V::Name("Collection")),
                ("View", V::Name("C")),
                ("Navigator", V::Ref(4)),
            ]),
        ),
    ]);
    pdf20_objs.push(V::Dict(vec![
        ("Type", V::Name("Navigator")),
        ("Layout", V::Name("CustomExtLayout")),
    ]));
    let mut pdf20 = doc(pdf20_objs);
    pdf20.version = Version { major: 2, minor: 0 };
    pdf20.trailer = V::Dict(vec![
        ("Size", V::Int(9)),
        ("Root", V::Ref(1)),
        (
            "ID",
            V::Array(vec![
                V::Str(b"0123456789abcdef"),
                V::Str(b"0123456789abcdef"),
            ]),
        ),
    ]);
    assert_eq!(check(&pdf20).unwrap().defects, vec![]);

    // In PDF 1.6 (below 1.7), fn:Extension(ADBE_Extn3,1.7) || 2.0 is decidedly false.
    let mut pdf16_objs = valid();
    pdf16_objs[1] = V::Dict(vec![
        ("Type", V::Name("Catalog")),
        ("Pages", V::Ref(2)),
        (
            "Collection",
            V::Dict(vec![
                ("Type", V::Name("Collection")),
                ("Navigator", V::Ref(4)),
            ]),
        ),
    ]);
    pdf16_objs.push(V::Dict(vec![("Layout", V::Name("FilmStrip"))]));
    let mut pdf16 = doc(pdf16_objs);
    pdf16.version = Version { major: 1, minor: 6 };
    let pdf16_report = check(&pdf16).unwrap();
    let pdf16_clauses: Vec<_> = pdf16_report
        .defects
        .iter()
        .map(|d| (d.kind, d.clause.as_str()))
        .collect();
    assert!(
        pdf16_clauses.contains(&(DefectKind::Version, "Collection/Navigator")),
        "{pdf16_clauses:?}"
    );

    // In PDF 1.7 without ADBE_Extn3, Navigator's ADBE_Extn3-only required keys are not demanded;
    // with ADBE_Extn3 declared in Catalog["Extensions"], they are checked as required.
    let mut no_ext_objs = valid();
    no_ext_objs[1] = V::Dict(vec![
        ("Type", V::Name("Catalog")),
        ("Pages", V::Ref(2)),
        (
            "Collection",
            V::Dict(vec![
                ("Type", V::Name("Collection")),
                ("View", V::Name("C")),
                ("Navigator", V::Ref(4)),
            ]),
        ),
    ]);
    no_ext_objs.push(V::Dict(vec![]));
    let no_ext_report = check(&doc(no_ext_objs)).unwrap();
    let no_ext_clauses: Vec<_> = no_ext_report
        .defects
        .iter()
        .map(|d| (d.kind, d.clause.as_str()))
        .collect();
    assert!(!no_ext_clauses.contains(&(DefectKind::Required, "Navigator/SWF")));

    let mut ext_objs = valid();
    ext_objs[1] = V::Dict(vec![
        ("Type", V::Name("Catalog")),
        ("Pages", V::Ref(2)),
        (
            "Extensions",
            V::Dict(vec![(
                "ADBE",
                V::Dict(vec![
                    ("BaseVersion", V::Name("1.7")),
                    ("ExtensionLevel", V::Int(3)),
                ]),
            )]),
        ),
        (
            "Collection",
            V::Dict(vec![
                ("Type", V::Name("Collection")),
                ("View", V::Name("C")),
                ("Navigator", V::Ref(4)),
            ]),
        ),
    ]);
    ext_objs.push(V::Dict(vec![]));
    let ext_report = check(&doc(ext_objs)).unwrap();
    let ext_clauses: Vec<_> = ext_report
        .defects
        .iter()
        .map(|d| (d.kind, d.clause.as_str()))
        .collect();
    assert!(!ext_clauses.contains(&(DefectKind::Version, "Collection/Navigator")));
    assert!(!ext_clauses.contains(&(DefectKind::Version, "Collection/View")));
    assert!(ext_clauses.contains(&(DefectKind::Required, "Navigator/SWF")));
    assert!(ext_clauses.contains(&(DefectKind::Required, "Navigator/ID")));
    assert!(ext_clauses.contains(&(DefectKind::Required, "Navigator/APIVersion")));
    assert!(ext_clauses.contains(&(DefectKind::Required, "Navigator/Resources")));
}

#[test]
fn single_quoted_string_possible_values_are_admitted() {
    let mut ok_objs = valid();
    ok_objs[1] = V::Dict(vec![
        ("Type", V::Name("Catalog")),
        ("Pages", V::Ref(2)),
        (
            "OpenAction",
            V::Dict(vec![
                ("Type", V::Name("Action")),
                ("S", V::Name("SubmitForm")),
                ("F", V::Str(b"https://example.test/submit")),
                ("CharSet", V::Str(b"utf-8")),
            ]),
        ),
    ]);
    assert_eq!(check(&doc(ok_objs)).unwrap().defects, vec![]);

    let mut bad_objs = valid();
    bad_objs[1] = V::Dict(vec![
        ("Type", V::Name("Catalog")),
        ("Pages", V::Ref(2)),
        (
            "OpenAction",
            V::Dict(vec![
                ("Type", V::Name("Action")),
                ("S", V::Name("SubmitForm")),
                ("F", V::Str(b"https://example.test/submit")),
                ("CharSet", V::Str(b"latin-1")),
            ]),
        ),
    ]);
    let bad_report = check(&doc(bad_objs)).unwrap();
    assert_eq!(bad_report.defects.len(), 1);
    assert_eq!(bad_report.defects[0].kind, DefectKind::Value);
    assert_eq!(bad_report.defects[0].clause, "ActionSubmitForm/CharSet");
}

fn with_image(entries: Vec<(&'static str, V)>) -> Vec<V> {
    let mut objects = valid();
    let V::Dict(page) = &mut objects[3] else {
        unreachable!()
    };
    for (key, value) in page.iter_mut() {
        if *key == "Resources" {
            *value = V::Dict(vec![("XObject", V::Dict(vec![("Im0", V::Ref(4))]))]);
        }
    }
    objects.push(V::Stream(entries));
    objects
}

#[test]
fn contains_checks_absent_single_name_and_array_filters_on_images() {
    let base = || {
        vec![
            ("Type", V::Name("XObject")),
            ("Subtype", V::Name("Image")),
            ("Width", V::Int(1)),
            ("Height", V::Int(1)),
            ("Length", V::Int(1)),
        ]
    };

    // Absent /Filter, single-Name /FlateDecode, and array [/FlateDecode] all require ColorSpace and BitsPerComponent.
    for filter in [
        None,
        Some(V::Name("FlateDecode")),
        Some(V::Array(vec![V::Name("FlateDecode")])),
    ] {
        let mut entries = base();
        if let Some(filter) = filter {
            entries.push(("Filter", filter));
        }
        let report = check(&doc(with_image(entries))).unwrap();
        let required: Vec<_> = report
            .defects
            .iter()
            .filter(|d| d.kind == DefectKind::Required)
            .map(|d| d.clause.as_str())
            .collect();
        assert!(
            required.contains(&"XObjectImage/ColorSpace"),
            "missing ColorSpace check: {required:?}"
        );
        assert!(
            required.contains(&"XObjectImage/BitsPerComponent"),
            "missing BitsPerComponent check: {required:?}"
        );
    }

    // JPXDecode (single Name or array) and ImageMask=true exempt ColorSpace and BitsPerComponent.
    for extra in [
        ("Filter", V::Name("JPXDecode")),
        (
            "Filter",
            V::Array(vec![V::Name("FlateDecode"), V::Name("JPXDecode")]),
        ),
        ("ImageMask", V::Bool(true)),
    ] {
        let mut entries = base();
        entries.push(extra);
        let report = check(&doc(with_image(entries))).unwrap();
        let required: Vec<_> = report
            .defects
            .iter()
            .filter(|d| d.kind == DefectKind::Required)
            .map(|d| d.clause.as_str())
            .collect();
        assert!(
            !required.contains(&"XObjectImage/ColorSpace")
                && !required.contains(&"XObjectImage/BitsPerComponent"),
            "unexpected required defect: {required:?}"
        );
    }

    // Soft-mask BitsPerComponent RequiredValue checks fire on single-Name filters too.
    let mut smask_objs = with_image(vec![
        ("Type", V::Name("XObject")),
        ("Subtype", V::Name("Image")),
        ("Width", V::Int(1)),
        ("Height", V::Int(1)),
        ("ColorSpace", V::Name("DeviceRGB")),
        ("BitsPerComponent", V::Int(8)),
        ("SMask", V::Ref(5)),
        ("Length", V::Int(3)),
    ]);
    smask_objs.push(V::Stream(vec![
        ("Type", V::Name("XObject")),
        ("Subtype", V::Name("Image")),
        ("Width", V::Int(1)),
        ("Height", V::Int(1)),
        ("ColorSpace", V::Name("DeviceGray")),
        ("BitsPerComponent", V::Int(4)),
        ("Filter", V::Name("DCTDecode")),
        ("Length", V::Int(1)),
    ]));
    let report = check(&doc(smask_objs)).unwrap();
    assert!(
        report
            .defects
            .iter()
            .any(|d| d.kind == DefectKind::Value
                && d.clause == "XObjectImageSoftMask/BitsPerComponent"),
        "{:?}",
        report.defects
    );
}

#[test]
fn multi_level_parent_tree_next_key_is_checked() {
    let build = |next: i64| {
        let mut objects = valid();
        objects[1] = V::Dict(vec![
            ("Type", V::Name("Catalog")),
            ("Pages", V::Ref(2)),
            ("StructTreeRoot", V::Ref(4)),
        ]);
        // 4: StructTreeRoot -> indirect multi-level ParentTree 6 without /Limits on root
        objects.push(V::Dict(vec![
            ("Type", V::Name("StructTreeRoot")),
            ("K", V::Ref(5)),
            ("ParentTree", V::Ref(6)),
            ("ParentTreeNextKey", V::Int(next)),
        ]));
        // 5: StructElem
        objects.push(V::Dict(vec![
            ("Type", V::Name("StructElem")),
            ("S", V::Name("P")),
            ("P", V::Ref(4)),
        ]));
        // 6: ParentTree root (/Kids only, no /Limits per ISO 32000-2 Table 37)
        objects.push(V::Dict(vec![(
            "Kids",
            V::Array(vec![V::Ref(7), V::Ref(8)]),
        )]));
        // 7: Intermediate node
        objects.push(V::Dict(vec![
            ("Limits", V::Array(vec![V::Int(0), V::Int(2)])),
            (
                "Nums",
                V::Array(vec![V::Int(0), V::Ref(5), V::Int(2), V::Ref(5)]),
            ),
        ]));
        // 8: Intermediate node pointing to leaf 9
        objects.push(V::Dict(vec![
            ("Limits", V::Array(vec![V::Int(3), V::Int(7)])),
            ("Kids", V::Array(vec![V::Ref(9)])),
        ]));
        // 9: Leaf holding key 7
        objects.push(V::Dict(vec![
            ("Limits", V::Array(vec![V::Int(3), V::Int(7)])),
            (
                "Nums",
                V::Array(vec![V::Int(3), V::Ref(5), V::Int(7), V::Ref(5)]),
            ),
        ]));
        let mut d = doc(objects);
        d.trailer = V::Dict(vec![("Size", V::Int(12)), ("Root", V::Ref(1))]);
        d
    };

    let ok_invariants: Vec<_> = check(&build(8))
        .unwrap()
        .defects
        .into_iter()
        .filter(|d| d.kind == DefectKind::Invariant)
        .collect();
    assert_eq!(ok_invariants, vec![]);

    let bad_invariants: Vec<_> = check(&build(7))
        .unwrap()
        .defects
        .into_iter()
        .filter(|d| d.kind == DefectKind::Invariant)
        .map(|d| (d.object, d.path, d.clause))
        .collect();
    assert_eq!(
        bad_invariants,
        vec![(
            Some(4),
            "ParentTreeNextKey".into(),
            "StructTreeRoot/ParentTreeNextKey".into()
        )]
    );
}

#[test]
fn appearance_state_checks_indirect_direct_null_and_rollover_down() {
    let base_annot = |ap: V, state: Option<V>| {
        let mut entries = vec![
            ("Type", V::Name("Annot")),
            ("Subtype", V::Name("Square")),
            (
                "Rect",
                V::Array(vec![V::Int(0), V::Int(0), V::Int(9), V::Int(9)]),
            ),
            ("AP", ap),
        ];
        if let Some(state) = state {
            entries.push(("AS", state));
        }
        V::Dict(entries)
    };

    // Indirect /AP with indirect /N state subdictionary and /AS null.
    let mut objects = valid();
    let V::Dict(page) = &mut objects[3] else {
        unreachable!()
    };
    page.push(("Annots", V::Array(vec![V::Ref(4)])));
    objects.push(base_annot(V::Ref(5), Some(V::Null)));
    objects.push(V::Dict(vec![("N", V::Ref(6))]));
    objects.push(V::Dict(vec![("On", V::Stream(vec![]))]));
    assert_eq!(
        invariants(objects),
        vec![(Some(4), "AS".into(), "AnnotSquare/AS".into())]
    );

    // Rollover /R and Down /D state subdictionaries with single-stream /N.
    for key in ["R", "D"] {
        let mut objects = valid();
        let V::Dict(page) = &mut objects[3] else {
            unreachable!()
        };
        page.push(("Annots", V::Array(vec![V::Ref(4)])));
        objects.push(base_annot(
            V::Dict(vec![("N", V::Ref(5)), (key, V::Ref(6))]),
            None,
        ));
        objects.push(V::Stream(vec![]));
        objects.push(V::Dict(vec![("Over", V::Ref(5))]));
        assert_eq!(
            invariants(objects),
            vec![(Some(4), "AS".into(), "AnnotSquare/AS".into())],
            "expected missing /AS for /{key}"
        );
    }

    // Direct annotation inside /Annots array.
    let mut objects = valid();
    let V::Dict(page) = &mut objects[3] else {
        unreachable!()
    };
    page.push((
        "Annots",
        V::Array(vec![base_annot(
            V::Dict(vec![("N", V::Dict(vec![("On", V::Ref(4))]))]),
            None,
        )]),
    ));
    objects.push(V::Stream(vec![]));
    assert_eq!(
        invariants(objects),
        vec![(Some(3), "Annots/0/AS".into(), "AnnotSquare/AS".into())]
    );
}
