//! The objects that a placed page depends on (DESIGN.md F4 and F6).
//!
//! A page drawn as a Form XObject is copied with its resources and the
//! objects they reach. A cache of such Forms records the stamps of those
//! objects in the source version, and reuses a Form only while the source
//! still holds each of them. [`reachable`] finds them: every indirect object
//! that some values reach, followed through every key, so that the set
//! covers whatever a copy of the values copies.

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::Value;
use pyropdf_store::version::Version;
use pyropdf_store::{LimitExceeded, Result};

/// The deepest nesting of arrays and dictionaries that the walk follows in
/// one object, as the import walk bounds it.
const MAX_DEPTH: usize = 64;

/// Returns the object numbers that `values` reach in `version`, ascending
/// and once each: those the values name, and those the named objects name
/// in turn.
///
/// A number that names no object is listed too, since an object may later
/// be written there. The walk charges its visited set and its work list to
/// `budget`, polls it, and refuses with `object_depth` an object nested
/// deeper than the import walk follows.
pub fn reachable<'b>(
    version: &Version,
    values: &[&Value],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u32>> {
    let mut walk = Walk {
        visited: Bits::new(version, budget),
        pending: BudgetVec::new(budget),
        found: BudgetVec::new(budget),
        budget,
    };
    for value in values {
        walk.value(value, 0)?;
    }
    while let Some(number) = walk.pending.pop() {
        budget.poll()?;
        if let Some(object) = version.get(number, budget)? {
            walk.value(object.value(), 0)?;
        }
    }
    let mut found = walk.found;
    found.as_mut_slice().sort_unstable();
    Ok(found)
}

struct Walk<'b> {
    visited: Bits<'b>,
    pending: BudgetVec<'b, u32>,
    found: BudgetVec<'b, u32>,
    budget: &'b Budget,
}

impl Walk<'_> {
    fn value(&mut self, value: &Value, depth: usize) -> Result<()> {
        match value {
            Value::Reference(reference) => {
                let number = reference.number();
                if self.visited.insert(number)? {
                    self.found.try_push(number)?;
                    self.pending.try_push(number)?;
                }
            }
            Value::Array(array) => {
                self.deeper(depth)?;
                for item in array.iter() {
                    self.value(item, depth + 1)?;
                }
            }
            Value::Dict(dict) => {
                self.deeper(depth)?;
                for (_, item) in dict.iter() {
                    self.value(item, depth + 1)?;
                }
            }
            Value::Stream(stream) => {
                self.deeper(depth)?;
                for (_, item) in stream.dict().iter() {
                    self.value(item, depth + 1)?;
                }
            }
            _ => {}
        }
        Ok(())
    }

    fn deeper(&self, depth: usize) -> Result<()> {
        self.budget.poll()?;
        if depth >= MAX_DEPTH {
            return Err(LimitExceeded::new(LimitCode::ObjectDepth).into());
        }
        Ok(())
    }
}

/// The numbers that the walk has seen: a bit for each number below the
/// version's next number, and a sorted list for those above it, which name
/// no object and so add nothing to follow.
struct Bits<'b> {
    words: BudgetVec<'b, u64>,
    next: u64,
    beyond: BudgetVec<'b, u32>,
}

impl<'b> Bits<'b> {
    fn new(version: &Version, budget: &'b Budget) -> Self {
        Self {
            words: BudgetVec::new(budget),
            next: version.next_number(),
            beyond: BudgetVec::new(budget),
        }
    }

    /// Adds `number`, and returns whether it was new.
    fn insert(&mut self, number: u32) -> Result<bool, LimitExceeded> {
        if u64::from(number) >= self.next {
            return match self.beyond.as_slice().binary_search(&number) {
                Ok(_) => Ok(false),
                Err(at) => {
                    // Numbers beyond the version are rare: the walk does not
                    // follow them, so each comes from a value it read.
                    self.beyond.try_push(number)?;
                    self.beyond.as_mut_slice()[at..].rotate_right(1);
                    Ok(true)
                }
            };
        }
        let word = (number / 64) as usize;
        if self.words.len() <= word {
            self.words.try_reserve(word + 1 - self.words.len())?;
            while self.words.len() <= word {
                self.words.try_push(0)?;
            }
        }
        let bit = 1_u64 << (number % 64);
        let slot = &mut self.words.as_mut_slice()[word];
        let new = *slot & bit == 0;
        *slot |= bit;
        Ok(new)
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use pyropdf_store::ErrorKind;
    use pyropdf_store::budget::CancellationToken;
    use pyropdf_store::value::{Bytes, Dict, Reference};
    use pyropdf_store::version::{Document, MemoryBase};

    use super::*;

    fn version(objects: &[(u32, &[u8])]) -> Arc<Version> {
        let mut base = MemoryBase::new(Dict::new());
        for &(number, bytes) in objects {
            base.insert(Reference::new(number, 0), Bytes::new(bytes.to_vec()));
        }
        Document::new(Arc::new(base), None).current()
    }

    fn refs(numbers: &[u32]) -> Value {
        Value::Array(
            numbers
                .iter()
                .map(|&number| Value::Reference(Reference::new(number, 0)))
                .collect(),
        )
    }

    #[test]
    fn the_walk_follows_cycles_sharing_streams_and_missing_objects() {
        let version = version(&[
            (1, b"<< /A 2 0 R /B 2 0 R /Stream 4 0 R >>"),
            (2, b"<< /Back 1 0 R /Missing 9 0 R /Beyond 400 0 R >>"),
            (3, b"<< /Unreached 5 0 R >>"),
            (4, b"<< /Length 4 /Font 5 0 R >>\nstream\ntest\nendstream"),
            (5, b"[1 0 R]"),
        ]);
        let budget = Budget::default();
        let found = reachable(&version, &[&refs(&[1])], &budget).unwrap();
        assert_eq!(found.as_slice(), [1, 2, 4, 5, 9, 400]);
        let none = reachable(&version, &[&Value::Integer(1)], &budget).unwrap();
        assert!(none.is_empty());
    }

    #[test]
    fn the_walk_is_bounded_and_budgeted() {
        let version = version(&[(1, b"<< /A 2 0 R >>"), (2, b"1")]);
        let mut deep = refs(&[1]);
        for _ in 0..MAX_DEPTH {
            deep = Value::Array(vec![deep].into_iter().collect());
        }
        let refused = reachable(&version, &[&deep], &Budget::default()).unwrap_err();
        assert!(
            matches!(refused.kind(), ErrorKind::Limit(limit) if limit.code() == LimitCode::ObjectDepth)
        );

        let version = self::version(&[(1, b"<< /A 2 0 R >>"), (2, b"1")]);
        let poor = Budget::default().with_limit(LimitCode::MemorySize, Some(8));
        assert!(reachable(&version, &[&refs(&[1])], &poor).is_err());
        let token = CancellationToken::new();
        token.cancel();
        let cancelled = Budget::default().with_cancellation(token);
        assert!(reachable(&version, &[&refs(&[1])], &cancelled).is_err());
    }
}
