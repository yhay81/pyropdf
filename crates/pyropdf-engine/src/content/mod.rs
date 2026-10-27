//! Content streams as the operations that hayro reads, each keeping its
//! bytes (DESIGN.md sections 6 and 14), ported from pylopdf's
//! `rust/src/content/syntax.rs` at 036be111cafe77755db4bb2be17bc70a8f2f9d82.
//!
//! hayro-syntax's `UntypedIter` tokenizes the content, so the operations are
//! exactly the ones that hayro interprets, inline images and its recoveries
//! from malformed input included. Each operation owns the bytes from the end
//! of the previous one to the end of its operator, or to the end of `EI` for
//! an inline image, and the bytes after the last operation are the tail: the
//! bytes of the operations and the tail, in order, are the content. hayro
//! stops at an operation that it cannot read, such as one with an eleventh
//! operand, and the rest of the content then stays in the tail.
//!
//! Until hayro reports where each painted item comes from (I4,
//! yhay81/pyropdf#104), an operation's index and span are what name it in a
//! page's own content streams; [`PageContent`] maps them to the streams.

use std::ops::Range;
use std::panic::{self, AssertUnwindSafe};

use hayro::hayro_syntax::content::UntypedIter;
use hayro::hayro_syntax::object::Object;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::{Bytes, Value};
use pyropdf_store::{Error, Result};

mod page;
#[cfg(test)]
mod tests;

pub use page::{PageContent, Part};

/// How many operations the parse reads between two polls of the budget.
const POLL_INTERVAL: usize = 1024;

/// The white-space characters of PDF (ISO 32000-2 table 1).
const WHITE_SPACE: &[u8] = b"\x00\t\n\x0c\r ";

/// One operation of a content stream: where its bytes and its operator are,
/// and which operands of its [`Operations`] are its own.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Operation {
    span: Range<usize>,
    operator: Range<usize>,
    operands: Range<usize>,
    image: Option<Range<usize>>,
}

impl Operation {
    /// Returns the bytes of the operation in the content: from the end of
    /// the previous operation, comments and white space included, to the
    /// end of its operator, or of `EI` for an inline image.
    #[must_use]
    pub fn span(&self) -> Range<usize> {
        self.span.clone()
    }

    /// Returns where the operator is in the content: `BI` for an inline
    /// image.
    #[must_use]
    pub fn operator_span(&self) -> Range<usize> {
        self.operator.clone()
    }

    /// Returns where the data of an inline image is in the content, between
    /// the white space after `ID` and the white-space byte before `EI`, or
    /// `None` for any other operation.
    #[must_use]
    pub fn image_span(&self) -> Option<Range<usize>> {
        self.image.clone()
    }
}

/// The operations of one content stream, as hayro reads them.
#[derive(Clone, Debug)]
pub struct Operations {
    data: Bytes,
    operations: Vec<Operation>,
    operands: Vec<Value>,
}

impl Operations {
    /// Tokenizes `data`, decoded content, through hayro-syntax.
    ///
    /// The operands keep their lexemes as slices of `data`, as the values of
    /// objects do. An inline image's one operand is its dictionary. Refuses
    /// with `content_operation_count` more operations than the budget
    /// allows, with the budget's other limits what it does not allow, and
    /// with a malformed input error an operator written with a `#` escape,
    /// whose place hayro-syntax does not report, or a panic of hayro-syntax
    /// (I3, yhay81/pyropdf#19).
    pub fn parse(data: Bytes, budget: &Budget) -> Result<Self> {
        panic::catch_unwind(AssertUnwindSafe(|| parse(&data, budget)))
            .unwrap_or_else(|_| Err(Error::malformed("hayro-syntax panicked on the content")))
            .map(|(operations, operands)| Self {
                data,
                operations,
                operands,
            })
    }

    /// Returns the content.
    #[must_use]
    pub const fn data(&self) -> &Bytes {
        &self.data
    }

    /// Returns the number of operations.
    #[must_use]
    pub const fn len(&self) -> usize {
        self.operations.len()
    }

