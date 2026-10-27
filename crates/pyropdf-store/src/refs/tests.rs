//! Tests of the reverse-reference index: an updated index equals a full
//! build, an update reads only the changed objects, and the roles and paths
//! of references.

use std::sync::Arc;

use proptest::prelude::*;

use super::*;
use crate::budget::LimitCode;
use crate::value::{Array, Bytes, Name, PdfString, Reference, Stream, StringForm, Value};
use crate::version::{Document, MemoryBase};
use crate::write;

/// The object numbers of the generated documents.
const NUMBERS: u32 = 8;

fn bytes_of(value: &Value) -> Bytes {
    let budget = Budget::default();
    let mut out = BudgetVec::new(&budget);
    write::value(&mut out, value).unwrap();
    Bytes::new(out.into_vec())
}

fn reference(number: u32) -> Value {
    Value::Reference(Reference::new(number, 0))
}

fn name(name: &str) -> Value {
    Value::Name(Name::from(name))
}

fn string(text: &str) -> Value {
    Value::String(PdfString::new(text.as_bytes(), StringForm::Literal))
}

fn dict(entries: Vec<(&str, Value)>) -> Value {
    Value::Dict(
        entries
            .into_iter()
            .map(|(key, value)| (Name::from(key), value))
            .collect(),
    )
}

fn array(items: Vec<Value>) -> Value {
    Value::Array(Array::from(items))
}

fn document_of(objects: &[(u32, Value)], trailer: Dict) -> Document {
    let mut base = MemoryBase::new(trailer);
    for (number, value) in objects {
        base.insert(Reference::new(*number, 0), bytes_of(value));
    }
    Document::new(Arc::new(base), None)
}

fn build(version: &Version, budget: &Budget) -> Result<References> {
    References::build(&mut Reads::new(version, budget))
}

/// A value that may hold references, nested up to `depth` containers.
fn value(depth: u32) -> impl Strategy<Value = Value> {
    let leaf = prop_oneof![
        (-9..9i64).prop_map(Value::Integer),
        (1..=NUMBERS + 1, 0..2u16)
            .prop_map(|(number, generation)| Value::Reference(Reference::new(number, generation))),
    ];
    leaf.prop_recursive(depth, 24, 4, |inner| {
        prop_oneof![
            proptest::collection::vec(inner.clone(), 0..4).prop_map(array),
            proptest::collection::vec((0..4u8, inner), 0..4).prop_map(|entries| {
                Value::Dict(
                    entries
                        .into_iter()
                        .map(|(key, value)| (Name::from(format!("K{key}").as_str()), value))
                        .collect(),
                )
            }),
        ]
    })
}

fn trailer_value() -> impl Strategy<Value = Dict> {
    proptest::collection::vec((0..3u8, value(2)), 0..3).prop_map(|entries| {
        entries
            .into_iter()
            .map(|(key, value)| (Name::from(format!("T{key}").as_str()), value))
            .collect()
    })
}

#[derive(Clone, Debug)]
enum Edit {
    Write(Vec<(u32, Value)>),
    Free(u32),
    Trailer(Dict),
    Undo,
    Redo,
}

fn edit() -> impl Strategy<Value = Edit> {
    prop_oneof![
        4 => proptest::collection::vec((1..=NUMBERS + 1, value(3)), 1..4).prop_map(Edit::Write),
        1 => (1..=NUMBERS).prop_map(Edit::Free),
        1 => trailer_value().prop_map(Edit::Trailer),
        1 => Just(Edit::Undo),
        1 => Just(Edit::Redo),
    ]
}

