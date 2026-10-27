//! The base that a document's versions start from, and the values parsed
//! from it (DESIGN.md section 4.1).

use std::collections::BTreeMap;
use std::fmt;
use std::sync::{Arc, OnceLock};

use super::Object;
use super::table::{CHUNK, FANOUT, capacity, child, height_for, slot};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::index::Index;
use crate::value::{Bytes, Dict, Reference, Value};
use crate::{LimitExceeded, Result};

/// The objects that a document's versions start from: the base of DESIGN.md
/// section 4.1.
///
/// A version reads through this interface every object that no transaction
/// wrote, and parses the bytes that it returns. The cross-reference index of
/// a file is connected by [`FileBase`](crate::save::FileBase), and
/// [`MemoryBase`] holds objects in memory. A base never changes,
/// and several threads may read it at once.
pub trait Base: fmt::Debug + Send + Sync {
    /// Returns the generation of object `number`, if the base has the
    /// object in use.
    fn generation(&self, number: u32) -> Option<u16>;

    /// Returns the generation to use if a free number is reused. Generation
    /// 65535 is retired and must never be reused. Returns `None` for an
    /// in-use number or a number absent from the base.
    fn free_generation(&self, number: u32) -> Option<u16>;

    /// Returns the bytes that the value of the object that `reference`
    /// names begins with, or `None` if the base has no such object.
    ///
    /// Refuses as reading the base refuses, such as with the limits of
    /// decoded streams for a member of an object stream.
    fn object(&self, reference: Reference, budget: &Budget) -> Result<Option<Bytes>>;

    /// Returns the trailer.
    fn trailer(&self) -> &Dict;

    /// Returns the first object number above every number that the base
    /// lists, in use or free: where new objects take their numbers.
    fn next_number(&self) -> u64;

    /// Appends the numbers listed by the base, in use or free. A number may
    /// occur more than once; full save sorts and deduplicates them. Growth
    /// and polling use the output's budget. Incremental save never calls it.
    fn numbers(&self, out: &mut BudgetVec<'_, u32>) -> Result<()>;

    /// Returns the original file index, when this base came from a file.
    /// Incremental save uses its bytes, revision offsets, and repair state.
    fn file(&self) -> Option<&Index> {
        None
    }
}

/// A base held in memory: the bytes of each object's value by object number,
/// and a trailer.
///
/// A new document starts from an empty one.
#[derive(Debug, Default)]
pub struct MemoryBase {
    objects: BTreeMap<u32, (u16, Option<Bytes>)>,
    trailer: Dict,
}

impl MemoryBase {
    /// Returns a base without objects, with `trailer`.
    #[must_use]
    pub const fn new(trailer: Dict) -> Self {
        Self {
            objects: BTreeMap::new(),
            trailer,
        }
    }

    /// Adds the object that `reference` names, whose value `bytes` begin
    /// with, in place of any object of the same number.
    pub fn insert(&mut self, reference: Reference, bytes: Bytes) {
        #[expect(
            clippy::disallowed_methods,
            reason = "the caller adds each entry; the map grows with explicit calls, not parsed input"
        )]
        self.objects
            .insert(reference.number(), (reference.generation(), Some(bytes)));
    }

    /// Adds a free entry with the generation of its next use. A generation
    /// of 65535 permanently retires the number.
    pub fn insert_free(&mut self, reference: Reference) {
        #[expect(
            clippy::disallowed_methods,
            reason = "the caller adds each entry; the map grows with explicit calls, not parsed input"
        )]
        self.objects
            .insert(reference.number(), (reference.generation(), None));
    }
}

impl Base for MemoryBase {
    fn generation(&self, number: u32) -> Option<u16> {
        self.objects
            .get(&number)
            .and_then(|(generation, bytes)| bytes.as_ref().map(|_| *generation))
    }

    fn free_generation(&self, number: u32) -> Option<u16> {
        self.objects
            .get(&number)
            .and_then(|(generation, bytes)| bytes.is_none().then_some(*generation))
    }

    fn object(&self, reference: Reference, _budget: &Budget) -> Result<Option<Bytes>> {
        Ok(self
            .objects
            .get(&reference.number())
            .filter(|(generation, _)| *generation == reference.generation())
            .and_then(|(_, bytes)| bytes.clone()))
    }

    fn trailer(&self) -> &Dict {
        &self.trailer
    }

    fn next_number(&self) -> u64 {
        self.objects
            .last_key_value()
            .map_or(1, |(&number, _)| u64::from(number) + 1)
    }

    fn numbers(&self, out: &mut BudgetVec<'_, u32>) -> Result<()> {
        for &number in self.objects.keys() {
            out.budget().poll()?;
            out.try_push(number)?;
        }
        Ok(())
    }
}

/// What the versions of one document share: the base, and the values parsed
/// from it.
#[derive(Debug)]
pub(super) struct Origin {
    base: Arc<dyn Base>,
    cache: Cache,
}

impl Origin {
    /// Returns the origin of a document over `base`, nothing parsed yet.
    pub(super) fn new(base: Arc<dyn Base>) -> Self {
        let cache = Cache::new(base.next_number());
        Self { base, cache }
    }

    /// Returns the base.
    pub(super) const fn base(&self) -> &Arc<dyn Base> {
        &self.base
    }

