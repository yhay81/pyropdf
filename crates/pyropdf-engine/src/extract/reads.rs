//! What interpreting a page may read, read through a recorder.
//!
//! hayro reads the bridged bytes of a version rather than the store, so the
//! store does not see what hayro reads (I1, #14). Until it does, a page's
//! derived results carry the page's closure as their read set (DESIGN.md
//! section 5): a superset of what hayro reads, so that a change it misses
//! cannot change the result.

use pyropdf_store::budget::BudgetVec;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::Page;
use pyropdf_store::value::{Dict, Value};
use pyropdf_store::version::Object;
use pyropdf_store::{ErrorKind, Result};

/// The attributes that a page inherits from its ancestors.
const INHERITABLE: [&str; 4] = ["Resources", "MediaBox", "CropBox", "Rotate"];

/// Reads through `reads` every object that interpreting `page` may read:
///
/// - the trailer and the catalog, and what the catalog's `/OCProperties`
///   reaches, since hayro hides optional content by it;
/// - the page's ancestors, and what their inheritable attributes reach;
/// - the page, and what its dictionary reaches but `/Parent`.
///
/// The walk records another page, a page-tree node, or the catalog that it
/// reaches, such as the target of a link, without entering it. Each object
/// is entered at most once, so the walk is bounded by the version's
/// objects, and its marks and worklists grow through the budget.
pub fn read_page(reads: &mut Reads<'_>, page: &Page) -> Result<()> {
    let budget = reads.budget();
    let words = usize::try_from(reads.version().next_number().div_ceil(64)).unwrap_or(usize::MAX);
    let mut seen = BudgetVec::new(budget);
    seen.try_reserve_exact(words)?;
    seen.try_extend(std::iter::repeat_n(0u64, words))?;
    let mut walk = Walk {
        seen,
        pending: BudgetVec::new(budget),
    };

    if let Some(&Value::Reference(root)) = reads.trailer().get("Root")
        && walk.mark(root.number())
        && let Some(catalog) = read(reads, root.number())?
        && let Value::Dict(catalog) = &**catalog.value()
        && let Some(properties) = catalog.get("OCProperties")
    {
        walk.scan(properties)?;
    }

    let mut next = parent(page.dict());
    while let Some(number) = next.take() {
        if !walk.mark(number) {
            break;
        }
        let Some(object) = read(reads, number)? else {
            break;
        };
        let Value::Dict(node) = &**object.value() else {
            break;
        };
        for key in INHERITABLE {
            if let Some(value) = node.get(key) {
                walk.scan(value)?;
            }
        }
        next = parent(node);
    }

    if let Some(object) = page.object() {
        walk.mark(object.number());
        read(reads, object.number())?;
    }
    for (key, value) in page.dict().iter() {
        if key.as_bytes() != b"Parent" {
            walk.scan(value)?;
        }
    }
    walk.drain(reads)
}

/// The objects that the walk has marked, and those it has yet to enter.
struct Walk<'b> {
    /// One bit per object number below the version's next number.
    seen: BudgetVec<'b, u64>,
    pending: BudgetVec<'b, u32>,
}

impl Walk<'_> {
    /// Marks object `number`, and returns whether it was unmarked. A number
    /// at or past the version's next number names no object of the version,
    /// so it is never marked.
    fn mark(&mut self, number: u32) -> bool {
        let at = (number / 64) as usize;
        let Some(word) = self.seen.get_mut(at) else {
            return true;
        };
        let bit = 1u64 << (number % 64);
        let unmarked = *word & bit == 0;
        *word |= bit;
        unmarked
    }

    /// Marks the objects that `value` refers to, at any depth, and queues
    /// those not yet marked to be entered.
    fn scan(&mut self, value: &Value) -> Result<()> {
        let mut stack = BudgetVec::new(self.seen.budget());
        stack.try_push(value)?;
        while let Some(value) = stack.pop() {
            match value {
                &Value::Reference(reference) => {
                    if self.mark(reference.number()) {
                        self.pending.try_push(reference.number())?;
                    }
                }
                Value::Array(items) => stack.try_extend(items.iter())?,
                Value::Dict(dict) => stack.try_extend(dict.iter().map(|(_, value)| value))?,
                Value::Stream(stream) => {
                    stack.try_extend(stream.dict().iter().map(|(_, value)| value))?;
                }
                _ => {}
            }
        }
        Ok(())
    }

    /// Enters the queued objects and what they reach, but for the pages,
    /// page-tree nodes, and catalogs, which are only read.
    fn drain(&mut self, reads: &mut Reads<'_>) -> Result<()> {
        while let Some(number) = self.pending.pop() {
            let Some(object) = read(reads, number)? else {
                continue;
            };
            let value = &**object.value();
            if !is_stop(value) {
                self.scan(value)?;
            }
        }
        Ok(())
    }
}

/// Reads and records object `number`. An object that the store cannot read
/// is recorded with nothing to enter, as hayro skips what it cannot parse,
/// and only a limit stops the walk.
fn read(reads: &mut Reads<'_>, number: u32) -> Result<Option<Object>> {
    match reads.get(number) {
        Err(error) if !matches!(error.kind(), ErrorKind::Limit(_)) => Ok(None),
        result => result,
    }
}

/// Returns the object number of the `/Parent` of `dict`.
fn parent(dict: &Dict) -> Option<u32> {
    match dict.get("Parent") {
        Some(&Value::Reference(reference)) => Some(reference.number()),
        _ => None,
    }
}

/// Whether `value` is a page, a page-tree node, or a catalog, which the
/// walk reads without entering.
fn is_stop(value: &Value) -> bool {
    let Value::Dict(dict) = value else {
        return false;
    };
    matches!(
        dict.get("Type"),
        Some(Value::Name(name)) if matches!(name.as_bytes(), b"Page" | b"Pages" | b"Catalog")
    )
}