proptest! {
    #[test]
    fn an_updated_index_equals_a_full_build(
        objects in proptest::collection::vec(value(3), NUMBERS as usize),
        trailer in trailer_value(),
        edits in proptest::collection::vec(edit(), 1..24),
    ) {
        let budget = Budget::default();
        let objects: Vec<(u32, Value)> = (1..).zip(objects).collect();
        let document = document_of(&objects, trailer);
        let cache = Cache::new();
        let mut version = document.current();
        let mut index = build(&version, &budget).unwrap();
        prop_assert_eq!(&*cache.get(&version, &budget).unwrap(), &index);
        for edit in edits {
            match edit {
                Edit::Write(values) => {
                    let mut transaction = document.begin();
                    for (number, value) in &values {
                        transaction.write(*number, value, &budget).unwrap();
                    }
                    transaction.commit(&budget).unwrap();
                }
                Edit::Free(number) => {
                    let mut transaction = document.begin();
                    transaction.free(number, &budget).unwrap();
                    transaction.commit(&budget).unwrap();
                }
                Edit::Trailer(trailer) => {
                    let mut transaction = document.begin();
                    transaction.set_trailer(trailer, &budget).unwrap();
                    transaction.commit(&budget).unwrap();
                }
                Edit::Undo => {
                    document.undo();
                }
                Edit::Redo => {
                    document.redo();
                }
            }
            let next = document.current();
            let changes = version.changes(&next, &budget).unwrap().unwrap();
            let mut reads = Reads::new(&next, &budget);
            let updated = index.update(&mut reads, &changes).unwrap();
            let read = reads.finish().unwrap();
            prop_assert_eq!(read.objects(), changes.objects());
            prop_assert_eq!(read.trailer(), changes.trailer());
            let built = build(&next, &budget).unwrap();
            prop_assert_eq!(&updated, &built);
            prop_assert_eq!(&*cache.get(&next, &budget).unwrap(), &built);
            for number in 0..=NUMBERS + 1 {
                for &referrer in built.referrers(number) {
                    prop_assert!(built.targets(referrer).contains(&number));
                }
            }
            index = updated;
            version = next;
        }
    }
}

/// A document with a page tree, annotations, an outline, a structure tree,
/// and name and number trees, numbered as the comments say.
fn tagged() -> Document {
    let objects = vec![
        // The catalog.
        (
            1,
            dict(vec![
                ("Type", name("Catalog")),
                ("Pages", reference(2)),
                ("Outlines", reference(6)),
                ("StructTreeRoot", reference(8)),
                ("Names", dict(vec![("Dests", reference(11))])),
            ]),
        ),
        // The page tree root, and its page.
        (
            2,
            dict(vec![
                ("Type", name("Pages")),
                ("Kids", array(vec![reference(3)])),
                ("Count", Value::Integer(1)),
            ]),
        ),
        (
            3,
            dict(vec![
                ("Type", name("Page")),
                ("Parent", reference(2)),
                ("Annots", array(vec![reference(4), reference(5)])),
                ("Contents", reference(13)),
                ("StructParents", Value::Integer(0)),
            ]),
        ),
        // A link to the page, and a highlight.
        (
            4,
            dict(vec![
                ("Type", name("Annot")),
                ("Subtype", name("Link")),
                ("Rect", array(vec![Value::Integer(0); 4])),
                (
                    "Dest",
                    array(vec![
                        reference(3),
                        name("XYZ"),
                        Value::Null,
                        Value::Null,
                        Value::Null,
                    ]),
                ),
            ]),
        ),
        (
            5,
            dict(vec![
                ("Subtype", name("Highlight")),
                ("Rect", array(vec![Value::Integer(0); 4])),
                ("P", reference(3)),
            ]),
        ),
        // The outline root and its item.
        (
            6,
            dict(vec![
                ("Type", name("Outlines")),
                ("First", reference(7)),
                ("Last", reference(7)),
            ]),
        ),
        (
            7,
            dict(vec![
                ("Title", string("One")),
                ("Parent", reference(6)),
                (
                    "A",
                    dict(vec![
                        ("S", name("GoTo")),
                        ("D", array(vec![reference(3), name("Fit")])),
                    ]),
                ),
            ]),
        ),
        // The structure tree root, an element, and the parent tree.
        (
            8,
            dict(vec![
                ("Type", name("StructTreeRoot")),
                ("K", reference(9)),
                ("ParentTree", reference(10)),
            ]),
        ),
        (
            9,
            dict(vec![
                ("Type", name("StructElem")),
                ("S", name("P")),
                ("P", reference(8)),
                ("Pg", reference(3)),
                (
                    "K",
                    array(vec![
                        Value::Integer(0),
                        dict(vec![
                            ("Type", name("OBJR")),
                            ("Pg", reference(3)),
                            ("Obj", reference(4)),
                        ]),
                    ]),
                ),
            ]),
        ),
        (
            10,
            dict(vec![(
                "Nums",
                array(vec![Value::Integer(0), array(vec![reference(9)])]),
            )]),
        ),
        // A name tree of destinations, with a kid.
        (11, dict(vec![("Kids", array(vec![reference(12)]))])),
        (
            12,
            dict(vec![
                ("Names", array(vec![string("a"), reference(3)])),
                ("Limits", array(vec![string("a"), string("a")])),
            ]),
        ),
        // The page's content, a stream whose dictionary refers to the page.
        (
            13,
            Value::Stream(Box::new(Stream::new(
                [(Name::from("Page"), reference(3))].into_iter().collect(),
                Bytes::new(Vec::new()),
            ))),
        ),
    ];
    let trailer = [(Name::from("Root"), reference(1))].into_iter().collect();
    document_of(&objects, trailer)
}

