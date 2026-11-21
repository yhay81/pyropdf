//! Integrity protection of encrypted PDF revisions (ISO/TS 32004:2024).
mod token;

use super::{Handler, bytes, crypto};
use crate::budget::Budget;
use crate::index::Index;
use crate::value::{Dict, Name, PdfString, Value};
use crate::{Error, ErrorKind, Result};

/// Result for the opened bytes, independent of whether the file key or a
/// signer's certificate authenticated successfully.
#[derive(Debug)]
pub enum Report {
    /// No PDF MAC is declared or required.
    Absent,
    /// The file must be unlocked to authenticate the MAC.
    Locked,
    /// The latest revision has a valid standalone or signature-attached MAC.
    Valid(&'static str),
    /// The token, its binding, or required fields did not validate.
    Invalid(String),
}

const MAC_BIT: u32 = 1 << 12;

fn encrypted_permissions(dict: &Dict, handler: &Handler) -> Result<Option<u32>> {
    if handler.is_recipient() || handler.version < 5 {
        return Ok(None);
    }
    let Some(block) = bytes(dict, "Perms").and_then(|v| v.try_into().ok()) else {
        return Ok(None);
    };
    let plain = crypto::aes256_ecb_block(&handler.key, block, true)?;
    if &plain[9..12] != b"adb" {
        return Ok(None);
    }
    Ok(Some(u32::from_le_bytes(
        plain[..4].try_into().expect("a four-byte word"),
    )))
}

/// The report describes the original bytes of the selected revision.
/// Unsaved edits have no byte ranges or MAC of their own.
pub fn verify(file: &Index, budget: &Budget) -> Result<Report> {
    match inspect(file, budget) {
        Ok((report, _)) => Ok(report),
        Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => {
            Ok(Report::Invalid(error.to_string()))
        }
        Err(error) => Err(error),
    }
}

/// Key material for a standalone token. Never displayed or exposed to Python.
pub struct Writer {
    file_key: [u8; 32],
    salt: [u8; 32],
    mac_key: [u8; 32],
}

impl Writer {
    pub(crate) fn new(handler: &Handler, salt: [u8; 32]) -> Result<Self> {
        let mut mac_key = [0; 32];
        getrandom::fill(&mut mac_key)
            .map_err(|_| Error::malformed("cannot obtain randomness for the PDF MAC key"))?;
        let file_key = handler
            .key
            .as_ref()
            .try_into()
            .map_err(|_| Error::malformed("PDF MAC requires a 256-bit file key"))?;
        Ok(Self {
            file_key,
            salt,
            mac_key,
        })
    }

    /// Add the MAC as an unsigned attribute of the newly generated signature
    /// or RFC 3161 token. Bytes covered by the signature stay untouched.
    pub fn attach(&self, data: &mut [u8], start: usize, end: usize, budget: &Budget) -> Result<()> {
        use crate::budget::BudgetVec;
        use sha2::{Digest, Sha256};
        let allocate = || Error::malformed("invalid reserved PDF signature range");
        let gap = data.get(start..end).ok_or_else(allocate)?;
        if gap.first() != Some(&b'<') || gap.last() != Some(&b'>') || gap.len() % 2 != 0 {
            return Err(allocate());
        }
        let mut der = BudgetVec::new(budget);
        for pair in gap[1..gap.len() - 1].as_chunks::<2>().0 {
            let digit = |c: u8| char::from(c).to_digit(16).ok_or_else(allocate);
            der.try_push((digit(pair[0])? * 16 + digit(pair[1])?) as u8)?;
        }
        let mut hash = Sha256::new();
        for part in [&data[..start], &data[end..]] {
            for chunk in part.chunks(64 * 1024) {
                budget.poll()?;
                hash.update(chunk);
            }
        }
        let result = token::attach(&der, self, &hash.finalize().into(), budget)?;
        if result.len() > der.len() {
            return Err(Error::malformed(format!(
                "signature with attached PDF MAC needs {} bytes, {} reserved",
                result.len(),
                der.len()
            )));
        }
        let hex = &mut data[start + 1..end - 1];
        hex.fill(b'0');
        const HEX: &[u8; 16] = b"0123456789abcdef";
        for (byte, pair) in result.iter().zip(hex.as_chunks_mut::<2>().0.iter_mut()) {
            *pair = [HEX[usize::from(byte >> 4)], HEX[usize::from(byte & 15)]];
        }
        Ok(())
    }

