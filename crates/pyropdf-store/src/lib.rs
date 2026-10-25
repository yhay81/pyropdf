//! Objects, the index and its repair, versions, change sets, the writer, and
//! the security handler (DESIGN.md section 11), with the budgets that bound
//! every operation and the typed errors that the store returns (section 9).

#![warn(clippy::allow_attributes_without_reason)]
#![cfg_attr(
    test,
    allow(
        clippy::disallowed_methods,
        reason = "tests grow standard collections freely; F6 binds the store's own code"
    )
)]

pub mod budget;
mod error;
pub mod index;
pub mod memo;
pub mod save;
pub mod value;
pub mod version;
pub mod write;

pub use error::{Error, ErrorKind, LimitExceeded, Password, Result};
