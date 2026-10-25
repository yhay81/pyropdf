//! Tests of the security handler: pylopdf's encrypted fixtures open with
//! the right passwords and refuse wrong ones, saves decrypt, keep the
//! encryption with the original encrypted bytes, or encrypt anew with
//! AES-128 and AES-256, which qpdf reads when it is installed.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;
use std::{env, fs};

use super::*;
use crate::ErrorKind;
use crate::save::{self, FileBase};
use crate::value::Bytes;
use crate::version::{Document, Version};

/// pylopdf's fixtures, and the handler that each uses (tests/test_encrypted.py
/// at 884af0db).
const HANDLERS: [(&str, &str); 8] = [
    ("user-rc4-40.pdf", "Standard V1 R2 40-bit RC4"),
    ("user-rc4-128.pdf", "Standard V2 R3 128-bit RC4"),
    ("user-r4-rc4-128.pdf", "Standard V4 R4 128-bit RC4"),
    ("user-aes-128.pdf", "Standard V4 R4 128-bit AES"),
    (
        "user-aes-128-plain-metadata.pdf",
        "Standard V4 R4 128-bit AES",
    ),
    ("user-aes-256-r5.pdf", "Standard V5 R5 256-bit AES"),
    ("user-aes-256.pdf", "Standard V5 R6 256-bit AES"),
    (
        "user-aes-256-object-streams.pdf",
        "Standard V5 R6 256-bit AES",
    ),
];

fn fixture(name: &str) -> Vec<u8> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/encrypted")
        .join(name);
    fs::read(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display()))
}

fn open(bytes: &[u8], password: Option<&str>) -> Result<Document> {
    let budget = Budget::default();
    let base = FileBase::open_with_password(
        Bytes::new(bytes.to_vec()),
        password.map(str::as_bytes),
        &budget,
    )?;
    Ok(Document::new(Arc::new(base), None))
}

fn security(document: &Document) -> Option<Handler> {
    document.current().base().file()?.security().cloned()
}

/// Returns the version with `/Encrypt` and `/ID` gone from its trailer, so
/// that a full save writes it decrypted.
fn without_encryption(document: &Document) -> Arc<Version> {
    let budget = Budget::default();
    let mut trailer = document.current().trailer().clone();
    trailer.remove("Encrypt");
    let mut transaction = document.begin();
    transaction.set_trailer(trailer, &budget).unwrap();
    transaction.commit(&budget).unwrap()
}

fn full(version: &Version) -> Vec<u8> {
    let mut out = Vec::new();
    save::full(version, &mut out, &Budget::default()).unwrap();
    out
}

/// Returns the content of each page, as hayro reads a plain file.
fn page_texts(plain: &[u8]) -> Vec<String> {
    let pdf = hayro_syntax::Pdf::new(plain.to_vec()).unwrap();
    pdf.pages()
        .iter()
        .map(|page| String::from_utf8_lossy(page.page_stream().unwrap_or_default()).into_owned())
        .collect()
}

fn assert_plain_text(plain: &[u8], name: &str) {
    let reopened = open(plain, None).unwrap();
    assert!(
        reopened.current().trailer().get("Encrypt").is_none(),
        "{name}"
    );
    let texts = page_texts(plain);
    assert_eq!(texts.len(), 2, "{name}");
    assert!(
        texts[0].contains("Encrypted page one"),
        "{name}: {}",
        texts[0]
    );
    assert!(
        texts[1].contains("Encrypted page two"),
        "{name}: {}",
        texts[1]
    );
}

#[test]
fn fixtures_open_with_either_password_and_refuse_wrong_ones() {
    for (name, description) in HANDLERS {
        let bytes = fixture(name);
        let user = open(&bytes, Some("userpw")).unwrap();
        let handler = security(&user).unwrap();
        assert_eq!(handler.access(), Access::User, "{name}");
        assert_eq!(handler.description(), description, "{name}");
        let owner = open(&bytes, Some("ownerpw")).unwrap();
        assert_eq!(security(&owner).unwrap().access(), Access::Owner, "{name}");
        let wrong = open(&bytes, Some("wrong")).map(|_| ()).unwrap_err();
        assert_eq!(
            wrong.kind(),
            &ErrorKind::Password(Password::Incorrect),
            "{name}"
        );
        // Without a password, the document opens locked.
        let locked = open(&bytes, None).unwrap();
        assert!(security(&locked).is_none(), "{name}");
        let root = match locked.current().trailer().get("Root") {
            Some(Value::Reference(root)) => root.number(),
            _ => panic!("{name}"),
        };
        let refused = locked.current().get(root, &Budget::default()).unwrap_err();
        assert_eq!(
            refused.kind(),
            &ErrorKind::Password(Password::Required),
            "{name}"
        );
        // Both passwords decrypt the same document.
        assert_plain_text(&full(&without_encryption(&user)), name);
        assert_plain_text(&full(&without_encryption(&owner)), name);
    }
}

