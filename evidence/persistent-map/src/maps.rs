//! The candidates behind one interface. Every map is `Send + Sync`, which the
//! trait requires, so a candidate that is not fails to compile.

use std::hash::{BuildHasher, Hasher};

use crate::chunks::ChunkTable;
use crate::flat::FlatChunks;
use crate::slot::Slot;

pub trait Map: Clone + Send + Sync + 'static {
    fn name() -> String;
    fn new() -> Self;
    /// Writes in place, after copying the nodes on the key's path that
    /// another version shares.
    fn insert(&mut self, key: u32, value: Slot);
    fn get(&self, key: u32) -> Option<&Slot>;
    fn len(&self) -> usize;
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
    /// Visits every entry in object-number order, which saving needs.
    fn for_each_ordered(&self, f: impl FnMut(u32, &Slot));
}

/// The hash of an object number: the finalizer of MurmurHash3, a bijection
/// of 32-bit numbers. Two object numbers never collide in the 32 bits that
/// im and imbl use, nor in the 64 that rpds uses, so a fixed function opens
/// no collision attack; adversarial numbers can only deepen a trie up to its
/// bounded height.
#[derive(Clone, Copy, Default)]
pub struct Mix;

pub struct MixHasher(u64);

impl BuildHasher for Mix {
    type Hasher = MixHasher;

    fn build_hasher(&self) -> MixHasher {
        MixHasher(0)
    }
}

impl Hasher for MixHasher {
    fn write(&mut self, bytes: &[u8]) {
        for byte in bytes {
            self.0 = self.0.rotate_left(8) ^ u64::from(*byte);
        }
    }

    fn write_u32(&mut self, n: u32) {
        self.0 = u64::from(n);
    }

    fn finish(&self) -> u64 {
        let mut h = self.0 as u32;
        h ^= h >> 16;
        h = h.wrapping_mul(0x85eb_ca6b);
        h ^= h >> 13;
        h = h.wrapping_mul(0xc2b2_ae35);
        h ^= h >> 16;
        u64::from(h) | self.0 << 32
    }
}

/// Visits a hash map's entries in object-number order by sorting them.
fn sorted<'a>(
    len: usize,
    entries: impl Iterator<Item = (&'a u32, &'a Slot)>,
    mut f: impl FnMut(u32, &Slot),
) {
    let mut sorted = Vec::with_capacity(len);
    sorted.extend(entries.map(|(key, value)| (*key, value)));
    sorted.sort_unstable_by_key(|entry: &(u32, &Slot)| entry.0);
    for (key, value) in sorted {
        f(key, value);
    }
}

#[derive(Clone)]
pub struct RpdsHash(rpds::HashTrieMapSync<u32, Slot, Mix>);

impl Map for RpdsHash {
    fn name() -> String {
        "rpds HashTrieMap".into()
    }

    fn new() -> Self {
        Self(rpds::HashTrieMap::new_with_hasher_and_ptr_kind(Mix))
    }

    fn insert(&mut self, key: u32, value: Slot) {
        self.0.insert_mut(key, value);
    }

    fn get(&self, key: u32) -> Option<&Slot> {
        self.0.get(&key)
    }

    fn len(&self) -> usize {
        self.0.size()
    }

    fn for_each_ordered(&self, f: impl FnMut(u32, &Slot)) {
        sorted(self.0.size(), self.0.iter(), f);
    }
}

#[derive(Clone)]
pub struct RpdsTree(rpds::RedBlackTreeMapSync<u32, Slot>);

impl Map for RpdsTree {
    fn name() -> String {
        "rpds RedBlackTreeMap".into()
    }

    fn new() -> Self {
        Self(rpds::RedBlackTreeMap::new_sync())
    }

    fn insert(&mut self, key: u32, value: Slot) {
        self.0.insert_mut(key, value);
    }

    fn get(&self, key: u32) -> Option<&Slot> {
        self.0.get(&key)
    }

    fn len(&self) -> usize {
        self.0.size()
    }

    fn for_each_ordered(&self, mut f: impl FnMut(u32, &Slot)) {
        for (key, value) in self.0.iter() {
            f(*key, value);
        }
    }
}

#[derive(Clone)]
pub struct ImHash(im::HashMap<u32, Slot, Mix>);

impl Map for ImHash {
    fn name() -> String {
        "im HashMap".into()
    }

    fn new() -> Self {
        Self(im::HashMap::with_hasher(Mix))
    }

    fn insert(&mut self, key: u32, value: Slot) {
        self.0.insert(key, value);
    }

    fn get(&self, key: u32) -> Option<&Slot> {
        self.0.get(&key)
    }

