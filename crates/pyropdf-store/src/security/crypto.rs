//! The primitives of the standard security handler: RC4, AES in CBC mode,
//! MD5 and SHA-2 from RustCrypto, and the hash of revision 6 (ISO 32000-2
//! section 7.6.4.3.4, algorithm 2.B).

use aes::cipher::{BlockCipherDecrypt, BlockCipherEncrypt, KeyInit};
use aes::{Aes128, Aes256, Block};
use md5::{Digest, Md5};
use sha2::{Sha256, Sha384, Sha512};

use crate::budget::{Budget, BudgetVec};
use crate::{Error, Result};

/// RC4 (ISO 32000-2 section 7.6.3.2), which is its own inverse. RC4 is
/// read but never written.
pub(super) fn rc4(key: &[u8], data: &[u8], budget: &Budget) -> Result<Vec<u8>> {
    let mut state: [u8; 256] = core::array::from_fn(|i| i as u8);
    let mut j: u8 = 0;
    for i in 0..256 {
        j = j.wrapping_add(state[i]).wrapping_add(key[i % key.len()]);
        state.swap(i, usize::from(j));
    }
    let mut out = BudgetVec::new(budget);
    out.try_reserve_exact(data.len())?;
    let (mut i, mut j) = (0u8, 0u8);
    for &byte in data {
        i = i.wrapping_add(1);
        j = j.wrapping_add(state[usize::from(i)]);
        state.swap(usize::from(i), usize::from(j));
        let k = state[usize::from(state[usize::from(i)].wrapping_add(state[usize::from(j)]))];
        out.try_push(byte ^ k)?;
    }
    Ok(out.into_vec())
}

pub(super) fn md5(parts: &[&[u8]]) -> [u8; 16] {
    let mut hash = Md5::new();
    for part in parts {
        hash.update(part);
    }
    hash.finalize().into()
}

pub(super) fn sha256(parts: &[&[u8]]) -> [u8; 32] {
    let mut hash = Sha256::new();
    for part in parts {
        hash.update(part);
    }
    hash.finalize().into()
}

/// An AES key of 16 or 32 bytes.
enum Aes {
    Aes128(Box<Aes128>),
    Aes256(Box<Aes256>),
}

impl Aes {
    fn new(key: &[u8]) -> Result<Self> {
        match key.len() {
            16 => Ok(Self::Aes128(Box::new(
                Aes128::new_from_slice(key).map_err(|_| Error::malformed("bad AES key"))?,
            ))),
            32 => Ok(Self::Aes256(Box::new(
                Aes256::new_from_slice(key).map_err(|_| Error::malformed("bad AES key"))?,
            ))),
            _ => Err(Error::malformed("an AES key is 16 or 32 bytes")),
        }
    }

    fn encrypt(&self, block: &mut Block) {
        match self {
            Self::Aes128(cipher) => cipher.encrypt_block(block),
            Self::Aes256(cipher) => cipher.encrypt_block(block),
        }
    }

    fn decrypt(&self, block: &mut Block) {
        match self {
            Self::Aes128(cipher) => cipher.decrypt_block(block),
            Self::Aes256(cipher) => cipher.decrypt_block(block),
        }
    }
}

/// Encrypts `data` with AES in CBC mode from `iv`, without padding when
/// `pad` is false; `data` must then be whole blocks.
pub(super) fn aes_cbc_encrypt(
    key: &[u8],
    iv: &[u8; 16],
    data: &[u8],
    pad: bool,
    budget: &Budget,
) -> Result<Vec<u8>> {
    let cipher = Aes::new(key)?;
    let padding = if pad { 16 - data.len() % 16 } else { 0 };
    if !pad && !data.len().is_multiple_of(16) {
        return Err(Error::malformed("unpadded AES data is not whole blocks"));
    }
    let mut out = BudgetVec::new(budget);
    out.try_reserve_exact(data.len() + padding)?;
    let mut previous = *iv;
    let mut chunks = data.chunks(16).peekable();
    let mut pending: Option<[u8; 16]> = None;
    loop {
        let block: [u8; 16] = match chunks.next() {
            Some(chunk) if chunk.len() == 16 => chunk.try_into().expect("16 bytes"),
            Some(chunk) => {
                let mut block = [padding as u8; 16];
                block[..chunk.len()].copy_from_slice(chunk);
                pending = Some(block);
                break;
            }
            None => {
                if pad && padding == 16 {
                    pending = Some([16; 16]);
                }
                break;
            }
        };
        previous = encrypt_block(&cipher, &previous, &block);
        out.try_extend_from_slice(&previous)?;
    }
    if let Some(block) = pending {
        previous = encrypt_block(&cipher, &previous, &block);
        out.try_extend_from_slice(&previous)?;
    }
    Ok(out.into_vec())
}