fn key(key: &str) -> Step {
    Step::Key(Name::from(key))
}

/// Returns the paths and roles of the references to `number` that
/// `referrer` holds.
fn found(version: &Version, referrer: Referrer, number: u32) -> Vec<(Vec<Step>, Option<Role>)> {
    let budget = Budget::default();
    let mut reads = Reads::new(version, &budget);
    find(&mut reads, referrer, number)
        .unwrap()
        .iter()
        .map(|found| (found.path.to_vec(), found.role))
        .collect()
}

#[test]
fn the_index_lists_referrers_and_targets() {
    let document = tagged();
    let version = document.current();
    let index = build(&version, &Budget::default()).unwrap();
    assert_eq!(index.referrers(3), &[2, 4, 5, 7, 9, 12, 13]);
    assert_eq!(index.targets(9), &[3, 4, 8]);
    assert_eq!(index.referrers(1), &[] as &[u32]);
    assert_eq!(index.trailer(), &[1]);
    assert_eq!(index.targets(99), &[] as &[u32]);
}

#[test]
fn find_names_the_path_and_role_of_each_reference() {
    let document = tagged();
    let version = document.current();
    let object = Referrer::Object;
    assert_eq!(
        found(&version, object(2), 3),
        [(vec![key("Kids"), Step::Index(0)], Some(Role::PageTreeKid))]
    );
    assert_eq!(
        found(&version, object(3), 2),
        [(vec![key("Parent")], Some(Role::PageTreeParent))]
    );
    assert_eq!(
        found(&version, object(3), 5),
        [(
            vec![key("Annots"), Step::Index(1)],
            Some(Role::PageAnnotation)
        )]
    );
    assert_eq!(
        found(&version, object(4), 3),
        [(vec![key("Dest"), Step::Index(0)], Some(Role::Destination))]
    );
    assert_eq!(
        found(&version, object(5), 3),
        [(vec![key("P")], Some(Role::AnnotationPage))]
    );
    assert_eq!(
        found(&version, object(6), 7),
        [
            (vec![key("Last")], Some(Role::OutlineLink)),
            (vec![key("First")], Some(Role::OutlineLink)),
        ]
    );
    assert_eq!(
        found(&version, object(7), 3),
        [(
            vec![key("A"), key("D"), Step::Index(0)],
            Some(Role::Destination)
        )]
    );
    assert_eq!(
        found(&version, object(7), 6),
        [(vec![key("Parent")], Some(Role::OutlineLink))]
    );
    assert_eq!(
        found(&version, object(8), 9),
        [(vec![key("K")], Some(Role::StructureKid))]
    );
    assert_eq!(
        found(&version, object(9), 8),
        [(vec![key("P")], Some(Role::StructureParent))]
    );
    assert_eq!(
        found(&version, object(9), 3),
        [
            (
                vec![key("K"), Step::Index(1), key("Pg")],
                Some(Role::StructurePage)
            ),
            (vec![key("Pg")], Some(Role::StructurePage)),
        ]
    );
    assert_eq!(
        found(&version, object(9), 4),
        [(
            vec![key("K"), Step::Index(1), key("Obj")],
            Some(Role::StructureObject)
        )]
    );
    assert_eq!(
        found(&version, object(10), 9),
        [(
            vec![key("Nums"), Step::Index(1), Step::Index(0)],
            Some(Role::NumberTreeValue)
        )]
    );
    assert_eq!(
        found(&version, object(11), 12),
        [(vec![key("Kids"), Step::Index(0)], Some(Role::TreeKid))]
    );
    assert_eq!(
        found(&version, object(12), 3),
        [(
            vec![key("Names"), Step::Index(1)],
            Some(Role::NameTreeValue)
        )]
    );
    assert_eq!(found(&version, object(13), 3), [(vec![key("Page")], None)]);
    assert_eq!(
        found(&version, Referrer::Trailer, 1),
        [(vec![key("Root")], None)]
    );
    assert_eq!(found(&version, object(1), 3), []);
    assert_eq!(found(&version, object(99), 3), []);
}

