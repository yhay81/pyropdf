//! The content of a page as hayro reads it: the streams of `/Contents`
//! decoded and joined, and the stream that each byte comes from.

use std::ops::Range;
use std::sync::Arc;

use pyropdf_store::budget::{BudgetVec, LimitCode, StreamKind};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::Page;
use pyropdf_store::value::{Bytes, Reference, Stream, Value};
use pyropdf_store::{ErrorKind, Result};

use super::{Operation, Operations};

/// One content stream of a page, and where its decoded data is in the
/// page's content.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Part {
    /// The stream object.
    pub stream: Reference,
    /// Where the stream's decoded data is in the content.
    pub span: Range<usize>,
}

/// The content of a page, and the content streams that it joins.
#[derive(Clone, Debug)]
pub struct PageContent {
    operations: Operations,
    parts: Vec<Part>,
}

impl PageContent {
    /// Reads the content of `page` through `reads`, as hayro reads it, and
    /// tokenizes it.
    ///
    /// A `/Contents` stream is the content. In an array, every stream is
    /// followed by one space, and the array ends at the first item that is
    /// not a stream. A stream whose filters do not decode is left out, and a
    /// page without such a stream has no content. Each stream decodes as a
    /// page content stream, which `page_content_size` bounds; the joined
    /// content refuses with `content_stream_size` beyond its limit, and the
    /// tokenizing refuses as [`Operations::parse`] does.
    pub fn read(reads: &mut Reads<'_>, page: &Page) -> Result<Self> {
        let budget = reads.budget();
        let mut data = BudgetVec::new(budget);
        let mut parts = BudgetVec::new(budget);
        match page.dict().get("Contents") {
            Some(&Value::Reference(reference)) => {
                if let Some(value) = resolve(reads, reference)? {
                    match &*value {
                        Value::Stream(stream) => {
                            append(reads, &mut data, &mut parts, reference, stream, false)?;
                        }
                        Value::Array(items) => array(reads, &mut data, &mut parts, items.iter())?,
                        _ => {}
                    }
                }
            }
            Some(Value::Array(items)) => array(reads, &mut data, &mut parts, items.iter())?,
            _ => {}
        }
        let operations = Operations::parse(Bytes::from(data.into_vec()), budget)?;
        Ok(Self {
            operations,
            parts: parts.into_vec(),
        })
    }

    /// Returns the operations of the content.
    #[must_use]
    pub const fn operations(&self) -> &Operations {
        &self.operations
    }

    /// Returns the content streams in the order of the content.
    #[must_use]
    pub fn parts(&self) -> &[Part] {
        &self.parts
    }

    /// Returns the part whose data holds byte `offset` of the content, or
    /// `None` for a space that joins two streams or an offset beyond them.
    #[must_use]
    pub fn part_at(&self, offset: usize) -> Option<&Part> {
        let after = self.parts.partition_point(|part| part.span.start <= offset);
        let part = self.parts.get(after.checked_sub(1)?)?;
        part.span.contains(&offset).then_some(part)
    }

    /// Returns the stream that holds the operator of `operation`: the
    /// stream that it comes from, even when its operands begin in an
    /// earlier one.
    #[must_use]
    pub fn stream_of(&self, operation: &Operation) -> Option<Reference> {
        self.part_at(operation.operator_span().start)
            .map(|part| part.stream)
    }
}

/// Returns the value of the object that `reference` names, as hayro
/// resolves it: one reference, to an object of that generation.
fn resolve(reads: &mut Reads<'_>, reference: Reference) -> Result<Option<Arc<Value>>> {
    Ok(reads
        .get(reference.number())?
        .filter(|object| object.generation() == reference.generation())
        .map(|object| Arc::clone(object.value())))
}

/// Appends the streams of a `/Contents` array, until the first item that
/// is not a reference to a stream.
fn array<'v>(
    reads: &mut Reads<'_>,
    data: &mut BudgetVec<'_, u8>,
    parts: &mut BudgetVec<'_, Part>,
    items: impl IntoIterator<Item = &'v Value>,
) -> Result<()> {
    for item in items {
        let &Value::Reference(reference) = item else {
            break;
        };
        let Some(value) = resolve(reads, reference)? else {
            break;
        };
        let Value::Stream(stream) = &*value else {
            break;
        };
        append(reads, data, parts, reference, stream, true)?;
    }
    Ok(())
}

/// Appends the decoded data of `stream`, and a space after it in an
/// array; a stream that does not decode is left out.
fn append(
    reads: &Reads<'_>,
    data: &mut BudgetVec<'_, u8>,
    parts: &mut BudgetVec<'_, Part>,
    reference: Reference,
    stream: &Stream,
    in_array: bool,
) -> Result<()> {
    let budget = reads.budget();
    let decoded = match stream.decoded_as(StreamKind::PageContent, budget) {
        Ok(decoded) => decoded,
        Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => return Ok(()),
        Err(error) => return Err(error),
    };
    let start = data.len();
    let size = start + decoded.len() + usize::from(in_array);
    budget.check(
        LimitCode::ContentStreamSize,
        u64::try_from(size).unwrap_or(u64::MAX),
    )?;
    data.try_extend_from_slice(&decoded)?;
    parts.try_push(Part {
        stream: reference,
        span: start..data.len(),
    })?;
    if in_array {
        data.try_push(b' ')?;
    }
    Ok(())
}