    /// Returns whether the content holds no operation.
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.operations.is_empty()
    }

    /// Returns the operations in order.
    #[must_use]
    pub fn operations(&self) -> &[Operation] {
        &self.operations
    }

    /// Returns the bytes of `operation`.
    #[must_use]
    pub fn bytes(&self, operation: &Operation) -> &[u8] {
        &self.data[operation.span()]
    }

    /// Returns the operator of `operation`.
    #[must_use]
    pub fn operator(&self, operation: &Operation) -> &[u8] {
        &self.data[operation.operator_span()]
    }

    /// Returns the operands of `operation`: the values before its operator,
    /// or the dictionary of an inline image.
    #[must_use]
    pub fn operands(&self, operation: &Operation) -> &[Value] {
        &self.operands[operation.operands.clone()]
    }

    /// Returns the encoded data of an inline image, or `None` for any other
    /// operation.
    #[must_use]
    pub fn image_data(&self, operation: &Operation) -> Option<&[u8]> {
        operation.image_span().map(|span| &self.data[span])
    }

    /// Returns where the tail begins: the end of the last operation.
    #[must_use]
    pub fn tail_start(&self) -> usize {
        self.operations.last().map_or(0, |last| last.span.end)
    }

    /// Returns the bytes after the last operation.
    #[must_use]
    pub fn tail(&self) -> &[u8] {
        &self.data[self.tail_start()..]
    }
}

/// Reads the operations and operands of `data`.
fn parse(data: &Bytes, budget: &Budget) -> Result<(Vec<Operation>, Vec<Value>)> {
    let content = data.as_slice();
    let most = budget.limit(LimitCode::ContentOperationCount);
    let mut operations = BudgetVec::new(budget);
    let mut operands = BudgetVec::new(budget);
    let mut previous_end = 0;
    let mut iter = UntypedIter::new(content);
    while let Some(instruction) = iter.next() {
        let count = operations.len() + 1;
        if most.is_some_and(|most| count as u64 > most) {
            budget.check(LimitCode::ContentOperationCount, count as u64)?;
        }
        if count.is_multiple_of(POLL_INTERVAL) {
            budget.poll()?;
        }
        // The operator borrows the content unless it was written with a `#`
        // escape, whose decoded copy has no place in it.
        let name: &[u8] = instruction.operator;
        let start = offset_in(content, name)
            .filter(|&start| &content[start..start + name.len()] == name)
            .ok_or_else(|| {
                Error::malformed(
                    "a content operator is written with a # escape, whose place hayro-syntax does not report",
                )
            })?;
        let operator = start..start + name.len();
        let mut end = operator.end;
        let first = operands.len();
        let mut image = None;
        for operand in instruction.operands() {
            if let Object::Stream(stream) = operand {
                // The data of an inline image ends where `EI` begins, after
                // the white-space byte that hayro-syntax keeps in it.
                let raw = stream.raw_data();
                let data_start = offset_in(content, &raw).ok_or_else(|| {
                    Error::malformed("an inline image's data is not in the content")
                })?;
                let mut data_end = data_start + raw.len();
                end = (data_end + 2).min(content.len());
                if data_end > data_start && WHITE_SPACE.contains(&content[data_end - 1]) {
                    data_end -= 1;
                }
                image = Some(data_start..data_end);
                operands.truncate(first);
                let dict = Object::Dict(stream.dict().clone());
                operands.try_push(Value::from_object(&dict, data, budget)?)?;
                break;
            }
            operands.try_push(Value::from_object(operand, data, budget)?)?;
        }
        if end < previous_end {
            return Err(Error::malformed("content operations overlap"));
        }
        operations.try_push(Operation {
            span: previous_end..end,
            operator,
            operands: first..operands.len(),
            image,
        })?;
        previous_end = end;
    }
    Ok((operations.into_vec(), operands.into_vec()))
}

/// Returns where `part` starts in `whole`, if it is a slice of it.
///
/// hayro-syntax borrows what it reads from the content, so the place of a
/// slice that it returns is the place of what it read.
fn offset_in(whole: &[u8], part: &[u8]) -> Option<usize> {
    let start = part.as_ptr().addr().checked_sub(whole.as_ptr().addr())?;
    (start.checked_add(part.len())? <= whole.len()).then_some(start)
}