    pub(crate) fn token(&self, digest: &[u8; 32]) -> Result<Vec<u8>> {
        token::encode(&self.file_key, &self.salt, &self.mac_key, digest, None)
    }
}

#[expect(
    clippy::disallowed_methods,
    reason = "adds one 32-byte salt and replaces two fixed-size permission entries"
)]
pub(crate) fn enable(dict: &mut Dict, handler: &mut Handler) -> Result<[u8; 32]> {
    if handler.version < 5 || handler.key.len() != 32 {
        return Err(Error::malformed(
            "PDF MAC needs PDF 2.0 and a 256-bit security handler",
        ));
    }
    let mut salt = [0u8; 32];
    getrandom::fill(&mut salt)
        .map_err(|_| Error::malformed("cannot obtain the PDF MAC KDF salt"))?;
    dict.insert(
        Name::from("KDFSalt"),
        Value::String(PdfString::hexadecimal(salt.as_slice())),
    );
    handler.permissions &= !MAC_BIT;
    if !handler.is_recipient() {
        let block = bytes(dict, "Perms")
            .and_then(|v| v.try_into().ok())
            .ok_or_else(|| Error::malformed("missing encrypted permissions for PDF MAC"))?;
        let mut perms = crypto::aes256_ecb_block(&handler.key, block, true)?;
        perms[..4].copy_from_slice(&handler.permissions.to_le_bytes());
        let block = crypto::aes256_ecb_block(&handler.key, perms, false)?;
        dict.insert(
            Name::from("P"),
            Value::Integer(i64::from(handler.permissions as i32)),
        );
        dict.insert(
            Name::from("Perms"),
            Value::String(PdfString::hexadecimal(block.as_slice())),
        );
    }
    Ok(salt)
}

fn inspect(file: &Index, budget: &Budget) -> Result<(Report, Option<[u8; 32]>)> {
    budget.poll()?;
    let auth = file.trailer().get("AuthCode");
    if !file.is_encrypted() && auth.is_none() {
        return Ok((Report::Absent, None));
    }
    let encrypt = crate::save::encryption_dict(file, budget)?.unwrap_or_default();
    let salt = encrypt.get("KDFSalt");
    let Some(handler) = file.security() else {
        return if file.is_encrypted() {
            Ok((Report::Locked, None))
        } else {
            Err(Error::malformed("PDF MAC requires encryption"))
        };
    };
    let authenticated = encrypted_permissions(&encrypt, handler)?;
    if handler.version >= 5 && !handler.is_recipient() && authenticated.is_none() {
        return Err(Error::malformed(
            "missing or invalid authenticated permissions in the 256-bit encryption dictionary",
        ));
    }
    let required = handler.version >= 5
        && (handler.permissions() & MAC_BIT == 0
            || authenticated.is_some_and(|p| p & MAC_BIT == 0));
    if !required && auth.is_none() && salt.is_none() {
        return Ok((Report::Absent, None));
    }
    if handler.version < 5
        || (!handler.is_recipient() && authenticated != Some(handler.permissions()))
    {
        return Err(Error::malformed(
            "invalid encrypted permissions for PDF MAC",
        ));
    }
    if handler.is_recipient() {
        let selected_default = ["StmF", "StrF"].into_iter().any(|key| matches!(encrypt.get(key), Some(Value::Name(n)) if n.as_bytes() == b"DefaultCryptFilter"));
        if !selected_default {
            return Err(Error::malformed(
                "PDF MAC requires the public-key DefaultCryptFilter key",
            ));
        }
    }
    let salt: [u8; 32] = bytes(&encrypt, "KDFSalt")
        .and_then(|s| s.try_into().ok())
        .ok_or_else(|| Error::malformed("PDF MAC requires a direct 32-byte /KDFSalt"))?;
    let Some(Value::Dict(dict)) = auth else {
        return Err(Error::malformed(
            "PDF MAC requires a direct /AuthCode dictionary",
        ));
    };
    let Some(Value::Name(location)) = dict.get("MACLocation") else {
        return Err(Error::malformed("missing PDF MAC location"));
    };
    let (location, cms, signature, ranges) = match location.as_bytes() {
        b"Standalone" if dict.get("SigObjRef").is_none() => {
            let value = bytes(dict, "MAC")
                .ok_or_else(|| Error::malformed("expected a standalone PDF MAC string"))?;
            let ranges = covered(file, dict, value, budget)?;
            (
                "standalone",
                token::standalone(value, budget)?,
                None,
                ranges,
            )
        }
        b"AttachedToSig" if dict.get("MAC").is_none() && dict.get("ByteRange").is_none() => {
            let Some(&Value::Reference(reference)) = dict.get("SigObjRef") else {
                return Err(Error::malformed("an attached PDF MAC requires /SigObjRef"));
            };
            let raw = file
                .object(reference, budget)?
                .ok_or_else(|| Error::malformed("missing PDF MAC signature object"))?;
            let Value::Dict(signature) = Value::parse(&raw, budget)? else {
                return Err(Error::malformed("invalid PDF MAC signature dictionary"));
            };
            let value = bytes(&signature, "Contents")
                .ok_or_else(|| Error::malformed("missing /Contents for attached PDF MAC"))?;
            let ranges = covered(file, &signature, value, budget)?;
            let (cms, signature_value) = token::attached(value, budget)?;
            ("attached", cms, Some(signature_value), ranges)
        }
        _ => {
            return Err(Error::malformed(
                "invalid PDF MAC location or incompatible /AuthCode entries",
            ));
        }
    };
    token::verify(
        cms,
        &handler.key,
        &salt,
        &ranges,
        signature.as_ref().map(|s| s.as_bytes()),
        budget,
    )?;
    Ok((Report::Valid(location), Some(salt)))
}

