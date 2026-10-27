//! Settling the structures that depend on other objects (DESIGN.md section
//! 7).
//!
//! A write that makes such a structure stale marks it in its transaction
//! with [`Transaction::mark`]. The store settles the marks with the
//! [`Settler`] that the document registers for their structure:
//!
//! - before a read that depends on one, which calls [`Transaction::settle`]
//!   with [`Scope::One`];
//! - when a transaction commits, which settles everything but font subsets;
//! - before a save, which settles everything with [`Scope::Save`] in a
//!   transaction that it then drops, so that the current version keeps its
//!   provisional fonts and the history gains no step.
//!
//! A version keeps the marks that it leaves stale: font subsets, and the
//! structures that no settler is registered for. Undo and redo therefore
//! restore them together with the objects they belong to.

use std::fmt;
use std::sync::Arc;

use crate::budget::{Budget, LimitCode};
use crate::version::Transaction;
use crate::{Error, Result};

/// A structure that depends on other objects, so that a write can make it
/// stale.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[non_exhaustive]
pub enum Structure {
    /// The appearance streams of the annotation that the mark names.
    Appearance,
    /// The `/Count` of the outline or outline item that the mark names.
    OutlineCount,
    /// The parent tree of the structure tree root that the mark names.
    ParentTree,
    /// The `/Limits` of the name- or number-tree node that the mark names.
    TreeLimits,
    /// The subset of the provisional font that the mark names. Only a save
    /// settles it.
    FontSubset,
}

impl Structure {
    /// Every structure, in the order that settling visits them. Font
    /// subsets come last, since the others may draw with fonts.
    pub const ALL: [Self; 5] = [
        Self::Appearance,
        Self::OutlineCount,
        Self::ParentTree,
        Self::TreeLimits,
        Self::FontSubset,
    ];

    const fn index(self) -> usize {
        self as usize
    }
}

/// A stale structure: which one, and the object that holds it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Mark {
    /// The structure.
    pub structure: Structure,
    /// The number of the object that holds it.
    pub number: u32,
}

/// Settles the stale structures of one kind, by writing into a transaction.
pub trait Settler: Send + Sync {
    /// Settles the structures that the objects `numbers`, sorted and
    /// distinct, hold. It may mark other structures stale, which settling
    /// then visits too.
    fn settle(&self, transaction: &mut Transaction, numbers: &[u32], budget: &Budget)
    -> Result<()>;
}

/// The settlers of a document, at most one per structure.
#[derive(Clone, Default)]
pub struct Settlers([Option<Arc<dyn Settler>>; Structure::ALL.len()]);

impl Settlers {
    /// Returns these settlers with `settler` settling `structure`, in place
    /// of any that did.
    #[must_use]
    pub fn with(mut self, structure: Structure, settler: Arc<dyn Settler>) -> Self {
        self.0[structure.index()] = Some(settler);
        self
    }

    /// Returns the settler of `structure`, if one is registered.
    #[must_use]
    pub fn get(&self, structure: Structure) -> Option<&Arc<dyn Settler>> {
        self.0[structure.index()].as_ref()
    }
}

impl fmt::Debug for Settlers {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_set()
            .entries(Structure::ALL.iter().filter(|s| self.get(**s).is_some()))
            .finish()
    }
}

/// The structures that a call of [`Transaction::settle`] settles.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scope {
    /// One structure, before a read that depends on it. Font subsets settle
    /// only at a save, so this scope never settles them.
    One(Structure),
    /// Every structure but font subsets, as a commit does.
    Commit,
    /// Every structure, as a save does.
    Save,
}

impl Scope {
    /// Returns whether the scope settles `structure`.
    #[must_use]
    pub fn covers(self, structure: Structure) -> bool {
        match self {
            Self::One(one) => one == structure && structure != Structure::FontSubset,
            Self::Commit => structure != Structure::FontSubset,
            Self::Save => true,
        }
    }
}

/// The most rounds that settling takes. A round settles each stale
/// structure once, and another follows only when settlers marked structures
/// again, so settlers that only mark structures after their own need two.
pub const MAX_ROUNDS: usize = 2 * Structure::ALL.len();

/// The marks of a version, sorted and distinct.
#[derive(Clone, Default, Debug)]
pub(crate) struct Stale(Vec<Mark>);

impl Stale {
    /// The bytes that one mark keeps alive, which a new mark charges.
    pub(crate) const MARK_SIZE: u64 = size_of::<Mark>() as u64;

    /// Adds `mark`, charging its memory when it is new.
    pub(crate) fn insert(&mut self, mark: Mark, budget: &Budget) -> Result<()> {
        if let Err(at) = self.0.binary_search(&mark) {
            budget.charge(LimitCode::MemorySize, Self::MARK_SIZE)?;
            #[expect(
                clippy::disallowed_methods,
                reason = "the budget charged the mark before the vector grows"
            )]
            self.0.insert(at, mark);
        }
        Ok(())
    }

    /// Returns whether `mark` is stale.
    pub(crate) fn contains(&self, mark: Mark) -> bool {
        self.0.binary_search(&mark).is_ok()
    }

    /// Returns whether a structure that `scope` covers is stale.
    pub(crate) fn any(&self, scope: Scope) -> bool {
        self.0.iter().any(|mark| scope.covers(mark.structure))
    }

    /// Returns whether a structure of the kind `structure` is stale.
    pub(crate) fn has(&self, structure: Structure) -> bool {
        self.0.iter().any(|mark| mark.structure == structure)
    }

    /// Removes the marks of `structure`, and returns the numbers they named.
    pub(crate) fn take(&mut self, structure: Structure) -> Vec<u32> {
        let start = self.0.partition_point(|mark| mark.structure < structure);
        let end = self.0.partition_point(|mark| mark.structure <= structure);
        #[expect(
            clippy::disallowed_methods,
            reason = "the numbers are as many as the marks, which the budget charged"
        )]
        self.0.drain(start..end).map(|mark| mark.number).collect()
    }

    /// Returns the marks.
    pub(crate) fn marks(&self) -> &[Mark] {
        &self.0
    }
}

/// Settles what `scope` covers in `transaction` with `settlers`.
///
/// Refuses with a malformed input error when settlers keep marking one
/// another's structures for [`MAX_ROUNDS`] rounds.
pub(crate) fn settle(
    transaction: &mut Transaction,
    settlers: &Settlers,
    scope: Scope,
    budget: &Budget,
) -> Result<()> {
    for _ in 0..MAX_ROUNDS {
        let mut settled = false;
        for structure in Structure::ALL {
            let Some(settler) = settlers.get(structure).filter(|_| scope.covers(structure)) else {
                continue;
            };
            let numbers = transaction.take_stale(structure);
            if numbers.is_empty() {
                continue;
            }
            settler.settle(transaction, &numbers, budget)?;
            settled = true;
        }
        if !settled {
            return Ok(());
        }
    }
    if Structure::ALL.iter().any(|structure| {
        scope.covers(*structure)
            && settlers.get(*structure).is_some()
            && transaction.version().stale.has(*structure)
    }) {
        return Err(Error::malformed(format!(
            "settling did not finish in {MAX_ROUNDS} rounds: the settlers keep marking one another's structures"
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests;
