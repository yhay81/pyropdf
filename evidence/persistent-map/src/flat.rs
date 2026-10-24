//! Copy-on-write chunks under one flat directory. The first write of every
//! transaction copies the whole directory, one pointer per chunk: the cost
//! that the radix directory of `chunks.rs` avoids.

use std::sync::Arc;

type Chunk<V, const C: usize> = Arc<[Option<V>; C]>;

#[derive(Clone)]
pub struct FlatChunks<V, const C: usize> {
    chunks: Arc<Vec<Option<Chunk<V, C>>>>,
    len: usize,
}

impl<V: Clone, const C: usize> Default for FlatChunks<V, C> {
    fn default() -> Self {
        Self {
            chunks: Arc::default(),
            len: 0,
        }
    }
}

impl<V: Clone, const C: usize> FlatChunks<V, C> {
    pub const fn len(&self) -> usize {
        self.len
    }

    pub const fn is_empty(&self) -> bool {
        self.len == 0
    }

    pub fn get(&self, key: u32) -> Option<&V> {
        let key = key as usize;
        self.chunks.get(key / C)?.as_deref()?[key % C].as_ref()
    }

    pub fn insert(&mut self, key: u32, value: V) -> Option<V> {
        let key = key as usize;
        let chunks = Arc::make_mut(&mut self.chunks);
        if chunks.len() <= key / C {
            chunks.resize(key / C + 1, None);
        }
        let chunk = chunks[key / C].get_or_insert_with(|| Arc::new(std::array::from_fn(|_| None)));
        let old = Arc::make_mut(chunk)[key % C].replace(value);
        if old.is_none() {
            self.len += 1;
        }
        old
    }

    pub fn for_each(&self, mut f: impl FnMut(u32, &V)) {
        for (i, chunk) in self.chunks.iter().enumerate() {
            if let Some(chunk) = chunk {
                for (j, slot) in chunk.iter().enumerate() {
                    if let Some(value) = slot {
                        f((i * C + j) as u32, value);
                    }
                }
            }
        }
    }
}