#[test]
fn an_empty_user_password_opens_transparently() {
    let bytes = fixture("owneronly-aes-256.pdf");
    let document = open(&bytes, None).unwrap();
    assert_eq!(security(&document).unwrap().access(), Access::User);
    assert_plain_text(&full(&without_encryption(&document)), "owneronly");
    let owner = open(&bytes, Some("ownerpw")).unwrap();
    assert_eq!(security(&owner).unwrap().access(), Access::Owner);
}

#[test]
fn plain_metadata_stays_plain() {
    let bytes = fixture("user-aes-128-plain-metadata.pdf");
    let handler = security(&open(&bytes, Some("userpw")).unwrap()).unwrap();
    assert!(!handler.encrypts_metadata());
}

#[test]
fn passwords_are_bounded() {
    let bytes = fixture("user-aes-256.pdf");
    let long = "x".repeat(MAX_PASSWORD + 1);
    let refused = open(&bytes, Some(&long)).map(|_| ()).unwrap_err();
    assert_eq!(
        refused.limit().unwrap().code(),
        LimitCode::PasswordInputSize
    );
    let wrong = open(&bytes, Some(&"x".repeat(MAX_PASSWORD)))
        .map(|_| ())
        .unwrap_err();
    assert_eq!(wrong.kind(), &ErrorKind::Password(Password::Incorrect));
}

/// Returns the bytes of an object of the file, as they are in it.
fn original(document: &Document, number: u32) -> Bytes {
    let version = document.current();
    let index = version.base().file().unwrap();
    let generation = index.get(number).unwrap().generation();
    let bytes = index
        .object(Reference::new(number, generation), &Budget::default())
        .unwrap()
        .unwrap();
    let end = bytes.windows(6).position(|w| w == b"endobj").unwrap();
    bytes.slice(0..end).unwrap()
}

#[test]
fn a_save_that_keeps_the_encryption_copies_untouched_encrypted_bytes() {
    let budget = Budget::default();
    for (name, _) in HANDLERS {
        let bytes = fixture(name);
        let document = open(&bytes, Some("userpw")).unwrap();
        let version = document.current();
        let index = version.base().file().unwrap();
        // Every uncompressed object but one, which the edit rewrites.
        let offsets: Vec<u32> = index
            .entries()
            .filter(|(_, e)| matches!(e.location(), crate::index::Location::Offset(_)))
            .map(|(n, _)| n)
            .collect();
        let edited_number = version.next_number() as u32;
        let mut transaction = document.begin();
        let text = Value::String(PdfString::literal(&b"a new secret"[..]));
        transaction.write(edited_number, &text, &budget).unwrap();
        let edited = transaction.commit(&budget).unwrap();
        for incremental in [false, true] {
            let mut out = Vec::new();
            if incremental {
                save::incremental(&edited, &mut out, &budget).unwrap();
            } else {
                save::full(&edited, &mut out, &budget).unwrap();
            }
            for &number in &offsets {
                let raw = original(&document, number);
                let raw = raw.as_slice();
                let trimmed = &raw[..raw
                    .iter()
                    .rposition(|b| !b.is_ascii_whitespace())
                    .map_or(0, |i| i + 1)];
                assert!(
                    out.windows(trimmed.len()).any(|w| w == trimmed),
                    "{name} object {number}"
                );
            }
            assert!(!out.windows(12).any(|w| w == b"a new secret"), "{name}");
            // The output opens with the same password and reads the edit.
            let reopened = open(&out, Some("userpw")).unwrap();
            let value = reopened
                .current()
                .get(edited_number, &budget)
                .unwrap()
                .unwrap();
            assert_eq!(**value.value(), text, "{name} incremental={incremental}");
            assert_plain_text(&full(&without_encryption(&reopened)), name);
        }
    }
}

fn encryption(aes256: bool) -> Encryption {
    Encryption {
        user: b"userpw".to_vec(),
        owner: b"ownerpw".to_vec(),
        permissions: 0b1111_0000_1100,
        aes256,
        random: core::array::from_fn(|i| (i * 37 + 11) as u8),
    }
}

fn qpdf() -> Option<PathBuf> {
    let path = env::var_os("QPDF").map_or_else(|| PathBuf::from("qpdf"), PathBuf::from);
    Command::new(&path)
        .arg("--version")
        .output()
        .ok()?
        .status
        .success()
        .then_some(path)
}

