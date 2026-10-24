//! Objects, the index and its repair, versions, change sets, the writer, and
//! the security handler (DESIGN.md section 11), with the budgets that bound
//! every operation and the typed errors that the store returns (section 9).

pub mod budget;
mod error;

pub use error::{Error, ErrorKind, LimitExceeded, Password, Result};
