//! The map from object number to slot that a version holds (DESIGN.md
//! section 4.1): pyropdf's own chunk table, decided in yhay81/pyropdf#9 and
//! measured in evidence/persistent-map, from whose `src/chunks.rs` it comes.
//!
//! The table divides object numbers into chunks of 16 slots, which a radix
//! directory of 32-way branches reaches. Cloning a table shares all of it. A
//! write copies the chunk of its slot and the branches above it while
//! another table holds them too, and changes in place what the table holds
//! alone, so a transaction copies each chunk and branch at most once. One
//! write therefore allocates at most one chunk and six branches with 32-bit
//! numbers, however many objects the table holds, and it charges what it
//! allocates to the operation's budget first. Numbers are positions, so a
//! lookup indexes one array per level, and the slots are visited in the
//! order of their numbers without sorting.

use std::sync::Arc;

use crate::LimitExceeded;
use crate::budget::{Budget, LimitCode};

/// The slots of a chunk.
pub(super) const CHUNK: usize = 16;

/// The children of a branch.
pub(super) const FANOUT: usize = 32;

/// The bits of an object number that choose its slot in a chunk.
const CHUNK_BITS: u32 = CHUNK.trailing_zeros();

/// The bits of an object number that choose a child of a branch.
const BRANCH_BITS: u32 = FANOUT.trailing_zeros();

/// The most levels of branches that a table of 32-bit numbers has.
pub(super) const MAX_HEIGHT: u32 = (u32::BITS - CHUNK_BITS).div_ceil(BRANCH_BITS);

/// Returns how many numbers, from 0, a table of `height` levels of branches
/// covers.
pub(super) const fn capacity(height: u32) -> u64 {
    1 << (CHUNK_BITS + height * BRANCH_BITS)
}

/// Returns the levels of branches that a table needs to hold `number`.
pub(super) const fn height_for(number: u32) -> u32 {
    let mut height = 1;
    while number as u64 >= capacity(height) {
        height += 1;
    }
    height
}

/// Returns the bits of a number below those that choose a child of a branch
/// at `level`, the branches of chunks being at level 1.
const fn shift(level: u32) -> u32 {
    CHUNK_BITS + (level - 1) * BRANCH_BITS
}

/// Returns which child of a branch at `level` leads to `number`.
pub(super) const fn child(number: u32, level: u32) -> usize {
    (number >> shift(level)) as usize & (FANOUT - 1)
}

/// Returns the slot of `number` in its chunk.
pub(super) const fn slot(number: u32) -> usize {
    number as usize & (CHUNK - 1)
}

/// Returns `N` empty places.
fn empty<T, const N: usize>() -> [Option<T>; N] {
    std::array::from_fn(|_| None)
}

/// A map from object numbers to values of type `V`, whose clones share
/// structure.
pub(crate) struct Table<V> {
    root: Option<Arc<Branch<V>>>,
    /// The levels of branches, the root's included, or 0 for an empty table.
    height: u32,
}

/// A branch of the directory.
#[derive(Clone)]
enum Branch<V> {
    /// A branch of the lowest level, whose children are chunks.
    Chunks([Option<Arc<Chunk<V>>>; FANOUT]),
    /// A branch above another level of branches.
    Branches([Option<Arc<Self>>; FANOUT]),
}

/// The slots of 16 consecutive object numbers.
#[derive(Clone)]
struct Chunk<V>([Option<V>; CHUNK]);

/// The bytes of the chunks and branches that only one of two tables holds,
/// as [`Table::diff`] counts them.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(super) struct Unshared {
    /// The bytes that only the first table holds.
    pub(super) first: u64,
    /// The bytes that only the second table holds.
    pub(super) second: u64,
}

impl<V> Table<V> {
    /// The bytes that allocating a branch takes: the branch, and the two
    /// counts of its `Arc`.
    pub(super) const BRANCH_BYTES: u64 = (2 * size_of::<usize>() + size_of::<Branch<V>>()) as u64;

    /// The bytes that allocating a chunk takes: the chunk, and the two counts
    /// of its `Arc`.
    pub(super) const CHUNK_BYTES: u64 = (2 * size_of::<usize>() + size_of::<Chunk<V>>()) as u64;