#[test]
fn the_walk_refuses_values_deeper_than_the_budget() {
    let nested = array(vec![array(vec![array(vec![reference(1)])])]);
    let document = document_of(&[(1, nested)], Dict::new());
    let version = document.current();
    let budget = Budget::default().with_limit(LimitCode::ObjectDepth, Some(2));
    let error = build(&version, &budget).unwrap_err();
    assert_eq!(
        error.limit().map(LimitExceeded::code),
        Some(LimitCode::ObjectDepth)
    );
    let budget = Budget::default().with_limit(LimitCode::ObjectDepth, Some(3));
    assert_eq!(build(&version, &budget).unwrap().referrers(1), &[1]);
}

#[test]
fn the_index_is_charged_to_the_budget() {
    let objects: Vec<(u32, Value)> = (1..=64)
        .map(|number| (number, array((1..=64).map(reference).collect())))
        .collect();
    let document = document_of(&objects, Dict::new());
    let version = document.current();
    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(16 * 1024));
    let error = build(&version, &budget).unwrap_err();
    assert_eq!(
        error.limit().map(LimitExceeded::code),
        Some(LimitCode::MemorySize)
    );
}

#[test]
fn the_cache_keeps_no_version_alive() {
    let document = tagged();
    let budget = Budget::default();
    let cache = Cache::new();
    let first = document.current();
    let count = Arc::strong_count(&first);
    let index = cache.get(&first, &budget).unwrap();
    assert_eq!(Arc::strong_count(&first), count);
    assert!(Arc::ptr_eq(&index, &cache.get(&first, &budget).unwrap()));

    let mut transaction = document.begin();
    transaction.write(5, &Value::Null, &budget).unwrap();
    let second = transaction.commit(&budget).unwrap();
    let index = cache.get(&second, &budget).unwrap();
    assert_eq!(index.referrers(3), &[2, 4, 7, 9, 12, 13]);

    // Another document's version is built, not updated.
    let other = tagged();
    let index = cache.get(&other.current(), &budget).unwrap();
    assert_eq!(index.referrers(3), &[2, 4, 5, 7, 9, 12, 13]);
}