    fn len(&self) -> usize {
        self.0.len()
    }

    fn for_each_ordered(&self, f: impl FnMut(u32, &Slot)) {
        sorted(self.0.len(), self.0.iter(), f);
    }
}

#[derive(Clone)]
pub struct ImOrd(im::OrdMap<u32, Slot>);

impl Map for ImOrd {
    fn name() -> String {
        "im OrdMap".into()
    }

    fn new() -> Self {
        Self(im::OrdMap::new())
    }

    fn insert(&mut self, key: u32, value: Slot) {
        self.0.insert(key, value);
    }

    fn get(&self, key: u32) -> Option<&Slot> {
        self.0.get(&key)
    }

    fn len(&self) -> usize {
        self.0.len()
    }

    fn for_each_ordered(&self, mut f: impl FnMut(u32, &Slot)) {
        for (key, value) in &self.0 {
            f(*key, value);
        }
    }
}

#[derive(Clone)]
pub struct ImblHash(imbl::GenericHashMap<u32, Slot, Mix, imbl::shared_ptr::DefaultSharedPtr>);

impl Map for ImblHash {
    fn name() -> String {
        "imbl HashMap".into()
    }

    fn new() -> Self {
        Self(imbl::GenericHashMap::with_hasher(Mix))
    }

    fn insert(&mut self, key: u32, value: Slot) {
        self.0.insert(key, value);
    }

    fn get(&self, key: u32) -> Option<&Slot> {
        self.0.get(&key)
    }

    fn len(&self) -> usize {
        self.0.len()
    }

    fn for_each_ordered(&self, f: impl FnMut(u32, &Slot)) {
        sorted(self.0.len(), self.0.iter(), f);
    }
}

#[derive(Clone)]
pub struct ImblOrd(imbl::OrdMap<u32, Slot>);

impl Map for ImblOrd {
    fn name() -> String {
        "imbl OrdMap".into()
    }

    fn new() -> Self {
        Self(imbl::OrdMap::new())
    }

    fn insert(&mut self, key: u32, value: Slot) {
        self.0.insert(key, value);
    }

    fn get(&self, key: u32) -> Option<&Slot> {
        self.0.get(&key)
    }

    fn len(&self) -> usize {
        self.0.len()
    }

    fn for_each_ordered(&self, mut f: impl FnMut(u32, &Slot)) {
        for (key, value) in &self.0 {
            f(*key, value);
        }
    }
}

impl<const C: usize, const F: usize> Map for ChunkTable<Slot, C, F> {
    fn name() -> String {
        format!("chunks {C}x{F}")
    }

    fn new() -> Self {
        Self::new()
    }

    fn insert(&mut self, key: u32, value: Slot) {
        self.insert(key, value);
    }

    fn get(&self, key: u32) -> Option<&Slot> {
        self.get(key)
    }

    fn len(&self) -> usize {
        self.len()
    }

    fn for_each_ordered(&self, f: impl FnMut(u32, &Slot)) {
        self.for_each(f);
    }
}

impl<const C: usize> Map for FlatChunks<Slot, C> {
    fn name() -> String {
        format!("flat directory {C}")
    }

    fn new() -> Self {
        Self::default()
    }

    fn insert(&mut self, key: u32, value: Slot) {
        self.insert(key, value);
    }

    fn get(&self, key: u32) -> Option<&Slot> {
        self.get(key)
    }

    fn len(&self) -> usize {
        self.len()
    }

    fn for_each_ordered(&self, f: impl FnMut(u32, &Slot)) {
        self.for_each(f);
    }
}

/// A plain vector indexed by object number, which a clone copies whole: the
/// baseline of the code-size probe, not a candidate.
#[derive(Clone, Default)]
pub struct VecTable(Vec<Option<Slot>>);

impl Map for VecTable {
    fn name() -> String {
        "Vec".into()
    }

    fn new() -> Self {
        Self::default()
    }

    fn insert(&mut self, key: u32, value: Slot) {
        let key = key as usize;
        if self.0.len() <= key {
            self.0.resize(key + 1, None);
        }
        self.0[key] = Some(value);
    }

    fn get(&self, key: u32) -> Option<&Slot> {
        self.0.get(key as usize)?.as_ref()
    }

    fn len(&self) -> usize {
        self.0.iter().filter(|slot| slot.is_some()).count()
    }

    fn for_each_ordered(&self, mut f: impl FnMut(u32, &Slot)) {
        for (key, slot) in self.0.iter().enumerate() {
            if let Some(value) = slot {
                f(key as u32, value);
            }
        }
    }
}
