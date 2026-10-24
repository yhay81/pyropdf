//! Copy-on-write chunks of a flat table indexed by object number.
//!
//! The table is split into chunks of `C` slots, and a radix directory of
//! `F`-way branches reaches them, so that a write copies one chunk and the
//! branches above it rather than the whole directory. Cloning a table shares
//! everything; `Arc::make_mut` copies a chunk or a branch only while another
//! table still holds it. Keys are implicit in positions, and lookups index
//! arrays without hashing or comparing.

use std::sync::Arc;

#[derive(Clone)]
pub struct ChunkTable<V, const C: usize, const F: usize> {
    root: Option<Arc<Branch<V, C, F>>>,
    /// The levels of branches above the chunks, root included.
    height: u32,
    len: usize,
}

#[derive(Clone)]
enum Branch<V, const C: usize, const F: usize> {
    /// The lowest level, whose children are chunks.
    Chunks([Option<Arc<Chunk<V, C>>>; F]),
    Branches([Option<Arc<Branch<V, C, F>>>; F]),
}

#[derive(Clone)]
struct Chunk<V, const C: usize>([Option<V>; C]);

fn empty<T, const N: usize>() -> [Option<T>; N] {
    std::array::from_fn(|_| None)
}

impl<V: Clone, const C: usize, const F: usize> Default for ChunkTable<V, C, F> {
    fn default() -> Self {
        Self::new()
    }
}

impl<V: Clone, const C: usize, const F: usize> ChunkTable<V, C, F> {
    const CHUNK_BITS: u32 = {
        assert!(C.is_power_of_two() && C >= 2, "C must be a power of two");
        C.trailing_zeros()
    };
    const BRANCH_BITS: u32 = {
        assert!(F.is_power_of_two() && F >= 2, "F must be a power of two");
        F.trailing_zeros()
    };

    pub const fn new() -> Self {
        Self {
            root: None,
            height: 0,
            len: 0,
        }
    }

    pub const fn len(&self) -> usize {
        self.len
    }

    pub const fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// The bits of a key below the child index of a branch at `level`, the
    /// branches of chunks being level 1.
    const fn shift(level: u32) -> u32 {
        Self::CHUNK_BITS + (level - 1) * Self::BRANCH_BITS
    }

    /// The first key that a table of `height` levels cannot hold.
    const fn limit(height: u32) -> u64 {
        1 << (Self::shift(height) + Self::BRANCH_BITS)
    }

    const fn index(key: u32, level: u32) -> usize {
        (key as u64 >> Self::shift(level)) as usize & (F - 1)
    }

    pub fn get(&self, key: u32) -> Option<&V> {
        let mut branch = self.root.as_deref()?;
        if u64::from(key) >= Self::limit(self.height) {
            return None;
        }
        let mut level = self.height;
        loop {
            let index = Self::index(key, level);
            match branch {
                Branch::Branches(children) => {
                    branch = children[index].as_deref()?;
                    level -= 1;
                }
                Branch::Chunks(chunks) => {
                    return chunks[index].as_deref()?.0[key as usize & (C - 1)].as_ref();
                }
            }
        }
    }

    /// Writes in place, after copying the chunk and the branches on the
    /// key's path that another table shares.
    pub fn insert(&mut self, key: u32, value: V) -> Option<V> {
        while self.root.is_none() || u64::from(key) >= Self::limit(self.height) {
            let below = self.root.take();
            let root = match below {
                None => Branch::Chunks(empty()),
                Some(below) => {
                    let mut children = empty();
                    children[0] = Some(below);
                    Branch::Branches(children)
                }
            };
            self.root = Some(Arc::new(root));
            self.height += 1;
        }
        let mut branch = self.root.as_mut().expect("the loop above sets a root");
        let mut level = self.height;
        let chunk = loop {
            let index = Self::index(key, level);
            match Arc::make_mut(branch) {
                Branch::Branches(children) => {
                    level -= 1;
                    branch = children[index].get_or_insert_with(|| {
                        Arc::new(if level == 1 {
                            Branch::Chunks(empty())
                        } else {
                            Branch::Branches(empty())
                        })
                    });
                }
                Branch::Chunks(chunks) => {
                    break chunks[index].get_or_insert_with(|| Arc::new(Chunk(empty())));
                }
            }
        };
        let old = Arc::make_mut(chunk).0[key as usize & (C - 1)].replace(value);
        if old.is_none() {
            self.len += 1;
        }
        old
    }

