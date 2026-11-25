//! Encryption, crypt filters, AES-GCM, Document MAC (ISO/TS 32004:2024), and
//! public-key recipient envelopes (DESIGN.md section 4.5, F6, F9).
//!
//! Inputs starting with `%PDF-` are opened with empty, fixture, and derived
//! passwords as well as a deterministic recipient callback, verifying Document
//! MACs, reading decrypted objects, opening historical revisions, and
//! round-tripping encrypted full saves. Other inputs fuzz `/Encrypt`
//! dictionaries, legacy CMS RC4/RC2 content ciphers, AES-128/AES-256/AES-GCM
//! object ciphers, and standalone/attached Document MAC DER tokens directly.

#![no_main]

use std::sync::{Arc, OnceLock};
use std::time::Duration;

use libfuzzer_sys::fuzz_target;
use pyropdf_store::budget::{Budget, DocumentLimits};
use pyropdf_store::save::{self, FileBase, Options};
use pyropdf_store::security::{
    self, Encryption, Handler, Kind, MAX_PASSWORD, Method, PasswordEncryption, RecipientEncryption,
    RecipientPayload, integrity,
};
use pyropdf_store::value::{Bytes, Reference, Value};
use pyropdf_store::version::Document;

/// Maximum objects decrypted per opened PDF to keep iterations fast.
const MAX_OBJECTS: usize = 48;
/// Maximum input length for full encrypted save round-trips.
const MAX_RESAVE_INPUT: usize = 32 * 1024;
/// Maximum raw token size wrapped into standalone/attached MAC templates.
const MAX_MAC_TOKEN: usize = 16 * 1024;

struct CachedHandlers {
    aes128: Handler,
    aes256: Handler,
    aes_gcm: Handler,
    /// Serialized `/Encrypt` dictionary bytes for a V5/R6 AES-256 document with
    /// empty user password, cleared MAC permission bit, and fixed `/KDFSalt`.
    mac_encrypt_dict: Vec<u8>,
}

fn cached() -> &'static CachedHandlers {
    static CACHE: OnceLock<CachedHandlers> = OnceLock::new();
    CACHE.get_or_init(|| {
        let budget = Budget::new(&DocumentLimits::web());
        let build = |method| {
            PasswordEncryption {
                user: b"userpw".to_vec(),
                owner: b"ownerpw".to_vec(),
                permissions: u32::MAX,
                method,
                random: [0x2A; 64],
            }
            .build(Some(b"0123456789abcdef"), &budget)
            .expect("fixed password handler builds")
            .2
        };
        let mac_enc = Encryption::PasswordWithMac(PasswordEncryption {
            user: Vec::new(),
            owner: b"ownerpw".to_vec(),
            permissions: u32::MAX,
            method: Method::Aes256,
            random: [0x3C; 64],
        });
        let (mac_dict, _, _) = mac_enc
            .build(Some(b"fedcba9876543210"), &budget)
            .expect("fixed MAC encryption dictionary builds");
        let mut out = pyropdf_store::budget::BudgetVec::new(&budget);
        pyropdf_store::write::value(&mut out, &Value::Dict(mac_dict))
            .expect("MAC encryption dictionary writes");
        CachedHandlers {
            aes128: build(Method::Aes128),
            aes256: build(Method::Aes256),
            aes_gcm: build(Method::AesGcm),
            mac_encrypt_dict: out.into_vec(),
        }
    })
}

fn fuzz_budget() -> Budget {
    let mut limits = DocumentLimits::web();
    limits.max_memory_size = Some(64 * 1024 * 1024);
    limits.max_decompressed_size = Some(4 * 1024 * 1024);
    limits.max_total_decompressed_size = Some(8 * 1024 * 1024);
    limits.max_duration = Some(Duration::from_secs(2));
    Budget::new(&limits)
}