/// Applies `rule` to every reference to object `number` in `document` that
/// `only` holds, or that any object holds, and returns the index of the
/// version that the edits make, with the objects that they free freed.
fn apply_everywhere(
    document: &Document,
    number: u32,
    only: Option<u32>,
    rule: Rule,
) -> Result<References> {
    let budget = Budget::default();
    let version = document.current();
    let index = build(&version, &budget)?;
    let mut reads = Reads::new(&version, &budget);
    let mut edits = Edits::new(&budget);
    for &referrer in index.referrers(number) {
        if only.is_some_and(|only| only != referrer) {
            continue;
        }
        for found in find(&mut reads, Referrer::Object(referrer), number)?.iter() {
            edits.apply(&mut reads, &index, referrer, &found.path, rule)?;
        }
    }
    let mut transaction = document.begin();
    edits.write(&mut transaction, &budget)?;
    let after = transaction.snapshot();
    let changes = version.changes(&after, &budget)?.unwrap();
    let updated = index.update(&mut Reads::new(&after, &budget), &changes)?;
    for &freed in unreferenced(&updated, &[], edits.orphans(), &budget)?.iter() {
        transaction.free(freed, &budget)?;
    }
    let committed = transaction.commit(&budget)?;
    build(&committed, &budget)
}

#[test]
fn each_rule_leaves_no_reference_to_the_object_that_leaves() {
    // The rules for references to the page, object 3, of the tagged document.
    for rule in [
        Rule::RemoveEntry,
        Rule::Unresolve,
        Rule::Retarget(Reference::new(1, 0)),
    ] {
        let index = apply_everywhere(&tagged(), 3, None, rule).unwrap();
        assert_eq!(index.referrers(3), &[] as &[u32], "{rule:?}");
    }
    // Removing the link, object 4, that leads to the page drops it from the
    // page's annotations, and frees it once the structure element no longer
    // refers to it.
    let document = tagged();
    let index = apply_everywhere(&document, 3, Some(4), Rule::RemoveReferrer).unwrap();
    assert_eq!(index.referrers(4), &[9]);
    assert_eq!(index.targets(3), &[2, 5, 13]);
    let budget = Budget::default();
    let mut transaction = document.begin();
    transaction.write(9, &Value::Null, &budget).unwrap();
    transaction.commit(&budget).unwrap();
    let index = apply_everywhere(&document, 3, Some(4), Rule::RemoveReferrer).unwrap();
    assert_eq!(index.referrers(4), &[] as &[u32]);
    assert!(!index.referrers(3).contains(&4));
    assert!(document.current().get(4, &budget).unwrap().is_none());
}

#[test]
fn removing_entries_keeps_the_positions_of_the_others() {
    let objects = vec![
        (
            1,
            array(vec![
                reference(3),
                Value::Integer(0),
                reference(3),
                reference(2),
                reference(3),
            ]),
        ),
        (2, Value::Integer(0)),
        (3, Value::Integer(0)),
    ];
    let document = document_of(&objects, Dict::new());
    apply_everywhere(&document, 3, None, Rule::RemoveEntry).unwrap();
    let budget = Budget::default();
    let value = document.current().get(1, &budget).unwrap().unwrap();
    assert_eq!(
        **value.value(),
        array(vec![Value::Integer(0), reference(2)])
    );

    // Removals from one array around an edit inside one of its items.
    let objects = vec![
        (
            1,
            array(vec![
                reference(3),
                reference(3),
                dict(vec![("A", reference(3)), ("B", reference(2))]),
                reference(3),
                reference(3),
            ]),
        ),
        (2, Value::Integer(0)),
        (3, Value::Integer(0)),
    ];
    let document = document_of(&objects, Dict::new());
    apply_everywhere(&document, 3, None, Rule::RemoveEntry).unwrap();
    let value = document.current().get(1, &budget).unwrap().unwrap();
    assert_eq!(
        **value.value(),
        array(vec![dict(vec![("B", reference(2))])])
    );
}

#[test]
fn removing_pages_refuses_navigation_that_aliases_a_page_before_any_edit() {
    let document = tagged();
    let budget = Budget::default();
    let mut transaction = document.begin();
    let mut catalog = match &**document.current().get(1, &budget).unwrap().unwrap().value() {
        Value::Dict(dict) => dict.clone(),
        _ => unreachable!(),
    };
    catalog.insert(Name::from("Outlines"), reference(3));
    transaction
        .write(1, &Value::Dict(catalog), &budget)
        .unwrap();
    let version = transaction.commit(&budget).unwrap();
    let index = build(&version, &budget).unwrap();
    let mut edits = Edits::new(&budget);
    let error = remove_pages(
        &mut Reads::new(&version, &budget),
        &index,
        &[3],
        &[],
        &mut edits,
    )
    .unwrap_err();
    assert!(error.to_string().contains("cannot alias a page"));
    assert!(edits.is_empty());
}