    /// Returns an empty table.
    pub(crate) const fn new() -> Self {
        Self {
            root: None,
            height: 0,
        }
    }

    /// Returns the levels of branches, the root's included, or 0 for an
    /// empty table.
    #[cfg(test)]
    pub(super) const fn height(&self) -> u32 {
        self.height
    }

    /// Returns the value of `number`, if the table has one.
    pub(crate) fn get(&self, number: u32) -> Option<&V> {
        if u64::from(number) >= capacity(self.height) {
            return None;
        }
        let mut branch = self.root.as_deref()?;
        let mut level = self.height;
        loop {
            let index = child(number, level);
            match branch {
                Branch::Branches(children) => {
                    branch = children[index].as_deref()?;
                    level -= 1;
                }
                Branch::Chunks(chunks) => {
                    return chunks[index].as_deref()?.0[slot(number)].as_ref();
                }
            }
        }
    }

    /// Sets the value of `number`, and returns the value it replaces.
    ///
    /// First charges `memory_size` for the chunk and the branches that the
    /// write allocates: the branches that a number beyond the table's
    /// capacity adds above the root, the chunk and the branches on the path
    /// to the slot that another table also holds, which it copies, and those
    /// that the path lacks. A refused write changes nothing.
    pub(crate) fn insert(
        &mut self,
        number: u32,
        value: V,
        budget: &Budget,
    ) -> Result<Option<V>, LimitExceeded>
    where
        V: Clone,
    {
        let (branches, chunks) = self.allocations(number);
        budget.charge(
            LimitCode::MemorySize,
            branches * Self::BRANCH_BYTES + chunks * Self::CHUNK_BYTES,
        )?;
        let height = self.height.max(height_for(number));
        let root = match self.root.take() {
            Some(mut root) => {
                for _ in self.height..height {
                    let mut children = empty();
                    children[0] = Some(root);
                    root = Arc::new(Branch::Branches(children));
                }
                root
            }
            None => Arc::new(Self::empty_branch(height)),
        };
        self.height = height;
        let mut level = height;
        let mut branch = self.root.insert(root);
        let chunk = loop {
            let index = child(number, level);
            match Arc::make_mut(branch) {
                Branch::Branches(children) => {
                    level -= 1;
                    branch =
                        children[index].get_or_insert_with(|| Arc::new(Self::empty_branch(level)));
                }
                Branch::Chunks(chunks) => {
                    break chunks[index].get_or_insert_with(|| Arc::new(Chunk(empty())));
                }
            }
        };
        Ok(Arc::make_mut(chunk).0[slot(number)].replace(value))
    }

    /// Removes a value, pruning empty chunks and branches. A clone keeps its
    /// old value, and a refused allocation leaves this table unchanged.
    pub(crate) fn remove(
        &mut self,
        number: u32,
        budget: &Budget,
    ) -> Result<Option<V>, LimitExceeded>
    where
        V: Clone,
    {
        budget.poll()?;
        if self.get(number).is_none() {
            return Ok(None);
        }
        let (branches, chunks) = self.allocations(number);
        budget.charge(
            LimitCode::MemorySize,
            branches * Self::BRANCH_BYTES + chunks * Self::CHUNK_BYTES,
        )?;
        let Some(root) = &mut self.root else {
            return Ok(None);
        };
        let (removed, empty) = Self::remove_from(root, self.height, number);
        if empty {
            self.root = None;
            self.height = 0;
        }
        Ok(removed)
    }

    /// The table's fixed height bounds this recursion to `MAX_HEIGHT`.
    fn remove_from(branch: &mut Arc<Branch<V>>, level: u32, number: u32) -> (Option<V>, bool)
    where
        V: Clone,
    {
        let index = child(number, level);
        match Arc::make_mut(branch) {
            Branch::Branches(children) => {
                let Some(child) = &mut children[index] else {
                    return (None, false);
                };
                let (removed, empty) = Self::remove_from(child, level - 1, number);
                if empty {
                    children[index] = None;
                }
                (removed, children.iter().all(Option::is_none))
            }
            Branch::Chunks(chunks) => {
                let Some(chunk) = &mut chunks[index] else {
                    return (None, false);
                };
                let entries = &mut Arc::make_mut(chunk).0;
                let removed = entries[slot(number)].take();
                if entries.iter().all(Option::is_none) {
                    chunks[index] = None;
                }
                (removed, chunks.iter().all(Option::is_none))
            }
        }
    }