/// Deterministic recipient envelope callback: also exercises `decrypt_legacy_cms`
/// on envelope slices, then derives a 20-byte stream or 24-byte document payload.
fn recipient_callback(
    selector: u8,
    budget: &Budget,
) -> impl FnMut(&[u8]) -> pyropdf_store::Result<Option<RecipientPayload>> + '_ {
    let mut call = 0u8;
    move |der: &[u8]| {
        call = call.wrapping_add(1);
        if der.len() >= 2 {
            let klen = usize::from(der[0] % 32).max(1).min(der.len());
            let key = &der[..klen];
            let ct = &der[klen..];
            let _ = security::decrypt_legacy_cms(key, ct, None, budget);
            if ct.len() >= 8 {
                let iv = &ct[..8];
                let bits = usize::from(der[1] % 128).max(1);
                let _ = security::decrypt_legacy_cms(key, &ct[8..], Some((bits, iv)), budget);
            }
        }
        if (selector ^ call) & 0x07 == 0 {
            return Ok(None);
        }
        if (selector.wrapping_add(call)) & 1 == 0 {
            let mut payload = [0u8; 24];
            for (i, slot) in payload.iter_mut().enumerate() {
                *slot = der.get(i).copied().unwrap_or(selector ^ i as u8);
            }
            // Exercise both owner (bit 2 set) and non-owner recipient permissions.
            if selector & 2 != 0 {
                payload[23] |= 0x02;
            }
            Ok(Some(RecipientPayload::Document(payload)))
        } else {
            let mut seed = [0u8; 20];
            for (i, slot) in seed.iter_mut().enumerate() {
                *slot = der.get(i).copied().unwrap_or(selector ^ i as u8);
            }
            Ok(Some(RecipientPayload::Stream(seed)))
        }
    }
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(char::from(HEX[usize::from(b >> 4)]));
        out.push(char::from(HEX[usize::from(b & 15)]));
    }
    out
}

