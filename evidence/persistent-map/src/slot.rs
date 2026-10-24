//! What a version maps an object number to (DESIGN.md section 4.1).

use std::sync::Arc;

/// A written object: its serialized bytes, shared by every version that
/// holds it. The objects of the benchmark hold no bytes, so that only the
/// maps allocate while they are measured.
pub struct Written {
    pub generation: u16,
    pub bytes: Box<[u8]>,
}

/// A written object or a free entry.
#[derive(Clone)]
pub enum Slot {
    Written(Arc<Written>),
    Free(u16),
}

// A slot and an empty slot of a table take two words each.
const _: () = assert!(
    size_of::<Slot>() == 2 * size_of::<usize>() && size_of::<Option<Slot>>() == size_of::<Slot>()
);

impl Slot {
    pub fn written(generation: u16) -> Self {
        Self::Written(Arc::new(Written {
            generation,
            bytes: Box::default(),
        }))
    }

    /// A number that identifies the slot without reading the object.
    pub fn tag(&self) -> u64 {
        match self {
            Self::Written(object) => Arc::as_ptr(object).addr() as u64,
            Self::Free(generation) => u64::from(*generation),
        }
    }
}