    /// Returns a branch at `level` without children.
    fn empty_branch(level: u32) -> Branch<V> {
        if level == 1 {
            Branch::Chunks(empty())
        } else {
            Branch::Branches(empty())
        }
    }

    /// Returns how many branches and chunks writing `number` allocates, as
    /// [`Table::insert`] does.
    ///
    /// A branch or a chunk that another table also holds is copied, and so
    /// is everything below a copy on the path, whose children the copy
    /// shares with the original.
    fn allocations(&self, number: u32) -> (u64, u64) {
        let height = self.height.max(height_for(number));
        let Some(root) = &self.root else {
            return (u64::from(height), 1);
        };
        if height > self.height {
            // The new top branch holds the old root as its first child, and
            // the number, which the old root cannot hold, under another: the
            // path from the top is new.
            return (u64::from(height - self.height) + u64::from(height - 1), 1);
        }
        let mut branch = root;
        let mut level = self.height;
        let mut branches = 0;
        let mut copied = false;
        loop {
            copied |= Arc::strong_count(branch) > 1;
            branches += u64::from(copied);
            let index = child(number, level);
            match &**branch {
                Branch::Branches(children) => {
                    level -= 1;
                    match &children[index] {
                        Some(next) => branch = next,
                        None => return (branches + u64::from(level), 1),
                    }
                }
                Branch::Chunks(chunks) => {
                    let copy = chunks[index]
                        .as_ref()
                        .is_none_or(|chunk| copied || Arc::strong_count(chunk) > 1);
                    return (branches, u64::from(copy));
                }
            }
        }
    }

    /// Returns the numbers that have values, ascending, with their values.
    pub(crate) fn iter(&self) -> Iter<'_, V> {
        let mut branches = [None; MAX_HEIGHT as usize];
        let mut depth = 0;
        if let Some(root) = &self.root {
            branches[0] = Some((&**root, 0, 0));
            depth = 1;
        }
        Iter {
            height: self.height,
            branches,
            depth,
            chunk: None,
        }
    }

    /// Calls `visit` with every number whose slots in `self` and `other`
    /// differ, ascending, and with both slots, and returns the bytes of the
    /// chunks and branches that only one of the tables holds.
    ///
    /// Two slots are the same when both are empty or when `same` holds for
    /// their values. The walk skips every chunk and branch that the tables
    /// share, so its time grows with what they do not share. It stops at the
    /// first error of `visit`.
    pub(super) fn diff<E>(
        &self,
        other: &Self,
        same: impl Fn(&V, &V) -> bool,
        visit: impl FnMut(u32, Option<&V>, Option<&V>) -> Result<(), E>,
    ) -> Result<Unshared, E> {
        let mut walk = Walk {
            same,
            visit,
            unshared: Unshared::default(),
        };
        let height = self.height.max(other.height);
        if height > 0 {
            walk.branches(height, 0, Node::of(self, height), Node::of(other, height))?;
        }
        Ok(walk.unshared)
    }
}

impl<V> Clone for Table<V> {
    /// Returns a table that shares every chunk and branch with this one.
    fn clone(&self) -> Self {
        Self {
            root: self.root.clone(),
            height: self.height,
        }
    }
}

impl<V> Default for Table<V> {
    fn default() -> Self {
        Self::new()
    }
}