    /// Visits every entry in key order. The recursion is bounded by the
    /// height, which 32-bit keys keep below 32 levels.
    pub fn for_each(&self, mut f: impl FnMut(u32, &V)) {
        if let Some(root) = &self.root {
            Self::walk(root, self.height, 0, &mut f);
        }
    }

    fn walk(branch: &Branch<V, C, F>, level: u32, first: u64, f: &mut impl FnMut(u32, &V)) {
        let span = 1 << Self::shift(level);
        match branch {
            Branch::Branches(children) => {
                for (i, child) in children.iter().enumerate() {
                    if let Some(child) = child {
                        Self::walk(child, level - 1, first + i as u64 * span, f);
                    }
                }
            }
            Branch::Chunks(chunks) => {
                for (i, chunk) in chunks.iter().enumerate() {
                    if let Some(chunk) = chunk {
                        let base = first + i as u64 * span;
                        for (j, slot) in chunk.0.iter().enumerate() {
                            if let Some(value) = slot {
                                f((base + j as u64) as u32, value);
                            }
                        }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::ChunkTable;
    use crate::rng::Rng;
    use std::collections::BTreeMap;

    fn check<const C: usize, const F: usize>(
        table: &ChunkTable<u64, C, F>,
        model: &BTreeMap<u32, u64>,
    ) {
        assert_eq!(table.len(), model.len());
        let mut entries = Vec::new();
        table.for_each(|key, value| entries.push((key, *value)));
        assert!(
            entries
                .iter()
                .copied()
                .eq(model.iter().map(|(k, v)| (*k, *v)))
        );
        for (key, value) in model {
            assert_eq!(table.get(*key), Some(value));
        }
    }

    /// Random writes over versions that stay alive: every version keeps
    /// what it held when it was cloned.
    fn versions<const C: usize, const F: usize>(range: u64) {
        let mut rng = Rng::new(range);
        let mut table = ChunkTable::<u64, C, F>::new();
        let mut model = BTreeMap::new();
        let mut kept = Vec::new();
        for step in 0..4_000u64 {
            let key = rng.below(range) as u32;
            assert_eq!(table.insert(key, step), model.insert(key, step));
            assert_eq!(
                table.get(key.wrapping_add(1)),
                model.get(&key.wrapping_add(1))
            );
            if step % 97 == 0 {
                kept.push((table.clone(), model.clone()));
            }
        }
        for (table, model) in &kept {
            check(table, model);
        }
        check(&table, &model);
    }

    #[test]
    fn dense_keys() {
        versions::<4, 4>(5_000);
        versions::<64, 64>(5_000);
    }

    #[test]
    fn sparse_keys() {
        versions::<4, 4>(u64::from(u32::MAX) + 1);
        versions::<64, 64>(u64::from(u32::MAX) + 1);
    }

    #[test]
    fn extreme_keys() {
        let mut table = ChunkTable::<u64, 64, 64>::new();
        for (step, key) in [u32::MAX, 0, 1 << 31, 63, 64].into_iter().enumerate() {
            table.insert(key, step as u64);
        }
        let mut keys = Vec::new();
        table.for_each(|key, _| keys.push(key));
        assert_eq!(keys, [0, 63, 64, 1 << 31, u32::MAX]);
        assert_eq!(table.get(u32::MAX - 1), None);
    }
}
