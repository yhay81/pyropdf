//! Read-only content ciphers for legacy PDF recipient envelopes (ISO 32000-2 §7.6.5).

use aes::cipher::{Block, BlockCipherDecrypt};
use rc2::Rc2;

use super::crypto;
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::{Error, Result};

/// Decrypts the bounded CMS ciphertext after key transport. RC4 uses no
/// parameters. RC2-CBC takes its independent effective-bit count and its IV.
pub fn decrypt_legacy_cms(
    key: &[u8],
    ciphertext: &[u8],
    rc2_parameters: Option<(usize, &[u8])>,
    budget: &Budget,
) -> Result<Vec<u8>> {
    budget.check(LimitCode::RecipientEnvelopeSize, ciphertext.len() as u64)?;
    let Some((bits, iv)) = rc2_parameters else {
        if !(1..=32).contains(&key.len()) {
            return Err(Error::malformed("a PDF CMS RC4 key is 1 to 32 bytes"));
        }
        return crypto::rc4(key, ciphertext, budget);
    };
    if !(1..=16).contains(&key.len()) || !(1..=128).contains(&bits) {
        return Err(Error::malformed(
            "invalid PDF CMS RC2 key length or effective bits",
        ));
    }
    let mut previous: [u8; 8] = iv
        .try_into()
        .map_err(|_| Error::malformed("invalid CMS RC2 IV length"))?;
    let (blocks, remainder) = ciphertext.as_chunks::<8>();
    if blocks.is_empty() || !remainder.is_empty() {
        return Err(Error::malformed("CMS RC2 ciphertext requires full blocks"));
    }
    let cipher = Rc2::new_with_eff_key_len(key, bits);
    let mut out = BudgetVec::new(budget);
    out.try_reserve_exact(ciphertext.len())?;
    for current in blocks {
        let mut block = Block::<Rc2>::from(*current);
        cipher.decrypt_block(&mut block);
        out.try_extend(
            block
                .iter()
                .zip(previous)
                .map(|(plain, chain)| plain ^ chain),
        )?;
        previous = *current;
    }
    let padding = usize::from(out[out.len() - 1]);
    if !(1..=8).contains(&padding)
        || !out[out.len() - padding..]
            .iter()
            .all(|&byte| usize::from(byte) == padding)
    {
        return Err(Error::malformed("invalid CMS RC2 padding"));
    }
    out.truncate(out.len() - padding);
    Ok(out.into_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn effective_bits_are_independent_of_raw_rc2_key_length() {
        // RFC 2268 §5: an all-zero 8-byte key with 63 effective bits produces
        // the specified encryption of an all-zero block. Select the CBC IV
        // so the recovered block has one byte of valid CMS padding.
        let ciphertext = [0xeb, 0xb7, 0x73, 0xf9, 0x93, 0x27, 0x8e, 0xff];
        let iv = [0, 0, 0, 0, 0, 0, 0, 1];
        let clear =
            decrypt_legacy_cms(&[0; 8], &ciphertext, Some((63, &iv)), &Budget::default()).unwrap();
        assert_eq!(clear, [0; 7]);
    }

    #[test]
    fn content_is_bounded_before_a_cipher_is_selected() {
        let budget = Budget::default().with_limit(LimitCode::RecipientEnvelopeSize, Some(8));
        let refusal = decrypt_legacy_cms(&[], &[0; 9], None, &budget).unwrap_err();
        assert_eq!(
            refusal.limit().unwrap().code(),
            LimitCode::RecipientEnvelopeSize
        );
    }
}