/// Refuse to bless a broken source with a freshly generated token on save.
pub(crate) fn original_salt(file: &Index, budget: &Budget) -> Result<Option<[u8; 32]>> {
    inspect(file, budget).map(|(_, salt)| salt)
}

fn covered<'a>(
    file: &'a Index,
    dictionary: &Dict,
    value: &[u8],
    budget: &Budget,
) -> Result<[&'a [u8]; 2]> {
    let invalid = || {
        Error::malformed(
            "PDF MAC /ByteRange must cover the entire revision except its hexadecimal token",
        )
    };
    let Some(Value::Array(array)) = dictionary.get("ByteRange") else {
        return Err(invalid());
    };
    let [
        Value::Integer(0),
        Value::Integer(a),
        Value::Integer(b),
        Value::Integer(c),
    ] = array.as_slice()
    else {
        return Err(invalid());
    };
    let a = usize::try_from(*a).map_err(|_| invalid())?;
    let b = usize::try_from(*b).map_err(|_| invalid())?;
    let c = usize::try_from(*c).map_err(|_| invalid())?;
    let bytes = file.bytes();
    let gap = bytes.get(a..b).ok_or_else(invalid)?;
    if b.checked_add(c) != Some(bytes.len())
        || value.len().checked_mul(2).and_then(|n| n.checked_add(2)) != Some(gap.len())
        || gap.first() != Some(&b'<')
        || gap.last() != Some(&b'>')
    {
        return Err(invalid());
    }
    if let Some(lexeme) = dictionary.lexeme()
        && let Some(start) = bytes.offset_of(lexeme)
        && (a < start || b > start + lexeme.len())
    {
        return Err(invalid());
    }
    for (index, (pair, byte)) in gap[1..gap.len() - 1]
        .as_chunks::<2>()
        .0
        .iter()
        .zip(value)
        .enumerate()
    {
        if index % 16384 == 0 {
            budget.poll()?;
        }
        let hex = |c: u8| char::from(c).to_digit(16).ok_or_else(invalid);
        if hex(pair[0])? * 16 + hex(pair[1])? != u32::from(*byte) {
            return Err(invalid());
        }
    }
    Ok([&bytes[..a], &bytes[b..]])
}