/// The numbers of a table that have values, ascending, with their values.
pub(crate) struct Iter<'t, V> {
    height: u32,
    /// The branches on the path to the current chunk, the root first, each
    /// with the index of the child to visit next and the first number that
    /// it covers.
    branches: [Option<(&'t Branch<V>, usize, u64)>; MAX_HEIGHT as usize],
    /// How many of `branches` are on the path.
    depth: usize,
    /// The chunk being visited, with the index of the slot to visit next and
    /// its first number.
    chunk: Option<(&'t Chunk<V>, usize, u64)>,
}

impl<'t, V> Iterator for Iter<'t, V> {
    type Item = (u32, &'t V);

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if let Some((chunk, next, first)) = &mut self.chunk {
                while let Some(slot) = chunk.0.get(*next) {
                    let number = *first + *next as u64;
                    *next += 1;
                    if let Some(value) = slot {
                        // A table holds no number beyond `u32::MAX`.
                        return Some((number as u32, value));
                    }
                }
                self.chunk = None;
            }
            let top = self.depth.checked_sub(1)?;
            let (branch, next, first) = self.branches[top]?;
            if next == FANOUT {
                self.depth = top;
                continue;
            }
            self.branches[top] = Some((branch, next + 1, first));
            // The root is at the height of the table, and each branch below
            // one level lower.
            let level = self.height - top as u32;
            let first = first + ((next as u64) << shift(level));
            match branch {
                Branch::Branches(children) => {
                    if let Some(child) = &children[next] {
                        self.branches[top + 1] = Some((&**child, 0, first));
                        self.depth = top + 2;
                    }
                }
                Branch::Chunks(chunks) => {
                    if let Some(chunk) = &chunks[next] {
                        self.chunk = Some((&**chunk, 0, first));
                    }
                }
            }
        }
    }
}

/// A branch of one of the two tables that a diff compares, at the level
/// that the walk has reached.
enum Node<'t, V> {
    /// No branch: the table holds nothing there.
    Empty,
    /// A branch of the table.
    Branch(&'t Arc<Branch<V>>),
    /// The root of a table lower than the walk by this many levels: the
    /// first child of the first child, so many times, of the branches that
    /// writing a larger number would add above it.
    Lifted(&'t Arc<Branch<V>>, u32),
}

impl<V> Clone for Node<'_, V> {
    fn clone(&self) -> Self {
        *self
    }
}

impl<V> Copy for Node<'_, V> {}

impl<'t, V> Node<'t, V> {
    /// Returns the root of `table` at `level`, its height or above.
    const fn of(table: &'t Table<V>, level: u32) -> Self {
        match &table.root {
            None => Self::Empty,
            Some(root) if level == table.height => Self::Branch(root),
            Some(root) => Self::Lifted(root, level - table.height),
        }
    }

    /// Returns the child at `index` of the node, a branch above the lowest
    /// level.
    fn child(self, index: usize) -> Self {
        match self {
            Self::Lifted(root, 1) if index == 0 => Self::Branch(root),
            Self::Lifted(root, levels) if index == 0 => Self::Lifted(root, levels - 1),
            Self::Branch(branch) => match &**branch {
                Branch::Branches(children) => {
                    children[index].as_ref().map_or(Self::Empty, Self::Branch)
                }
                Branch::Chunks(_) => Self::Empty,
            },
            _ => Self::Empty,
        }
    }

    /// Returns the chunk at `index` of the node, a branch of the lowest
    /// level.
    fn chunk(self, index: usize) -> Option<&'t Arc<Chunk<V>>> {
        match self {
            Self::Branch(branch) => match &**branch {
                Branch::Chunks(chunks) => chunks[index].as_ref(),
                Branch::Branches(_) => None,
            },
            _ => None,
        }
    }
}

/// The state of a diff: how it compares values, what it calls for each
/// number whose slots differ, and the bytes it has counted.
struct Walk<S, F> {
    same: S,
    visit: F,
    unshared: Unshared,
}

impl<S, F> Walk<S, F> {
    /// Compares two branches at `level` whose first number is `first`.
    ///
    /// The recursion is as deep as the taller table is high: at most
    /// [`MAX_HEIGHT`] levels.
    fn branches<V, E>(
        &mut self,
        level: u32,
        first: u64,
        a: Node<'_, V>,
        b: Node<'_, V>,
    ) -> Result<(), E>
    where
        S: Fn(&V, &V) -> bool,
        F: FnMut(u32, Option<&V>, Option<&V>) -> Result<(), E>,
    {
        match (a, b) {
            (Node::Empty, Node::Empty) => return Ok(()),
            (Node::Branch(x), Node::Branch(y)) if Arc::ptr_eq(x, y) => return Ok(()),
            _ => {}
        }
        if matches!(a, Node::Branch(_)) {
            self.unshared.first += Table::<V>::BRANCH_BYTES;
        }
        if matches!(b, Node::Branch(_)) {
            self.unshared.second += Table::<V>::BRANCH_BYTES;
        }
        for index in 0..FANOUT {
            let first = first + ((index as u64) << shift(level));
            if level == 1 {
                self.chunks(first, a.chunk(index), b.chunk(index))?;
            } else {
                self.branches(level - 1, first, a.child(index), b.child(index))?;
            }
        }
        Ok(())
    }

