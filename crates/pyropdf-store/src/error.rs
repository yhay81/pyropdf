//! The store's typed errors (DESIGN.md section 9).
//!
//! Core crates return typed errors, and the binding maps them to pylopdf's
//! exception hierarchy. This module, like the rest of the store, does not
//! depend on PyO3 (section 11).

use std::fmt;

use crate::budget::LimitCode;

/// The result of an operation of the store.
pub type Result<T, E = Error> = std::result::Result<T, E>;

/// Why an operation of the store refused or failed.
///
/// The binding maps each kind to pylopdf's hierarchy (DESIGN.md section 9):
///
/// | Kind | Python exception |
/// | --- | --- |
/// | [`ErrorKind::Limit`] | `LimitError`, whose `code` is [`LimitCode::as_str`] |
/// | [`ErrorKind::Malformed`] | `MalformedPdfError` |
/// | [`ErrorKind::Password`] with [`Password::Required`] | `EncryptedDocumentError` |
/// | [`ErrorKind::Password`] with [`Password::Incorrect`] | `PasswordError` |
/// | [`ErrorKind::Stale`] | `StaleObjectError` |
///
/// The message, which `Display` gives, says what the operation was doing,
/// from [`Error::context`], and then what went wrong.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Error {
    kind: ErrorKind,
    context: Option<Box<str>>,
}

/// What kind of error an [`Error`] is.
#[derive(Clone, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum ErrorKind {
    /// A limit of the operation's budget refused it.
    Limit(LimitExceeded),
    /// The input is not PDF where the operation had to read it, with what is
    /// wrong.
    Malformed(Box<str>),
    /// The document needs a password, or the password given is wrong.
    Password(Password),
    /// A handle names an object that the current version no longer has, with
    /// what is gone.
    Stale(Box<str>),
}

/// Why a password refused an operation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum Password {
    /// The document is encrypted, and the operation needs a password that
    /// has not been given.
    Required,
    /// The password matches neither the user password nor the owner
    /// password.
    Incorrect,
}

/// A refusal by a limit: its code, and the limit and the amount when they
/// are known.
///
/// The amount is what the operation would have reached: the total of a meter
/// after the refused charge, or the measurement that a check refused.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LimitExceeded {
    code: LimitCode,
    limit: Option<u64>,
    amount: Option<u64>,
}

impl Error {
    /// Returns an error of `kind`.
    #[must_use]
    pub const fn new(kind: ErrorKind) -> Self {
        Self {
            kind,
            context: None,
        }
    }

    /// Returns the error for malformed input, with what is wrong.
    #[must_use]
    pub fn malformed(description: impl Into<Box<str>>) -> Self {
        Self::new(ErrorKind::Malformed(description.into()))
    }

    /// Returns the error for a stale handle, with what is gone.
    #[must_use]
    pub fn stale(description: impl Into<Box<str>>) -> Self {
        Self::new(ErrorKind::Stale(description.into()))
    }

    /// Returns the kind of the error.
    #[must_use]
    pub const fn kind(&self) -> &ErrorKind {
        &self.kind
    }

    /// Returns the refusal, if a limit refused the operation.
    #[must_use]
    pub const fn limit(&self) -> Option<&LimitExceeded> {
        match &self.kind {
            ErrorKind::Limit(refusal) => Some(refusal),
            _ => None,
        }
    }

    /// Returns the error with what the operation was doing, which the
    /// message gives before any context added earlier.
    #[must_use]
    pub fn context(mut self, context: impl fmt::Display) -> Self {
        let context = match self.context.take() {
            Some(inner) => format!("{context}: {inner}"),
            None => context.to_string(),
        };
        self.context = Some(context.into_boxed_str());
        self
    }
}

impl From<LimitExceeded> for Error {
    fn from(refusal: LimitExceeded) -> Self {
        Self::new(ErrorKind::Limit(refusal))
    }
}

impl From<Password> for Error {
    fn from(password: Password) -> Self {
        Self::new(ErrorKind::Password(password))
    }
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let Some(context) = &self.context {
            write!(formatter, "{context}: ")?;
        }
        fmt::Display::fmt(&self.kind, formatter)
    }
}

impl std::error::Error for Error {}

impl fmt::Display for ErrorKind {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Limit(refusal) => fmt::Display::fmt(refusal, formatter),
            Self::Malformed(description) | Self::Stale(description) => {
                formatter.write_str(description)
            }
            Self::Password(password) => fmt::Display::fmt(password, formatter),
        }
    }
}

impl fmt::Display for Password {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Required => "the document is encrypted and needs a password",
            Self::Incorrect => "the password matches neither the user nor the owner password",
        })
    }
}

impl LimitExceeded {
    /// Returns a refusal with `code` that gives no numbers.
    #[must_use]
    pub const fn new(code: LimitCode) -> Self {
        Self {
            code,
            limit: None,
            amount: None,
        }
    }