#[test]
fn new_aes_encryption_reads_back_and_in_qpdf() {
    let budget = Budget::default();
    let plain = full(&without_encryption(
        &open(&fixture("user-aes-256.pdf"), Some("userpw")).unwrap(),
    ));
    let document = open(&plain, None).unwrap();
    for aes256 in [false, true] {
        let mut out = Vec::new();
        save::full_encrypted(&document.current(), &mut out, &budget, &encryption(aes256)).unwrap();
        assert!(!out.windows(18).any(|w| w == b"Encrypted page one"));
        let user = open(&out, Some("userpw")).unwrap();
        let handler = security(&user).unwrap();
        assert_eq!(handler.access(), Access::User);
        let expected = if aes256 {
            "Standard V5 R6 256-bit AES"
        } else {
            "Standard V4 R4 128-bit AES"
        };
        assert_eq!(handler.description(), expected);
        assert_eq!(handler.permissions() & 0b1111_0000_1100, 0b1111_0000_1100);
        assert_eq!(
            security(&open(&out, Some("ownerpw")).unwrap())
                .unwrap()
                .access(),
            Access::Owner
        );
        assert!(open(&out, Some("wrong")).is_err());
        assert_plain_text(&full(&without_encryption(&user)), expected);
        // The same input encrypts to the same output.
        let mut again = Vec::new();
        save::full_encrypted(
            &document.current(),
            &mut again,
            &budget,
            &encryption(aes256),
        )
        .unwrap();
        assert_eq!(out, again);
        let Some(qpdf) = qpdf() else {
            eprintln!("qpdf is not installed; skipping its check");
            continue;
        };
        let dir = env::temp_dir().join(format!("pyropdf-security-{}-{aes256}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let (input, output) = (dir.join("in.pdf"), dir.join("out.pdf"));
        fs::write(&input, &out).unwrap();
        let status = Command::new(&qpdf)
            .args(["--password=userpw", "--decrypt"])
            .arg(&input)
            .arg(&output)
            .status()
            .unwrap();
        // qpdf exits with 3 for warnings, which a failed decryption also is.
        assert_eq!(status.code(), Some(0), "qpdf decrypts {expected}");
        assert_plain_text(&fs::read(&output).unwrap(), expected);
        fs::remove_dir_all(&dir).unwrap();
    }
}

#[test]
fn a_locked_document_refuses_saves_that_need_its_key() {
    let bytes = fixture("user-aes-256.pdf");
    let locked = open(&bytes, None).unwrap();
    let version = locked.current();
    let mut out = Vec::new();
    assert!(matches!(
        save::full(&version, &mut out, &Budget::default()),
        Err(save::Error::Refused(save::Refusal::Encryption))
    ));
    assert!(matches!(
        save::incremental(&version, &mut out, &Budget::default()),
        Err(save::Error::Refused(save::Refusal::Encryption))
    ));
    // Decrypting needs the key too.
    let mut out = Vec::new();
    let refused = save::full(&without_encryption(&locked), &mut out, &Budget::default());
    assert!(matches!(
        refused,
        Err(save::Error::Store(ref e)) if e.kind() == &ErrorKind::Password(Password::Required)
    ));
}

#[test]
fn rc4_and_aes_match_known_vectors() {
    let budget = Budget::default();
    // RFC 6229 is keyed by length; the classic "Key"/"Plaintext" vector.
    assert_eq!(
        crypto::rc4(b"Key", b"Plaintext", &budget).unwrap(),
        [0xBB, 0xF3, 0x16, 0xE8, 0xD9, 0x40, 0xAF, 0x0A, 0xD3]
    );
    // FIPS-197 appendix C.1, as one CBC block from a zero vector.
    let key: [u8; 16] = core::array::from_fn(|i| i as u8);
    let plain: [u8; 16] = core::array::from_fn(|i| (i * 0x11) as u8);
    let cipher = crypto::aes_cbc_encrypt(&key, &[0; 16], &plain, false, &budget).unwrap();
    assert_eq!(
        cipher,
        [
            0x69, 0xC4, 0xE0, 0xD8, 0x6A, 0x7B, 0x04, 0x30, 0xD8, 0xCD, 0xB7, 0x80, 0x70, 0xB4,
            0xC5, 0x5A
        ]
    );
    for len in [0, 1, 15, 16, 17, 40] {
        let data: Vec<u8> = (0..len as u8).collect();
        let iv = [7; 16];
        let sealed = crypto::aes_cbc_encrypt(&key, &iv, &data, true, &budget).unwrap();
        assert_eq!(sealed.len() % 16, 0);
        assert_eq!(
            crypto::aes_cbc_decrypt(&key, &iv, &sealed, true, &budget).unwrap(),
            data
        );
    }
}

#[test]
fn the_base_reads_decrypted_values_and_the_encrypt_dictionary_as_it_is() {
    let bytes = fixture("user-rc4-128.pdf");
    let document = open(&bytes, Some("userpw")).unwrap();
    let version = document.current();
    let Some(Value::Reference(encrypt)) = version.trailer().get("Encrypt") else {
        panic!("the fixture's /Encrypt is indirect");
    };
    let raw = version
        .base()
        .file()
        .unwrap()
        .object(*encrypt, &Budget::default())
        .unwrap()
        .unwrap();
    let read = version
        .base()
        .object(*encrypt, &Budget::default())
        .unwrap()
        .unwrap();
    assert_eq!(raw.as_slice(), read.as_slice());
}