    /// Compares two chunks whose first number is `first`.
    fn chunks<V, E>(
        &mut self,
        first: u64,
        a: Option<&Arc<Chunk<V>>>,
        b: Option<&Arc<Chunk<V>>>,
    ) -> Result<(), E>
    where
        S: Fn(&V, &V) -> bool,
        F: FnMut(u32, Option<&V>, Option<&V>) -> Result<(), E>,
    {
        match (a, b) {
            (None, None) => return Ok(()),
            (Some(x), Some(y)) if Arc::ptr_eq(x, y) => return Ok(()),
            _ => {}
        }
        if a.is_some() {
            self.unshared.first += Table::<V>::CHUNK_BYTES;
        }
        if b.is_some() {
            self.unshared.second += Table::<V>::CHUNK_BYTES;
        }
        for index in 0..CHUNK {
            let x = a.and_then(|chunk| chunk.0[index].as_ref());
            let y = b.and_then(|chunk| chunk.0[index].as_ref());
            let differ = match (x, y) {
                (None, None) => false,
                (Some(x), Some(y)) => !(self.same)(x, y),
                _ => true,
            };
            if differ {
                // A table holds no number beyond `u32::MAX`.
                (self.visit)((first + index as u64) as u32, x, y)?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    //! The table keeps every value written to it in every clone, visits its
    //! values in order, finds what two tables hold differently, and charges
    //! exactly the chunks and branches that a write allocates.

    use std::collections::{BTreeMap, BTreeSet};
    use std::convert::Infallible;

    use proptest::prelude::*;

    use super::*;

    /// The bytes of `branches` branches and `chunks` chunks of a table of
    /// `u64` values.
    const fn bytes(branches: u64, chunks: u64) -> u64 {
        branches * Table::<u64>::BRANCH_BYTES + chunks * Table::<u64>::CHUNK_BYTES
    }

    /// Returns the table's values in the order that it visits them.
    fn entries(table: &Table<u64>) -> Vec<(u32, u64)> {
        table
            .iter()
            .map(|(number, &value)| (number, value))
            .collect()
    }

    /// Checks that `table` holds what `model` holds, by lookup and in order.
    fn check(table: &Table<u64>, model: &BTreeMap<u32, u64>) -> Result<(), TestCaseError> {
        prop_assert_eq!(
            entries(table),
            model
                .iter()
                .map(|(&number, &value)| (number, value))
                .collect::<Vec<_>>()
        );
        for (&number, value) in model {
            prop_assert_eq!(table.get(number), Some(value));
            prop_assert_eq!(
                table.get(number.wrapping_add(1)),
                model.get(&number.wrapping_add(1))
            );
        }
        Ok(())
    }

    /// Returns the numbers whose slots differ between `a` and `b`.
    fn differing(a: &Table<u64>, b: &Table<u64>) -> (Vec<u32>, Unshared) {
        let mut numbers = Vec::new();
        let unshared = a
            .diff(
                b,
                |x, y| x == y,
                |number, x, y| {
                    assert_ne!(x, y);
                    numbers.push(number);
                    Ok::<_, Infallible>(())
                },
            )
            .unwrap();
        (numbers, unshared)
    }

    /// Object numbers of every height: dense ones, and some from the whole
    /// 32-bit range.
    fn number() -> impl Strategy<Value = u32> {
        prop_oneof![
            4 => 0..2_000_u32,
            1 => any::<u32>(),
            1 => Just(u32::MAX),
        ]
    }

    proptest! {
        #[test]
        fn removals_preserve_clones_and_match_a_map(
            operations in proptest::collection::vec((number(), any::<u64>(), any::<bool>()), 0..300),
        ) {
            let budget = Budget::default();
            let mut table = Table::<u64>::new();
            let mut model = BTreeMap::new();
            for (number, value, remove) in operations {
                let previous = table.clone();
                let old_model = model.clone();
                if remove {
                    prop_assert_eq!(table.remove(number, &budget).unwrap(), model.remove(&number));
                } else {
                    prop_assert_eq!(table.insert(number, value, &budget).unwrap(), model.insert(number, value));
                }
                check(&table, &model)?;
                check(&previous, &old_model)?;
            }
        }

        #[test]
        fn every_clone_keeps_what_it_held(
            writes in proptest::collection::vec((number(), any::<u64>()), 1..300),
            keep in proptest::collection::btree_set(0..300_usize, 0..12),
        ) {
            let budget = Budget::default();
            let mut table = Table::<u64>::new();
            let mut model = BTreeMap::new();
            let mut kept = Vec::new();
            for (step, &(number, value)) in writes.iter().enumerate() {
                prop_assert_eq!(
                    table.insert(number, value, &budget).unwrap(),
                    model.insert(number, value)
                );
                if keep.contains(&step) {
                    kept.push((table.clone(), model.clone()));
                }
            }
            for (table, model) in &kept {
                check(table, model)?;
            }
            check(&table, &model)?;
        }

        #[test]
        fn a_diff_finds_exactly_the_numbers_that_differ(
            base in proptest::collection::vec((number(), any::<u64>()), 0..200),
            left in proptest::collection::vec((number(), any::<u64>()), 0..40),
            right in proptest::collection::vec((number(), any::<u64>()), 0..40),
        ) {
            let budget = Budget::default();
            let mut common = Table::<u64>::new();
            for &(number, value) in &base {
                common.insert(number, value, &budget).unwrap();
            }
            let (mut a, mut b) = (common.clone(), common.clone());
            for &(number, value) in &left {
                a.insert(number, value, &budget).unwrap();
            }
            for &(number, value) in &right {
                b.insert(number, value, &budget).unwrap();
            }
            let a_model: BTreeMap<_, _> = a.iter().map(|(n, &v)| (n, v)).collect();
            let b_model: BTreeMap<_, _> = b.iter().map(|(n, &v)| (n, v)).collect();
            let expected: Vec<u32> = a_model
                .keys()
                .chain(b_model.keys())
                .copied()
                .collect::<BTreeSet<_>>()
                .into_iter()
                .filter(|number| a_model.get(number) != b_model.get(number))
                .collect();
            let (found, unshared) = differing(&a, &b);
            prop_assert_eq!(&found, &expected);
            let (found, reverse) = differing(&b, &a);
            prop_assert_eq!(&found, &expected);
            prop_assert_eq!((reverse.first, reverse.second), (unshared.second, unshared.first));
            prop_assert_eq!(differing(&a, &a.clone()), (Vec::new(), Unshared::default()));
        }

        #[test]
        fn a_write_charges_what_it_allocates(
            base in proptest::collection::vec((number(), any::<u64>()), 0..200),
            writes in proptest::collection::vec((number(), any::<u64>()), 1..40),
        ) {
            let budget = Budget::default();
            let mut parent = Table::<u64>::new();
            for &(number, value) in &base {
                parent.insert(number, value, &budget).unwrap();
            }
            let charged = budget.used(LimitCode::MemorySize);
            let (_, unshared) = differing(&Table::new(), &parent);
            prop_assert_eq!(charged, unshared.second);
            // The writes into a clone allocate the chunks and branches that
            // only the clone holds: its copies, and what it adds.
            let mut table = parent.clone();
            for &(number, value) in &writes {
                table.insert(number, value, &budget).unwrap();
            }
            let (_, unshared) = differing(&parent, &table);
            prop_assert_eq!(budget.used(LimitCode::MemorySize) - charged, unshared.second);
        }
    }

    #[test]
    fn refused_removal_leaves_the_table_and_its_clone_unchanged() {
        let budget = Budget::default();
        let mut table = Table::<u64>::new();
        table.insert(123, 7, &budget).unwrap();
        let saved = table.clone();
        let limited = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
        assert!(table.remove(123, &limited).is_err());
        assert_eq!(table.get(123), Some(&7));
        assert_eq!(saved.get(123), Some(&7));
        assert_eq!(table.remove(123, &budget).unwrap(), Some(7));
        assert!(table.root.is_none());
        assert_eq!(table.height(), 0);
        assert_eq!(saved.get(123), Some(&7));
    }

    #[test]
    fn one_write_copies_one_chunk_and_the_branches_above_it() {
        for (numbers, height) in [
            ((1..=10_000).collect::<Vec<u32>>(), 2),
            ((1..=100_000).collect(), 3),
            (vec![1, u32::MAX], 6),
        ] {
            let budget = Budget::default();
            let mut table = Table::<u64>::new();
            for &number in &numbers {
                table.insert(number, 0, &budget).unwrap();
            }
            assert_eq!(table.height(), height);
            for &number in numbers.iter().step_by(97).chain(numbers.last()) {
                let mut clone = table.clone();
                let budget = Budget::default();
                assert_eq!(clone.insert(number, 1, &budget), Ok(Some(0)));
                assert_eq!(
                    budget.used(LimitCode::MemorySize),
                    bytes(u64::from(height), 1),
                    "{number}"
                );
                // A second write into the same chunk writes in place.
                clone.insert(number ^ 1, 1, &budget).unwrap();
                assert_eq!(
                    budget.used(LimitCode::MemorySize),
                    bytes(u64::from(height), 1)
                );
            }
        }
        // Six levels are the most: one chunk and six branches.
        assert_eq!(MAX_HEIGHT, 6);
        assert_eq!(height_for(u32::MAX), MAX_HEIGHT);
        assert_eq!(bytes(6, 1), 1_952);
    }

    #[test]
    fn a_larger_number_adds_branches_above_the_root() {
        let budget = Budget::default();
        let mut table = Table::<u64>::new();
        table.insert(1, 1, &budget).unwrap();
        assert_eq!(
            (table.height(), budget.used(LimitCode::MemorySize)),
            (1, bytes(1, 1))
        );
        let parent = table.clone();
        // A number beyond the capacity adds two levels above the root, and
        // its path leaves the old root's range at the top.
        let budget = Budget::default();
        table.insert(20_000, 2, &budget).unwrap();
        assert_eq!(
            (table.height(), budget.used(LimitCode::MemorySize)),
            (3, bytes(4, 1))
        );
        // The old root, now below the new branches, is still the parent's
        // too: writing under it copies it and its chunk.
        let budget = Budget::default();
        table.insert(2, 3, &budget).unwrap();
        assert_eq!(budget.used(LimitCode::MemorySize), bytes(1, 1));
        assert_eq!(entries(&table), [(1, 1), (2, 3), (20_000, 2)]);
        assert_eq!(entries(&parent), [(1, 1)]);
        assert_eq!(differing(&parent, &table).0, [2, 20_000]);
    }

    #[test]
    fn a_refused_write_changes_nothing() {
        let budget = Budget::default();
        let mut parent = Table::<u64>::new();
        for number in 0..1_000 {
            parent.insert(number, u64::from(number), &budget).unwrap();
        }
        let mut table = parent.clone();
        let tight = Budget::default().with_limit(LimitCode::MemorySize, Some(bytes(2, 1) - 1));
        let refusal = table.insert(5, 0, &tight).unwrap_err();
        assert_eq!(refusal.code(), LimitCode::MemorySize);
        assert_eq!(tight.used(LimitCode::MemorySize), 0);
        assert_eq!(
            differing(&parent, &table),
            (Vec::new(), Unshared::default())
        );
        let exact = Budget::default().with_limit(LimitCode::MemorySize, Some(bytes(2, 1)));
        assert_eq!(table.insert(5, 0, &exact), Ok(Some(5)));
        assert_eq!(differing(&parent, &table).0, [5]);
    }

    #[test]
    fn extreme_numbers_keep_their_order() {
        let budget = Budget::default();
        let mut table = Table::<u64>::new();
        for (value, number) in [u32::MAX, 0, 1 << 31, 15, 16, 511, 512]
            .into_iter()
            .enumerate()
        {
            table.insert(number, value as u64, &budget).unwrap();
        }
        assert_eq!(
            table.iter().map(|(number, _)| number).collect::<Vec<_>>(),
            [0, 15, 16, 511, 512, 1 << 31, u32::MAX]
        );
        assert_eq!(table.get(u32::MAX - 1), None);
        assert_eq!(table.get(u32::MAX), Some(&0));
        assert_eq!(Table::<u64>::new().get(0), None);
        assert_eq!(Table::<u64>::new().iter().next(), None);
    }
}