#[test]
fn unreferenced_frees_what_only_freed_objects_refer_to() {
    let objects = vec![
        (1, dict(vec![("Kids", array(vec![reference(2)]))])),
        // A page, its content, and a font that another page shares.
        (
            2,
            dict(vec![
                ("Contents", reference(3)),
                ("Font", reference(4)),
                ("Annots", array(vec![reference(6)])),
            ]),
        ),
        (3, Value::Integer(0)),
        (4, Value::Integer(0)),
        (5, dict(vec![("Font", reference(4))])),
        // An annotation that refers back to the page.
        (6, dict(vec![("P", reference(2))])),
    ];
    let trailer = [
        (Name::from("Root"), reference(1)),
        (Name::from("Other"), reference(5)),
    ]
    .into_iter()
    .collect();
    let document = document_of(&objects, trailer);
    let budget = Budget::default();
    let index = build(&document.current(), &budget).unwrap();
    let freed = unreferenced(&index, &[2], &[], &budget).unwrap();
    assert_eq!(&*freed, &[2, 3, 6]);
}

#[test]
fn unreferenced_frees_a_candidate_with_what_refers_only_to_it() {
    let objects = vec![
        (1, dict(vec![("K", array(vec![reference(4)]))])),
        // A removed element and its parent, which only the element names.
        (2, dict(vec![("P", reference(1)), ("K", reference(3))])),
        (3, dict(vec![("P", reference(2)), ("Pg", reference(6))])),
        // A removed element that an element that stays still names.
        (4, dict(vec![("K", reference(5))])),
        (5, dict(vec![("P", reference(4))])),
        (6, Value::Integer(0)),
    ];
    let trailer = [(Name::from("Root"), reference(1))].into_iter().collect();
    let document = document_of(&objects, trailer);
    let budget = Budget::default();
    let index = build(&document.current(), &budget).unwrap();
    let freed = unreferenced(&index, &[], &[3, 5], &budget).unwrap();
    assert_eq!(&*freed, &[2, 3, 6]);
}

/// One element of a generated tagged document: the position of its
/// parent among the elements before it (none for the root), its page, its
/// content, and the element that its `/Ref` names.
#[derive(Clone, Debug)]
struct Element {
    parent: Option<usize>,
    page: Option<usize>,
    content: Vec<Content>,
    refers: Option<usize>,
}

/// A content item of a generated element: a marked-content identifier on
/// its page, a marked-content reference to a page, or an object reference
/// to an annotation.
#[derive(Clone, Debug)]
enum Content {
    Mcid,
    Mcr(usize),
    Objr(usize),
}

fn content() -> impl Strategy<Value = Content> {
    prop_oneof![
        Just(Content::Mcid),
        (0..8usize).prop_map(Content::Mcr),
        (0..8usize).prop_map(Content::Objr),
    ]
}

fn elements() -> impl Strategy<Value = Vec<Element>> {
    proptest::collection::vec(
        (
            proptest::option::of(0..16usize),
            proptest::option::of(0..8usize),
            proptest::collection::vec(content(), 0..3),
            proptest::option::of(0..16usize),
        ),
        1..12,
    )
    .prop_map(|raw| {
        raw.into_iter()
            .enumerate()
            .map(|(at, (parent, page, content, refers))| Element {
                parent: parent.filter(|_| at > 0).map(|parent| parent % at),
                page,
                content,
                refers,
            })
            .collect()
    })
}

