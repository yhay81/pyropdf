//! Named destinations, link annotations and actions, and the document
//! outline (DESIGN.md section 11).

pub mod destinations;
pub mod links;
pub mod outline;
#[cfg(test)]
mod tests;

use std::collections::HashSet;
use std::fmt;
use std::hash::Hash;

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, LimitCode, try_insert_set};
use pyropdf_store::version::Transaction;

pub use destinations::{
    Destination, Lead, LeadReader, NamedDestinations, lead, named_destinations, read, settle,
    target_entry,
};
pub use links::{link_add, link_edit, link_list, link_read};
pub use outline::{
    CountCache, MAX_DEPTH, MAX_EDGES, MAX_ITEMS, MAX_TEXT_BYTES, Outline, outline_children,
    outline_entries, outline_item, outline_set_open, outline_set_style, outline_set_target,
    outline_set_title, outline_walk_counts, outline_write,
};

/// The result of a navigation or outline operation.
pub type Result<T, E = Error> = std::result::Result<T, E>;

/// A display rectangle, `(x0, y0, x1, y1)`.
pub type Rect = (f64, f64, f64, f64);

/// An outline item's color: red, green, and blue from 0 to 1.
pub type Color = (f64, f64, f64);

/// Where a destination leads: its zero-based page, display point, and zoom.
pub type Place = (u32, Option<(f64, f64)>, Option<f64>);

/// A named destination: its name, and where it leads, or `None` when it
/// names no page.
pub type Named = (String, Option<Place>);

/// Where a link or outline item leads: its kind; the zero-based page of this
/// document for `"goto"`, or of another for `"gotor"`; a display point; a
/// zoom; a URI or a file; and a name. The kind `"none"` stands for an action
/// that pyropdf does not read, or a destination that names no page.
pub type Target = (
    String,
    Option<u32>,
    Option<(f64, f64)>,
    Option<f64>,
    Option<String>,
    Option<String>,
);

/// A link as a handle reads it: its position in the page's `/Annots`, its
/// object, `(0, 0)` for a direct dictionary, its display rectangle, and
/// where it leads.
pub type LinkRecord = (u32, (u32, u16), Rect, Target);

/// An outline item as a handle reads it: its level, title, target, color,
/// `/F` flags, `/Count`, and whether it has children.
pub type ItemRecord = (u32, String, Target, Option<Color>, i64, i64, bool);

/// An item of the whole outline in document order: its object, `(0, 0)` for
/// a direct dictionary, level, title, target, color, `/F` flags, and
/// `/Count`.
pub type EntryRecord = ((u32, u16), u32, String, Target, Option<Color>, i64, i64);

/// An outline item to write: its level, title, target, color, `/F` flags,
/// and whether its children show.
pub type Spec = (u32, String, Target, Option<Color>, u32, bool);

/// Returns the unresolved `"none"` target.
#[must_use]
pub fn none() -> Target {
    ("none".to_owned(), None, None, None, None, None)
}

/// Why a navigation or outline operation refused or failed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Error {
    /// A refusal or failure from the underlying store or budget.
    Store(pyropdf_store::Error),
    /// A limit refusal with a custom message and stable limit code.
    Limit { code: LimitCode, message: String },
    /// Malformed PDF structure detected during navigation or outline reading.
    Malformed(String),
    /// A handle whose link or outline item no longer exists in this version.
    Stale(String),
    /// An invalid navigation argument or structural PDF error.
    Pdf(String),
}

impl Error {
    /// Returns the limit code if a limit refused the operation.
    #[must_use]
    pub const fn limit_code(&self) -> Option<LimitCode> {
        match self {
            Self::Store(error) => match error.limit() {
                Some(refusal) => Some(refusal.code()),
                None => None,
            },
            Self::Limit { code, .. } => Some(*code),
            Self::Malformed(_) | Self::Stale(_) | Self::Pdf(_) => None,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Store(error) => fmt::Display::fmt(error, formatter),
            Self::Limit { message, .. }
            | Self::Malformed(message)
            | Self::Stale(message)
            | Self::Pdf(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for Error {}

impl From<pyropdf_store::Error> for Error {
    fn from(error: pyropdf_store::Error) -> Self {
        Self::Store(error)
    }
}

impl From<LimitExceeded> for Error {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Store(pyropdf_store::Error::from(refusal))
    }
}

pub(crate) fn visit<T: Eq + Hash>(seen: &mut HashSet<T>, key: T, budget: &Budget) -> Result<bool> {
    Ok(try_insert_set(seen, key, budget)?)
}

pub(crate) fn with_savepoint<T>(
    transaction: &mut Transaction,
    change: impl FnOnce(&mut Transaction) -> Result<T>,
) -> Result<T> {
    let savepoint = transaction.savepoint();
    let result = change(transaction);
    if result.is_err() {
        transaction.rollback(savepoint);
    }
    result
}
