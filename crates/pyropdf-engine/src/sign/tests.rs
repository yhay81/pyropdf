//! The placeholder, the in-place fill, and the CMS structure of a signature,
//! with a self-signed P-256 test certificate. pyHanko validates signatures
//! in tests/test_signing.py.

use der::Decode;

use super::*;

/// A P-256 key for these tests only.
const KEY: &str = "\
-----BEGIN PRIVATE KEY-----\n\
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgrGOmEQvQqZhsokpO\n\
a6vhld/9iiEBey+OzO54OFFCgYChRANCAAQwKjdr5MXfJt4aG9QAMpiS305hzx3a\n\
SWHeK/JTnrAtbWbyT0Ua7Fn4fPxMkouwDkXTLsvVeGUpL5aG8yv/gsYb\n\
-----END PRIVATE KEY-----\n\
";

/// A self-signed certificate of [`KEY`].
const CERTIFICATE: &str = "\
-----BEGIN CERTIFICATE-----\n\
MIIBcjCCARigAwIBAgIUWR+ZwwxXzUdQLE4sfBdNzR6heKIwCgYIKoZIzj0EAwIw\n\
HjEcMBoGA1UEAwwTcHlyb3BkZiBlbmdpbmUgdGVzdDAgFw0yNjEwMzAwMzAxNDJa\n\
GA8yMTI2MTAwNjAzMDE0MlowHjEcMBoGA1UEAwwTcHlyb3BkZiBlbmdpbmUgdGVz\n\
dDBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABDAqN2vkxd8m3hob1AAymJLfTmHP\n\
HdpJYd4r8lOesC1tZvJPRRrsWfh8/EySi7AORdMuy9V4ZSkvlobzK/+CxhujMjAw\n\
MB0GA1UdDgQWBBSuIA+1yJwIdhLdRE2sYBdkpUojMzAPBgNVHRMBAf8EBTADAQH/\n\
MAoGCCqGSM49BAMCA0gAMEUCIQDSIcdHpMKMIDrk0qfTqMexSDIy7TxQE5QfABAS\n\
Q3qP0gIgSwlnSQSZQMElz+qE9PTvGPmJdCkDSrIdq9T4oasMwT8=\n\
-----END CERTIFICATE-----\n\
";

const RESERVE: usize = 64;

/// A saved document's bytes around a placeholder of `reserve` bytes.
fn document(reserve: usize) -> Vec<u8> {
    let mut data = b"%PDF-1.7\n1 0 obj\n<</Type /Sig ".to_vec();
    data.extend_from_slice(&placeholder_prefix());
    data.extend(std::iter::repeat_n(b'0', 2 * reserve));
    data.extend_from_slice(b")>>\nendobj\n%%EOF\n");
    data
}

fn chain() -> Chain {
    Chain::from_pem(CERTIFICATE.as_bytes()).expect("the test certificate")
}

fn key() -> Key {
    Key::from_pem(KEY.as_bytes(), None).expect("the test key")
}

type Stamp = fn(&[u8], DigestAlgorithm) -> Result<Vec<u8>, ()>;

#[test]
fn the_placeholder_is_found_once() {
    let data = document(RESERVE);
    let placeholder = Placeholder::find(&data, RESERVE).expect("one placeholder");
    assert_eq!(data[placeholder.contents.start], b'(');
    assert_eq!(data[placeholder.contents.end - 1], b')');
    assert_eq!(
        &data[placeholder.byte_range.clone()],
        b"[0 9999999999 9999999999 9999999999]"
    );
    // Another reserve, or none, is no placeholder.
    assert!(Placeholder::find(&data, RESERVE + 1).is_err());
    assert!(Placeholder::find(b"%PDF-1.7", RESERVE).is_err());
    let mut twice = data.clone();
    twice.extend_from_slice(&data);
    assert!(matches!(
        Placeholder::find(&twice, RESERVE),
        Err(Error::Placeholder(message)) if message.contains("more than one")
    ));
}

#[test]
fn signing_fills_the_placeholder_in_place() {
    let reserve = 2048;
    let mut data = document(reserve);
    let length = data.len();
    let contents = Placeholder::find(&data, reserve)
        .expect("a placeholder")
        .contents;
    let key = key();
    sign(
        &mut data,
        reserve,
        &chain(),
        |attributes| Ok::<_, ()>(key.sign(attributes)),
        None::<Stamp>,
    )
    .expect("a signature");
    assert_eq!(data.len(), length);
    let text = String::from_utf8(data.clone()).expect("ASCII");
    let expected = format!(
        "/ByteRange [0 {} {} {}",
        contents.start,
        contents.end,
        length - contents.end
    );
    assert!(text.contains(&expected), "{text}");
    assert_eq!(data[contents.start], b'<');
    assert_eq!(data[contents.end - 1], b'>');
    let hex = &text[contents.start + 1..contents.end - 1];
    let der: Vec<u8> = (0..hex.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&hex[at..at + 2], 16))
        .collect::<Result<_, _>>()
        .expect("hex");
    // The signature's DER, followed by the zeros of the reserve.
    let mut reader = der::SliceReader::new(&der).expect("a reader");
    let info = ::cms::content_info::ContentInfo::decode(&mut reader).expect("a ContentInfo");
    let signed = info
        .content
        .decode_as::<::cms::signed_data::SignedData>()
        .expect("SignedData");
    let signer = signed.signer_infos.0.iter().next().expect("one signer");
    assert!(signer.unsigned_attrs.is_none());
    let attributes = signer.signed_attrs.as_ref().expect("signed attributes");
    assert_eq!(attributes.len(), 3);
}

#[test]
fn a_small_reserve_is_refused() {
    let mut data = document(RESERVE);
    let key = key();
    let result = sign(
        &mut data,
        RESERVE,
        &chain(),
        |attributes| Ok::<_, ()>(key.sign(attributes)),
        None::<Stamp>,
    );
    assert!(matches!(
        result,
        Err(Failure::Sign(Error::Reserve {
            reserved: RESERVE,
            ..
        }))
    ));
}

#[test]
fn a_callback_error_passes_through() {
    let mut data = document(2048);
    let result = sign(
        &mut data,
        2048,
        &chain(),
        |_| Err("no key"),
        None::<fn(&[u8], DigestAlgorithm) -> Result<Vec<u8>, &'static str>>,
    );
    assert!(matches!(result, Err(Failure::Callback("no key"))));
}

#[test]
fn a_token_must_stamp_the_signature() {
    let mut data = document(2048);
    let key = key();
    let result = sign(
        &mut data,
        2048,
        &chain(),
        |attributes| Ok::<_, ()>(key.sign(attributes)),
        Some(|_: &[u8], _| Ok(vec![0x30, 0x00])),
    );
    assert!(matches!(result, Err(Failure::Sign(Error::Invalid(_)))));
}

#[test]
fn the_key_matches_its_certificate() {
    let chain = chain();
    assert!(key().matches(chain.signer()));
    assert_eq!(chain.algorithm(), SignatureAlgorithm::EcdsaP256);
    let other = Key::P256(p256::ecdsa::SigningKey::from_slice(&[7; 32]).expect("a scalar"));
    assert!(!other.matches(chain.signer()));
}

#[test]
fn keys_that_pyropdf_does_not_sign_with_are_refused() {
    assert!(Key::from_pem(CERTIFICATE.as_bytes(), None).is_err());
    assert!(Key::from_pem(b"not PEM", None).is_err());
    assert!(Chain::from_der(&[]).is_err());
}