    /// Returns object `number` of the base, which its first read parses and
    /// every later read shares.
    ///
    /// A stream whose `/Length` refers to an object reads the data that the
    /// base's object gives ([`Value::parse_with_lengths`]). Refuses as
    /// [`Base::object`] and [`Value::parse`] do. A refusal or an error is not
    /// kept, so a later read tries again.
    pub(super) fn object(&self, number: u32, budget: &Budget) -> Result<Option<Object>> {
        let Some(generation) = self.base.generation(number) else {
            return Ok(None);
        };
        if let Some(value) = self.cache.get(number) {
            return Ok(Some(Object { generation, value }));
        }
        let context = || format!("object {number} {generation}");
        let reference = Reference::new(number, generation);
        let Some(bytes) = self
            .base
            .object(reference, budget)
            .map_err(|error| error.context(context()))?
        else {
            return Ok(None);
        };
        // The length of a stream is read from the base, whose bytes hold
        // the stream, whatever a version wrote since.
        let fetch = |reference| self.base.object(reference, budget);
        let value = Value::parse_with_lengths(&bytes, budget, &fetch)
            .map_err(|error| error.context(context()))?;
        let value = self.cache.keep(number, value, budget)?;
        Ok(Some(Object { generation, value }))
    }
}

/// The bytes that sharing a value takes: the value, and the two counts of
/// its `Arc`.
pub(super) const VALUE_BYTES: u64 = (2 * size_of::<usize>() + size_of::<Value>()) as u64;

/// The values parsed from a base, by object number.
///
/// The cache is a tree like a version's table, as high as the base's numbers
/// need. The first read that needs a branch, a leaf, or a value sets it, and
/// it never changes after, so readers on several threads share the cache
/// without locks.
struct Cache {
    /// The levels of branches.
    height: u32,
    root: OnceLock<Box<CacheBranch>>,
}

/// A branch of the cache.
enum CacheBranch {
    /// A branch of the lowest level, whose children are leaves.
    Leaves([OnceLock<Box<Leaf>>; FANOUT]),
    /// A branch above another level of branches.
    Branches([OnceLock<Box<Self>>; FANOUT]),
}

/// The values of 16 consecutive object numbers.
struct Leaf([OnceLock<Arc<Value>>; CHUNK]);

impl Cache {
    /// Returns an empty cache for a base whose numbers are below
    /// `next_number`.
    fn new(next_number: u64) -> Self {
        let last = u32::try_from(next_number.saturating_sub(1)).unwrap_or(u32::MAX);
        Self {
            height: height_for(last),
            root: OnceLock::new(),
        }
    }

    /// Returns the value of `number`, if a read has kept it.
    fn get(&self, number: u32) -> Option<Arc<Value>> {
        if u64::from(number) >= capacity(self.height) {
            return None;
        }
        let mut branch = self.root.get()?;
        let mut level = self.height;
        loop {
            let index = child(number, level);
            match &**branch {
                CacheBranch::Branches(children) => {
                    branch = children[index].get()?;
                    level -= 1;
                }
                CacheBranch::Leaves(leaves) => {
                    return leaves[index].get()?.0[slot(number)].get().cloned();
                }
            }
        }
    }

    /// Keeps `value` as the value of `number`, unless a read on another
    /// thread kept one first, and returns what is kept.
    ///
    /// Charges `memory_size` for the value's `Arc` and the branches and the
    /// leaf that the cache allocates for it. A number beyond the base's is
    /// not kept.
    fn keep(
        &self,
        number: u32,
        value: Value,
        budget: &Budget,
    ) -> Result<Arc<Value>, LimitExceeded> {
        budget.charge(LimitCode::MemorySize, VALUE_BYTES)?;
        let value = Arc::new(value);
        if u64::from(number) >= capacity(self.height) {
            return Ok(value);
        }
        let mut branch = set_once(&self.root, budget, || empty_branch(self.height))?;
        let mut level = self.height;
        let leaf = loop {
            let index = child(number, level);
            match branch {
                CacheBranch::Branches(children) => {
                    level -= 1;
                    branch = set_once(&children[index], budget, || empty_branch(level))?;
                }
                CacheBranch::Leaves(leaves) => {
                    break set_once(&leaves[index], budget, || {
                        Leaf(std::array::from_fn(|_| OnceLock::new()))
                    })?;
                }
            }
        };
        Ok(Arc::clone(leaf.0[slot(number)].get_or_init(|| value)))
    }
}

impl fmt::Debug for Cache {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Cache")
            .field("height", &self.height)
            .finish_non_exhaustive()
    }
}

/// Returns an empty branch of the cache at `level`.
fn empty_branch(level: u32) -> CacheBranch {
    if level == 1 {
        CacheBranch::Leaves(std::array::from_fn(|_| OnceLock::new()))
    } else {
        CacheBranch::Branches(std::array::from_fn(|_| OnceLock::new()))
    }
}

/// Returns what `cell` holds, after setting it to a box of what `make`
/// returns if it holds nothing, which charges the box to `budget` first.
fn set_once<'c, T>(
    cell: &'c OnceLock<Box<T>>,
    budget: &Budget,
    make: impl FnOnce() -> T,
) -> Result<&'c T, LimitExceeded> {
    if let Some(held) = cell.get() {
        return Ok(held);
    }
    let bytes = size_of::<T>() as u64;
    budget.charge(LimitCode::MemorySize, bytes)?;
    let mut made = false;
    let held = cell.get_or_init(|| {
        made = true;
        Box::new(make())
    });
    // Another thread set the cell first.
    if !made {
        budget.release(LimitCode::MemorySize, bytes);
    }
    Ok(held)
}