fn encrypt_block(cipher: &Aes, previous: &[u8; 16], block: &[u8; 16]) -> [u8; 16] {
    let mut mixed = Block::from(core::array::from_fn::<u8, 16, _>(|i| {
        block[i] ^ previous[i]
    }));
    cipher.encrypt(&mut mixed);
    mixed.into()
}

/// Decrypts AES-CBC `data` from `iv`, removing PKCS #7 padding when `pad`.
/// A final partial block is dropped, and padding that does not read as
/// padding is kept, as readers commonly do.
pub(super) fn aes_cbc_decrypt(
    key: &[u8],
    iv: &[u8; 16],
    data: &[u8],
    pad: bool,
    budget: &Budget,
) -> Result<Vec<u8>> {
    let cipher = Aes::new(key)?;
    let mut out = BudgetVec::new(budget);
    out.try_reserve_exact(data.len())?;
    let mut previous = *iv;
    for chunk in data.as_chunks::<16>().0 {
        let current: [u8; 16] = *chunk;
        let mut block = Block::from(current);
        cipher.decrypt(&mut block);
        let plain: [u8; 16] = block.into();
        out.try_extend(plain.iter().zip(previous).map(|(a, b)| a ^ b))?;
        previous = current;
    }
    if pad
        && let Some(&last) = out.last()
        && (1..=16).contains(&last)
        && out.len() >= usize::from(last)
        && out[out.len() - usize::from(last)..]
            .iter()
            .all(|&b| b == last)
    {
        let len = out.len() - usize::from(last);
        out.truncate(len);
    }
    Ok(out.into_vec())
}

/// Encrypts one block with AES-256 without chaining, for `/Perms`.
pub(super) fn aes256_ecb_block(key: &[u8], block: [u8; 16], decrypt: bool) -> Result<[u8; 16]> {
    let cipher = Aes::new(key)?;
    let mut block = Block::from(block);
    if decrypt {
        cipher.decrypt(&mut block);
    } else {
        cipher.encrypt(&mut block);
    }
    Ok(block.into())
}

/// The hash of revision 6 (algorithm 2.B), or of revision 5 when `r6` is
/// false: SHA-256 of the password, the salt, and `user`.
#[expect(
    clippy::disallowed_methods,
    reason = "keys, hashes, and the /Encrypt entries are at most 127 + 64 bytes, not input-sized"
)]
pub(super) fn hash(
    password: &[u8],
    salt: &[u8],
    user: &[u8],
    r6: bool,
    budget: &Budget,
) -> Result<[u8; 32]> {
    let mut k: Vec<u8> = sha256(&[password, salt, user]).to_vec();
    if !r6 {
        return Ok(k.try_into().expect("32 bytes"));
    }
    let mut round: usize = 0;
    loop {
        // K1: the password, K, and the user data, 64 times.
        let mut k1 = BudgetVec::new(budget);
        for _ in 0..64 {
            k1.try_extend_from_slice(password)?;
            k1.try_extend_from_slice(&k)?;
            k1.try_extend_from_slice(user)?;
        }
        let iv: [u8; 16] = k[16..32].try_into().expect("16 bytes");
        let e = aes_cbc_encrypt(&k[..16], &iv, &k1, false, budget)?;
        let selector = e[..16].iter().map(|&b| u32::from(b)).sum::<u32>() % 3;
        k = match selector {
            0 => Sha256::digest(&e).to_vec(),
            1 => Sha384::digest(&e).to_vec(),
            _ => Sha512::digest(&e).to_vec(),
        };
        round += 1;
        let last = usize::from(*e.last().expect("E is not empty"));
        if round >= 64 && last + 32 <= round {
            break;
        }
    }
    Ok(k[..32].try_into().expect("32 bytes"))
}
