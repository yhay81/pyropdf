//! The decoded size of one stream (DESIGN.md section 9).

use super::{Budget, LimitCode};
use crate::LimitExceeded;

/// What a decoded stream is, which decides the limits on its decoded size.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum StreamKind {
    /// A stream that only `decompressed_size` and `total_decompressed_size`
    /// bound.
    Any,
    /// A content stream of a page, which `page_content_size` also bounds.
    PageContent,
}

impl StreamKind {
    /// Returns the limits on the size of one stream of this kind, in the
    /// order that wins a tie.
    const fn stream_codes(self) -> &'static [LimitCode] {
        match self {
            Self::Any => &[LimitCode::DecompressedSize],
            Self::PageContent => &[LimitCode::DecompressedSize, LimitCode::PageContentSize],
        }
    }
}

/// The decoded size of one stream, charged to its budget as the stream
/// decodes.
///
/// As in pylopdf's `validate_decompression_limits`, three limits bound a
/// stream: `decompressed_size` for any one stream, `page_content_size` for a
/// content stream of a page, and `total_decompressed_size` for all the
/// streams of the operation. Each charge counts against all of them. A
/// refusal names the limit that the decoded bytes cross first, a limit on
/// the stream winning a tie with the total, and changes neither the stream's
/// size nor the budget.
#[derive(Debug)]
pub struct DecodeMeter<'b> {
    budget: &'b Budget,
    kind: StreamKind,
    size: u64,
}

impl<'b> DecodeMeter<'b> {
    /// Returns a meter for a stream of `kind` that has decoded nothing yet.
    pub(super) const fn new(budget: &'b Budget, kind: StreamKind) -> Self {
        Self {
            budget,
            kind,
            size: 0,
        }
    }

    /// Returns the decoded bytes charged so far.
    #[must_use]
    pub const fn size(&self) -> u64 {
        self.size
    }

    /// Returns how many more bytes the stream may decode to, or `None` if no
    /// limit bounds it.
    ///
    /// A decoder that needs a bound up front can decode at most one byte
    /// more and charge what it produced, so that a stream over a limit
    /// refuses with that limit's code.
    #[must_use]
    pub fn remaining(&self) -> Option<u64> {
        self.kind
            .stream_codes()
            .iter()
            .filter_map(|&code| self.budget.limit(code))
            .map(|limit| limit.saturating_sub(self.size))
            .chain(self.budget.remaining(LimitCode::TotalDecompressedSize))
            .min()
    }

    /// Charges `bytes` more decoded bytes of the stream.
    ///
    /// After [`Budget::poll`], refuses with the code of the first limit that
    /// the bytes would cross: the one with the least room left.
    pub fn charge(&mut self, bytes: u64) -> Result<(), LimitExceeded> {
        self.budget.poll()?;
        let size = self.size.saturating_add(bytes);
        let mut first: Option<(u64, LimitExceeded)> = None;
        for &code in self.kind.stream_codes() {
            if let Some(limit) = self.budget.limit(code) {
                let room = limit.saturating_sub(self.size);
                if bytes > room && first.is_none_or(|(least, _)| room < least) {
                    first = Some((room, LimitExceeded::over(code, limit, size)));
                }
            }
        }
        let total = LimitCode::TotalDecompressedSize;
        if let Some(limit) = self.budget.limit(total) {
            let used = self.budget.used(total);
            let room = limit.saturating_sub(used);
            if bytes > room && first.is_none_or(|(least, _)| room < least) {
                return Err(LimitExceeded::over(
                    total,
                    limit,
                    used.saturating_add(bytes),
                ));
            }
        }
        if let Some((_, refusal)) = first {
            return Err(refusal);
        }
        // Another thread of the operation may have charged the total since it
        // was read, so this charge checks it again.
        self.budget.add(total, bytes)?;
        self.size = size;
        Ok(())
    }
}