/// Wrap raw token bytes into a V5/R6 PDF with either a standalone `/AuthCode`
/// MAC or an `AttachedToSig` signature dictionary whose `/ByteRange` covers
/// every byte outside the hex string, sending mutated DER directly into
/// `integrity::verify`.
fn mac_template_pdf(encrypt_dict: &[u8], token: &[u8], attached: bool) -> Vec<u8> {
    let hex = hex_encode(token);
    let mut out = b"%PDF-2.0\n".to_vec();
    let o1 = out.len();
    out.extend_from_slice(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n");
    let o2 = out.len();
    out.extend_from_slice(b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n");
    let o3 = out.len();
    let (sig_placeholder_start, sig_hex_start, sig_hex_end) = if attached {
        out.extend_from_slice(b"3 0 obj\n<< /Type /Sig /ByteRange ");
        let br_pos = out.len();
        out.extend_from_slice(b"[0000000000 0000000000 0000000000 0000000000]");
        out.extend_from_slice(b" /Contents <");
        let h_start = out.len() - 1;
        out.extend_from_slice(hex.as_bytes());
        out.extend_from_slice(b"> >>\nendobj\n");
        let h_end = out.len() - b" >>\nendobj\n".len();
        (Some(br_pos), h_start, h_end)
    } else {
        out.extend_from_slice(b"3 0 obj\n<< >>\nendobj\n");
        (None, 0, 0)
    };
    let o4 = out.len();
    out.extend_from_slice(b"4 0 obj\n");
    out.extend_from_slice(encrypt_dict);
    out.extend_from_slice(b"\nendobj\n");
    let startxref = out.len();
    let xref = format!(
        "xref\n0 5\n0000000000 65535 f \n{o1:010} 00000 n \n{o2:010} 00000 n \n{o3:010} 00000 n \n{o4:010} 00000 n \n"
    );
    out.extend_from_slice(xref.as_bytes());
    let (br_pos, h_start, h_end) = if attached {
        out.extend_from_slice(
            b"trailer\n<< /Size 5 /Root 1 0 R /Encrypt 4 0 R /ID [<0011223344556677> <0011223344556677>] \
              /AuthCode << /MACLocation /AttachedToSig /SigObjRef 3 0 R >> >>\n",
        );
        (
            sig_placeholder_start.expect("attached placeholder"),
            sig_hex_start,
            sig_hex_end,
        )
    } else {
        out.extend_from_slice(
            b"trailer\n<< /Size 5 /Root 1 0 R /Encrypt 4 0 R /ID [<0011223344556677> <0011223344556677>] \
              /AuthCode << /MACLocation /Standalone /ByteRange ",
        );
        let br_pos = out.len();
        out.extend_from_slice(b"[0000000000 0000000000 0000000000 0000000000]");
        out.extend_from_slice(b" /MAC <");
        let h_start = out.len() - 1;
        out.extend_from_slice(hex.as_bytes());
        out.extend_from_slice(b"> >> >>\n");
        let h_end = out.len() - b" >> >>\n".len();
        (br_pos, h_start, h_end)
    };
    let tail = format!("startxref\n{startxref}\n%%EOF\n");
    out.extend_from_slice(tail.as_bytes());
    let total = out.len();
    let br = format!(
        "[{:010} {:010} {:010} {:010}]",
        0usize,
        h_start,
        h_end,
        total.saturating_sub(h_end)
    );
    debug_assert_eq!(br.len(), 45);
    out[br_pos..br_pos + br.len()].copy_from_slice(br.as_bytes());
    out
}

fn inspect_base(base: FileBase, data: &[u8], selector: u8, budget: &Budget) {
    let _ = integrity::verify(base.index(), budget);
    if base.index().revisions().len() > 1 {
        let _ = base.revision(0, budget);
    }
    if let Some(handler) = base.security() {
        let _ = handler.description();
        let _ = handler.access();
        let _ = handler.permissions();
        let _ = handler.revision();
        let _ = handler.methods();
        let _ = handler.uses_gcm();
        let _ = handler.encrypts_metadata();
        let _ = handler.reader_password();
        let _ = handler.recipient_bridge(budget);
    }
    let numbers: Vec<u32> = base
        .index()
        .entries()
        .map(|(n, _)| n)
        .take(MAX_OBJECTS)
        .collect();
    let unlocked = !base.index().is_encrypted() || base.security().is_some();
    let doc = Document::new(Arc::new(base), None);
    let version = doc.current();
    for number in numbers {
        let _ = version.get(number, budget);
    }
    if unlocked && data.len() <= MAX_RESAVE_INPUT {
        let enc = match selector % 6 {
            0 => Encryption::Password(PasswordEncryption {
                user: b"userpw".to_vec(),
                owner: b"ownerpw".to_vec(),
                permissions: u32::MAX,
                method: Method::Aes128,
                random: [selector; 64],
            }),
            1 => Encryption::Password(PasswordEncryption {
                user: b"userpw".to_vec(),
                owner: b"ownerpw".to_vec(),
                permissions: u32::MAX,
                method: Method::Aes256,
                random: [selector; 64],
            }),
            2 => Encryption::Password(PasswordEncryption {
                user: b"userpw".to_vec(),
                owner: b"ownerpw".to_vec(),
                permissions: u32::MAX,
                method: Method::AesGcm,
                random: [selector; 64],
            }),
            3 => Encryption::PasswordWithMac(PasswordEncryption {
                user: b"userpw".to_vec(),
                owner: b"ownerpw".to_vec(),
                permissions: u32::MAX,
                method: Method::AesGcm,
                random: [selector; 64],
            }),
            4 => Encryption::Recipient(RecipientEncryption {
                seed: [selector; 20],
                id: [selector; 16],
                envelopes: vec![vec![0x30, 0x03, 0x02, 0x01, selector]],
                encrypt_metadata: selector & 8 != 0,
                method: if selector & 16 != 0 {
                    Method::AesGcm
                } else {
                    Method::Aes256
                },
            }),
            _ => Encryption::RecipientWithMac(RecipientEncryption {
                seed: [selector; 20],
                id: [selector; 16],
                envelopes: vec![vec![0x30, 0x03, 0x02, 0x01, selector]],
                encrypt_metadata: true,
                method: Method::Aes256,
            }),
        };
        let mut out = Vec::new();
        if save::full_with(&version, &mut out, budget, Some(&enc), Options::default()).is_ok() {
            let reopened = match &enc {
                Encryption::Password(_) | Encryption::PasswordWithMac(_) => {
                    FileBase::open_with_password(Bytes::new(out), Some(b"ownerpw"), budget)
                }
                Encryption::Recipient(r) | Encryption::RecipientWithMac(r) => {
                    let seed = r.seed;
                    FileBase::open_with_recipient(
                        Bytes::new(out),
                        |_| {
                            let mut payload = [0u8; 24];
                            payload[..20].copy_from_slice(&seed);
                            payload[20..].copy_from_slice(&u32::MAX.to_be_bytes());
                            Ok(Some(RecipientPayload::Document(payload)))
                        },
                        budget,
                    )
                }
            };
            if let Ok(reopened) = reopened {
                let _ = integrity::verify(reopened.index(), budget);
                let redoc = Document::new(Arc::new(reopened), None).current();
                if let Some(Value::Reference(root)) = redoc.trailer().get("Root").cloned() {
                    let _ = redoc.get(root.number(), budget);
                }
            }
        }
    }
}

fuzz_target!(
    // The store turns a panic of hayro-syntax into an error (DESIGN.md I3),
    // but libFuzzer's hook aborts on any panic: keep the default hook, so
    // that only a panic that escapes the target is a crash.
    init: {
        let _ = std::panic::take_hook();
    },
    |data: &[u8]| {
        let budget = fuzz_budget();
        let selector = data.last().copied().unwrap_or(0);

        if data.starts_with(b"%PDF-") {
            let shared = Bytes::new(data.to_vec());
            let mut inspected = 0usize;
            if let Ok(base) = FileBase::open(shared.clone(), &budget) {
                inspect_base(base, data, selector, &budget);
                inspected += 1;
            }
            for password in [b"userpw".as_slice(), b"ownerpw".as_slice()] {
                if inspected >= 2 {
                    break;
                }
                if let Ok(base) = FileBase::open_with_password(shared.clone(), Some(password), &budget) {
                    inspect_base(base, data, selector, &budget);
                    inspected += 1;
                }
            }
            // Also exercise password-length limit enforcement (> MAX_PASSWORD).
            let pw_len = usize::from(selector).min(MAX_PASSWORD + 8).min(data.len());
            let tail_pw = &data[data.len() - pw_len..];
            let _ = FileBase::open_with_password(shared.clone(), Some(tail_pw), &budget);

            if let Ok(base) = FileBase::open_with_recipient(
                shared.clone(),
                recipient_callback(selector, &budget),
                &budget,
            ) {
                if let Some(first) = base.security()
                    && let Ok(second_base) = FileBase::open_with_recipient(
                        shared,
                        recipient_callback(selector ^ 2, &budget),
                        &budget,
                    )
                    && let Some(second) = second_base.security()
                {
                    first.retain_recipient_access(second);
                }
                if inspected < 2 {
                    inspect_base(base, data, selector, &budget);
                }
            }
            return;
        }

        // Non-PDF inputs: exercise raw ciphers, /Encrypt dicts, legacy CMS, and MAC DER tokens.
        let cache = cached();

        // 1. Legacy CMS RC4 and RC2-CBC decryption.
        if data.len() >= 2 {
            let key_len = usize::from(data[0] % 36).min(data.len() - 1);
            let key = &data[1..1 + key_len];
            let ct = &data[1 + key_len..];
            let _ = security::decrypt_legacy_cms(key, ct, None, &budget);
            let iv_len = usize::from(data[0] % 12).min(ct.len());
            let bits = usize::from(selector);
            let _ = security::decrypt_legacy_cms(key, &ct[iv_len..], Some((bits, &ct[..iv_len])), &budget);
        }

        // 2. AES-128, AES-256, and AES-GCM decryption of arbitrary ciphertext + round-trip.
        let reference = Reference::new(u32::from(selector).max(1), 0);
        for handler in [&cache.aes128, &cache.aes256, &cache.aes_gcm] {
            for kind in [Kind::String, Kind::Stream] {
                let _ = handler.decrypt(kind, reference, data, &budget);
            }
        }
        if data.len() <= 4096 {
            let handler = match selector % 3 {
                0 => &cache.aes128,
                1 => &cache.aes256,
                _ => &cache.aes_gcm,
            };
            let kind = if selector & 4 == 0 {
                Kind::String
            } else {
                Kind::Stream
            };
            if let Ok(ciphertext) = handler.encrypt(kind, reference, data, &budget) {
                let plain = handler
                    .decrypt(kind, reference, &ciphertext, &budget)
                    .expect("freshly encrypted bytes decrypt");
                assert_eq!(plain, data, "cipher round-trip");
            }
        }

        // 3. Direct /Encrypt dictionary parsing for Standard and Adobe.PubSec handlers.
        if let Ok(Value::Dict(dict)) = Value::parse(&Bytes::new(data.to_vec()), &budget) {
            let id = b"0123456789abcdef";
            for pw in [b"".as_slice(), b"userpw".as_slice()] {
                if let Ok(handler) = Handler::open(&dict, id, pw, &budget) {
                    let _ = handler.description();
                    let _ = handler.decrypt(Kind::String, reference, data, &budget);
                }
            }
            if let Ok(handler) =
                Handler::open_recipient(&dict, recipient_callback(selector, &budget), &budget)
            {
                let _ = handler.description();
                let _ = handler.recipient_bridge(&budget);
                let _ = handler.decrypt(Kind::Stream, reference, data, &budget);
            }
        }

        // 4. ISO/TS 32004:2024 Document MAC DER decoding (standalone & signature-attached).
        if !data.is_empty() && data.len() <= MAX_MAC_TOKEN {
            let attached = selector & 1 != 0;
            let pdf = mac_template_pdf(&cache.mac_encrypt_dict, data, attached);
            if let Ok(base) = FileBase::open(Bytes::new(pdf), &budget) {
                let _ = integrity::verify(base.index(), &budget);
            }
        }
    }
);
