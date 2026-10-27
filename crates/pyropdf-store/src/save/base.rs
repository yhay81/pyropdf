//! The original file index as the base of immutable versions.

use crate::budget::{Budget, BudgetVec};
use crate::index::{Index, Listing, Location};
use crate::security::{self, Handler};
use crate::value::{Bytes, Dict, Reference, Value};
use crate::version::Base;
use crate::{Error, Password, Result, write};

/// An indexed file shared by every version of an opened document.
/// Opening parses the index, not all the objects. Object streams remain
/// lazy and share the index's bounded decode cache.
#[derive(Debug)]
pub struct FileBase {
    index: Index,
    next: u64,
    /// The object that holds the `/Encrypt` dictionary, which is never
    /// encrypted itself.
    encrypt: Option<Reference>,
}

impl FileBase {
    /// Opens `bytes` and retains them without a second copy. An encrypted
    /// document opens with the empty user password when it has one, and
    /// stays locked otherwise: its objects then refuse with
    /// [`Password::Required`].
    pub fn open(bytes: Bytes, budget: &Budget) -> Result<Self> {
        Self::open_with_password(bytes, None, budget)
    }

    /// Opens `bytes` as [`FileBase::open`] does, unlocking an encrypted
    /// document with `password`, which refuses with
    /// [`Password::Incorrect`] when it is neither the user nor the owner
    /// password. An unencrypted document ignores it.
    pub fn open_with_password(
        bytes: Bytes,
        password: Option<&[u8]>,
        budget: &Budget,
    ) -> Result<Self> {
        let mut index = Index::open(bytes, budget)?;
        unlock(&mut index, password, budget)?;
        Self::new(index, budget)
    }

    /// Connects an already-open index to versions. The high-water mark
    /// includes free entries and `/Size`, so allocation cannot reuse an
    /// object number that the original file reserved.
    pub fn new(index: Index, budget: &Budget) -> Result<Self> {
        let mut next = 1;
        for (number, _) in index.entries() {
            budget.poll()?;
            next = next.max(u64::from(number) + 1);
        }
        for section in index.revisions().iter().flat_map(|r| r.sections()) {
            budget.poll()?;
            if let Some(&(number, _)) = section.listings().last() {
                next = next.max(u64::from(number) + 1);
            }
        }
        if let Some(&Value::Integer(size)) = index.trailer().get("Size") {
            next = next.max(
                u64::try_from(size)
                    .unwrap_or(0)
                    .min(u64::from(u32::MAX) + 1),
            );
        }
        let encrypt = match index.trailer().get("Encrypt") {
            Some(Value::Reference(reference)) => Some(*reference),
            _ => None,
        };
        Ok(Self {
            index,
            next,
            encrypt,
        })
    }

    /// Returns the security handler of an unlocked encrypted document.
    #[must_use]
    pub const fn security(&self) -> Option<&Handler> {
        self.index.security()
    }

    /// Returns the original index, including its repair report.
    #[must_use]
    pub const fn index(&self) -> &Index {
        &self.index
    }
}

impl Base for FileBase {
    fn generation(&self, number: u32) -> Option<u16> {
        self.index.get(number).map(|entry| entry.generation())
    }

    fn free_generation(&self, number: u32) -> Option<u16> {
        if self.generation(number).is_some() {
            return None;
        }
        for section in self
            .index
            .revisions()
            .iter()
            .rev()
            .flat_map(|r| r.sections())
        {
            if let Ok(at) = section
                .listings()
                .binary_search_by_key(&number, |&(n, _)| n)
            {
                return match section.listings()[at].1 {
                    Listing::Free { generation } => Some(generation),
                    Listing::InUse(_) => None,
                };
            }
        }
        None
    }

    fn object(&self, reference: Reference, budget: &Budget) -> Result<Option<Bytes>> {
        if !self.index.is_encrypted() || Some(reference) == self.encrypt {
            return self.index.object(reference, budget);
        }
        let Some(handler) = self.index.security() else {
            if self.index.get(reference.number()).is_none() {
                return Ok(None);
            }
            return Err(Error::from(Password::Required).context(format!(
                "object {} {}",
                reference.number(),
                reference.generation()
            )));
        };
        let Some(bytes) = self.index.object(reference, budget)? else {
            return Ok(None);
        };
        // Members of object streams were decrypted with their stream.
        if matches!(
            self.index.get(reference.number()).map(|e| e.location()),
            Some(Location::Member { .. })
        ) {
            return Ok(Some(bytes));
        }
        // The data of a stream whose `/Length` refers to an object is
        // decrypted as the length gives it. The length is an integer, which
        // is never encrypted, so the index gives its bytes as they are.
        let fetch = |length| self.index.object(length, budget);
        let value = Value::parse_with_lengths(&bytes, budget, &fetch)?;
        let plain = security::decrypt_value(handler, reference, &value, budget)?;
        let mut out = BudgetVec::new(budget);
        write::value(&mut out, &plain)?;
        Ok(Some(Bytes::new(out.into_vec())))
    }

    fn trailer(&self) -> &Dict {
        self.index.trailer()
    }

    fn next_number(&self) -> u64 {
        self.next
    }

    fn numbers(&self, out: &mut BudgetVec<'_, u32>) -> Result<()> {
        for (number, _) in self.index.entries() {
            out.budget().poll()?;
            out.try_push(number)?;
        }
        for section in self.index.revisions().iter().flat_map(|r| r.sections()) {
            for &(number, _) in section.listings() {
                out.budget().poll()?;
                out.try_push(number)?;
            }
        }
        Ok(())
    }

    fn file(&self) -> Option<&Index> {
        Some(&self.index)
    }
}

/// Unlocks an encrypted `index` with `password`, or else with the empty
/// password, which leaves the index locked when it does not match.
fn unlock(index: &mut Index, password: Option<&[u8]>, budget: &Budget) -> Result<()> {
    if !index.is_encrypted() {
        return Ok(());
    }
    let encrypt = match index.trailer().get("Encrypt") {
        Some(Value::Dict(dict)) => Some(dict.clone()),
        Some(&Value::Reference(reference)) => match index.object(reference, budget)? {
            Some(bytes) => match Value::parse(&bytes, budget)? {
                Value::Dict(dict) => Some(dict),
                _ => None,
            },
            None => None,
        },
        _ => None,
    };
    let id = match index.trailer().get("ID") {
        Some(Value::Array(id)) => match id.get(0) {
            Some(Value::String(first)) => first.as_bytes(),
            _ => &[],
        },
        _ => &[],
    };
    let handler = match (encrypt, password) {
        (Some(encrypt), Some(password)) => Some(Handler::open(&encrypt, id, password, budget)?),
        (Some(encrypt), None) => match Handler::open(&encrypt, id, b"", budget) {
            Ok(handler) => Some(handler),
            Err(error) if error.limit().is_some() => return Err(error),
            Err(_) => None,
        },
        (None, Some(_)) => {
            return Err(Error::malformed(
                "the trailer's /Encrypt is not a dictionary",
            ));
        }
        (None, None) => None,
    };
    if let Some(handler) = handler {
        index.unlock(handler, budget)?;
    }
    Ok(())
}
