use crate::{COMMIT, Condition, Kind, OBJECTS, Since, Version, object};

#[test]
fn objects_are_sorted_and_unique() {
    assert!(OBJECTS.windows(2).all(|pair| pair[0].name < pair[1].name));
    assert_eq!(COMMIT.len(), 40);
}

#[test]
fn every_object_is_found_by_name() {
    for (at, described) in OBJECTS.iter().enumerate() {
        let found = object(described.name).expect("a described object is found");
        assert!(std::ptr::eq(found, &OBJECTS[at]));
    }
    assert!(object("NoSuchObject").is_none());
}

#[test]
fn every_key_has_a_type_and_every_link_an_object() {
    for described in &OBJECTS {
        assert!(!described.keys.is_empty(), "{}", described.name);
        for key in described.keys {
            assert!(!key.types.is_empty(), "{}/{}", described.name, key.name);
            for typed in key.types {
                for link in typed.links {
                    assert!(usize::from(link.object) < OBJECTS.len());
                }
            }
        }
    }
}

#[test]
fn the_catalog_requires_an_indirect_page_tree_root() {
    let pages = object("Catalog")
        .and_then(|catalog| catalog.key("Pages"))
        .expect("Catalog/Pages");

    assert_eq!(pages.required, Condition::Always);
    assert_eq!(pages.since, Since::Version(Version { major: 1, minor: 0 }));
    let [typed] = pages.types else {
        panic!("one type")
    };
    assert_eq!(typed.kind, Kind::Dictionary);
    assert_eq!(typed.indirect, Condition::Always);
    let [link] = typed.links else {
        panic!("one link")
    };
    assert_eq!(link.target().name, "PageTreeNodeRoot");
    assert_eq!(link.condition, None);
}

#[test]
fn a_page_inherits_its_rotation() {
    let rotate = object("PageObject")
        .and_then(|page| page.key("Rotate"))
        .expect("PageObject/Rotate");

    assert!(rotate.inheritable);
    assert_eq!(rotate.required, Condition::Never);
    assert_eq!(rotate.types[0].kind, Kind::Integer);
    assert_eq!(rotate.types[0].default, Some("0"));
}

#[test]
fn predicates_are_kept_as_the_model_writes_them() {
    let caret = object("AnnotCaret").expect("AnnotCaret");
    let wrapped = caret
        .keys
        .iter()
        .flat_map(|key| key.types)
        .flat_map(|typed| typed.links)
        .find_map(|link| link.condition.filter(|text| text.contains("fn:Extension")))
        .expect("a link admitted by an extension");

    assert_eq!(
        wrapped,
        "fn:IsPDFVersion(1.7,fn:Extension(ADBE_Extn3,ExDataMarkupGeo))"
    );
}