/// Returns a tagged document of `pages` pages, the annotations whose pages
/// `annotations` gives, and `elements`, with each element's `/P` naming
/// the parent whose `/K` holds it. Pages are objects 10 and on,
/// annotations 30 and on, elements 50 and on. The parent tree lists the
/// annotations that an element holds, and is one dictionary, or when
/// `leaf` is given, object 69 whose kids are leaves of `leaf` pairs at
/// most, objects 70 and on.
fn tagged_of(
    pages: usize,
    annotations: &[usize],
    elements: &[Element],
    leaf: Option<usize>,
) -> Document {
    let page = |at: usize| u32::try_from(10 + at % pages).unwrap();
    let annotation = |at: usize| u32::try_from(30 + at % annotations.len()).unwrap();
    let element = |at: usize| u32::try_from(50 + at).unwrap();
    let mut objects = vec![
        (
            1,
            dict(vec![
                ("Type", name("Catalog")),
                ("Pages", reference(2)),
                ("StructTreeRoot", reference(3)),
            ]),
        ),
        (
            2,
            dict(vec![
                ("Type", name("Pages")),
                (
                    "Kids",
                    array((0..pages).map(|at| reference(page(at))).collect()),
                ),
                ("Count", Value::Integer(i64::try_from(pages).unwrap())),
            ]),
        ),
    ];
    let mut kids = vec![Vec::new(); elements.len()];
    let mut top = Vec::new();
    let mut on_page = vec![Vec::new(); pages];
    let mut holder = vec![None; annotations.len()];
    let mut mcid = 0;
    for (at, spec) in elements.iter().enumerate() {
        match spec.parent {
            Some(parent) => kids[parent].push(reference(element(at))),
            None => top.push(reference(element(at))),
        }
        for item in &spec.content {
            mcid += 1;
            let item = match item {
                Content::Mcid => Value::Integer(mcid),
                Content::Mcr(target) => {
                    on_page[*target % pages].push(reference(element(at)));
                    dict(vec![
                        ("Type", name("MCR")),
                        ("Pg", reference(page(*target))),
                        ("MCID", Value::Integer(mcid)),
                    ])
                }
                Content::Objr(_) if annotations.is_empty() => continue,
                Content::Objr(target) => {
                    holder[*target % annotations.len()] = Some(element(at));
                    dict(vec![
                        ("Type", name("OBJR")),
                        ("Obj", reference(annotation(*target))),
                    ])
                }
            };
            kids[at].push(item);
        }
        if let Some(target) = spec.page
            && spec
                .content
                .iter()
                .any(|item| matches!(item, Content::Mcid))
        {
            on_page[target % pages].push(reference(element(at)));
        }
    }
    for at in 0..pages {
        let listed: Vec<Value> = (0..annotations.len())
            .filter(|&listed| annotations[listed] % pages == at)
            .map(|listed| reference(annotation(listed)))
            .collect();
        let mut entries = vec![
            ("Type", name("Page")),
            ("Parent", reference(2)),
            ("StructParents", Value::Integer(i64::try_from(at).unwrap())),
        ];
        if !listed.is_empty() {
            entries.push(("Annots", array(listed)));
        }
        objects.push((page(at), dict(entries)));
    }
    let mut pairs = Vec::new();
    for (at, elements) in on_page.into_iter().enumerate() {
        pairs.push((i64::try_from(at).unwrap(), array(elements)));
    }
    for (at, holder) in holder.into_iter().enumerate() {
        if let Some(holder) = holder {
            pairs.push((i64::try_from(100 + at).unwrap(), reference(holder)));
        }
    }
    let nums = |pairs: &[(i64, Value)]| {
        array(
            pairs
                .iter()
                .flat_map(|(key, value)| [Value::Integer(*key), value.clone()])
                .collect(),
        )
    };
    let parent_tree = match leaf {
        None => dict(vec![("Nums", nums(&pairs))]),
        Some(leaf) => {
            let mut kids = Vec::new();
            for (at, chunk) in pairs.chunks(leaf).enumerate() {
                let number = 70 + u32::try_from(at).unwrap();
                let limits = vec![
                    Value::Integer(chunk[0].0),
                    Value::Integer(chunk[chunk.len() - 1].0),
                ];
                objects.push((
                    number,
                    dict(vec![("Limits", array(limits)), ("Nums", nums(chunk))]),
                ));
                kids.push(reference(number));
            }
            objects.push((69, dict(vec![("Kids", array(kids))])));
            reference(69)
        }
    };
    for (at, &on) in annotations.iter().enumerate() {
        objects.push((
            annotation(at),
            dict(vec![
                ("Type", name("Annot")),
                ("Subtype", name("Square")),
                ("P", reference(page(on))),
                (
                    "StructParent",
                    Value::Integer(i64::try_from(100 + at).unwrap()),
                ),
            ]),
        ));
    }
    objects.push((
        3,
        dict(vec![
            ("Type", name("StructTreeRoot")),
            ("K", array(top)),
            ("ParentTree", parent_tree),
        ]),
    ));
    for (at, (spec, kids)) in elements.iter().zip(kids).enumerate() {
        let parent = spec.parent.map_or(3, element);
        let mut entries = vec![
            ("Type", name("StructElem")),
            ("S", name("P")),
            ("P", reference(parent)),
            ("K", array(kids)),
        ];
        if let Some(target) = spec.page {
            entries.push(("Pg", reference(page(target))));
        }
        if let Some(target) = spec.refers {
            entries.push((
                "Ref",
                array(vec![reference(element(target % elements.len()))]),
            ));
        }
        objects.push((element(at), dict(entries)));
    }
    let Value::Dict(trailer) = dict(vec![("Root", reference(1))]) else {
        unreachable!()
    };
    document_of(&objects, trailer)
}