    /// Returns a refusal with `code` because `amount` is over `limit`.
    #[must_use]
    pub const fn over(code: LimitCode, limit: u64, amount: u64) -> Self {
        Self {
            code,
            limit: Some(limit),
            amount: Some(amount),
        }
    }

    /// Returns the refusal, giving the limit.
    #[must_use]
    pub const fn with_limit(mut self, limit: u64) -> Self {
        self.limit = Some(limit);
        self
    }

    /// Returns the refusal, giving the amount.
    #[must_use]
    pub const fn with_amount(mut self, amount: u64) -> Self {
        self.amount = Some(amount);
        self
    }

    /// Returns the code of the limit.
    #[must_use]
    pub const fn code(&self) -> LimitCode {
        self.code
    }

    /// Returns the limit, if the refusal gives it.
    #[must_use]
    pub const fn limit(&self) -> Option<u64> {
        self.limit
    }

    /// Returns the amount that the operation would have reached, if the
    /// refusal gives it.
    #[must_use]
    pub const fn amount(&self) -> Option<u64> {
        self.amount
    }
}

impl fmt::Display for LimitExceeded {
    /// Writes the code's message, followed by the amount and the limit when
    /// the refusal gives them, such as "a stream decodes to too many bytes:
    /// 70000000 bytes, over the limit of 67108864".
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code.message())?;
        let (space, unit) = match self.code.unit() {
            Some(unit) => (" ", unit),
            None => ("", ""),
        };
        match (self.amount, self.limit) {
            (Some(amount), Some(limit)) => {
                write!(
                    formatter,
                    ": {amount}{space}{unit}, over the limit of {limit}"
                )
            }
            (Some(amount), None) => write!(formatter, ": {amount}{space}{unit}"),
            (None, Some(limit)) => {
                write!(formatter, ": more than the limit of {limit}{space}{unit}")
            }
            (None, None) => Ok(()),
        }
    }
}

impl std::error::Error for LimitExceeded {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_refusal_gives_the_numbers_it_knows() {
        let code = LimitCode::DecompressedSize;
        let cases = [
            (
                LimitExceeded::over(code, 64, 70),
                "a stream decodes to too many bytes: 70 bytes, over the limit of 64",
            ),
            (
                LimitExceeded::new(code).with_amount(70),
                "a stream decodes to too many bytes: 70 bytes",
            ),
            (
                LimitExceeded::new(code).with_limit(64),
                "a stream decodes to too many bytes: more than the limit of 64 bytes",
            ),
            (
                LimitExceeded::new(code),
                "a stream decodes to too many bytes",
            ),
            (
                LimitExceeded::new(LimitCode::Cancelled),
                "the operation was cancelled",
            ),
            (
                LimitExceeded::new(LimitCode::Cancelled).with_amount(3),
                "the operation was cancelled: 3",
            ),
        ];
        for (refusal, message) in cases {
            assert_eq!(refusal.to_string(), message);
        }
    }

    #[test]
    fn a_refusal_keeps_its_code_limit_and_amount() {
        let refusal = LimitExceeded::over(LimitCode::ObjectCount, 10, 11);
        assert_eq!(refusal.code(), LimitCode::ObjectCount);
        assert_eq!(refusal.limit(), Some(10));
        assert_eq!(refusal.amount(), Some(11));
        let error = Error::from(refusal);
        assert_eq!(error.kind(), &ErrorKind::Limit(refusal));
        assert_eq!(error.limit(), Some(&refusal));
        assert_eq!(
            error.to_string(),
            "the document has too many objects: 11 objects, over the limit of 10"
        );
    }

    #[test]
    fn each_kind_has_its_message() {
        let cases = [
            (
                Error::malformed("the cross-reference table is truncated"),
                "the cross-reference table is truncated",
            ),
            (Error::stale("object 12 was freed"), "object 12 was freed"),
            (
                Error::from(Password::Required),
                "the document is encrypted and needs a password",
            ),
            (
                Error::from(Password::Incorrect),
                "the password matches neither the user nor the owner password",
            ),
        ];
        for (error, message) in cases {
            assert_eq!(error.limit(), None);
            assert_eq!(error.to_string(), message);
        }
        assert_eq!(
            Error::from(Password::Incorrect).kind(),
            &ErrorKind::Password(Password::Incorrect)
        );
    }

    #[test]
    fn context_comes_first_from_the_outermost() {
        let error = Error::from(LimitExceeded::over(LimitCode::StreamFilterCount, 16, 17))
            .context("stream 12 0")
            .context("reading page 3");
        assert_eq!(
            error.to_string(),
            "reading page 3: stream 12 0: a stream chains too many filters: 17 filters, over the limit of 16"
        );
        assert_eq!(
            error.limit().map(LimitExceeded::code),
            Some(LimitCode::StreamFilterCount)
        );
    }
}