/// Plans the rules for removing the pages `removed` of `document` with the
/// structure rules of `reach`, and returns the reach that planned them,
/// the values of the objects that the edits leave, and the orphans.
fn planned_with(
    document: &Document,
    removed: &[u32],
    retained: &[u32],
    reach: structure::Reach,
) -> (structure::Reach, Vec<Option<Value>>, Vec<u32>) {
    let budget = Budget::default();
    let version = document.current();
    let index = build(&version, &budget).unwrap();
    let mut reads = Reads::new(&version, &budget);
    let mut edits = Edits::new(&budget);
    navigation::plan(&mut reads, &index, removed, retained, &mut edits).unwrap();
    let used = structure::plan(&mut reads, &index, removed, retained, &mut edits, reach).unwrap();
    let orphans = edits.orphans().to_vec();
    let mut transaction = document.begin();
    edits.write(&mut transaction, &budget).unwrap();
    let after = transaction.snapshot();
    let values = (1..80)
        .map(|number| {
            after
                .get(number, &budget)
                .unwrap()
                .map(|object| (**object.value()).clone())
        })
        .collect();
    (used, values, orphans)
}

proptest! {
    #[test]
    fn the_affected_walk_plans_what_the_whole_walk_plans(
        pages in 1..5usize,
        annotations in proptest::collection::vec(0..8usize, 0..4),
        elements in elements(),
        removed in proptest::collection::vec(any::<bool>(), 4),
        leaf in proptest::option::of(1..4usize),
    ) {
        let document = tagged_of(pages, &annotations, &elements, leaf);
        let page = |at: usize| u32::try_from(10 + at).unwrap();
        // The first page leaves when no other does.
        let leaves = |at: usize| removed[at] || (at == 0 && !removed[..pages].contains(&true));
        let removed_pages: Vec<u32> = (0..pages).filter(|&at| leaves(at)).map(page).collect();
        let retained: Vec<u32> = (0..pages).filter(|&at| !leaves(at)).map(page).collect();
        let affected = planned_with(&document, &removed_pages, &retained, structure::Reach::Affected);
        let whole = planned_with(&document, &removed_pages, &retained, structure::Reach::Whole);
        prop_assert_eq!(affected.0, structure::Reach::Affected);
        prop_assert_eq!(&affected.1, &whole.1);
        prop_assert_eq!(&affected.2, &whole.2);
    }
}
