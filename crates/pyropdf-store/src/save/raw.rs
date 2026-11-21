//! Locate a value's exact original bytes with hayro's public reader.

use std::panic::{self, AssertUnwindSafe};

use hayro_syntax::object::{FromBytes, MaybeRef, Object, Stream};
use hayro_syntax::reader::{Reader, ReaderContext, ReaderExt};

use crate::budget::Budget;
use crate::index::Index;
use crate::value::{Bytes, Reference, Value};
use crate::version::Base;
use crate::{Error, Result};

/// The index returns a suffix beginning with the value. Read its extent,
/// without serializing it or copying a container or stream's contents.
pub(super) fn body(base: &dyn Base, reference: Reference, budget: &Budget) -> Result<Bytes> {
    body_with(
        &|reference| base.object(reference, budget),
        reference,
        budget,
    )
}

/// Returns the bytes of a value of the file as they are in it, encrypted
/// when the file is, for a save that keeps the encryption.
pub(super) fn file_body(index: &Index, reference: Reference, budget: &Budget) -> Result<Bytes> {
    body_with(
        &|reference| index.object(reference, budget),
        reference,
        budget,
    )
}

fn body_with(
    fetch: &dyn Fn(Reference) -> Result<Option<Bytes>>,
    reference: Reference,
    budget: &Budget,
) -> Result<Bytes> {
    budget.poll()?;
    let bytes = fetch(reference)?
        .ok_or_else(|| Error::malformed("an in-use base object has no readable bytes"))?;
    let end = panic::catch_unwind(AssertUnwindSafe(|| extent(fetch, &bytes, budget)))
        .unwrap_or_else(|_| {
            Err(Error::malformed(
                "hayro-syntax panicked on an object being saved",
            ))
        })?;
    budget.poll()?;
    bytes
        .slice(0..end)
        .ok_or_else(|| Error::malformed("the object extends past its bytes"))
}

fn extent(
    fetch: &dyn Fn(Reference) -> Result<Option<Bytes>>,
    bytes: &Bytes,
    budget: &Budget,
) -> Result<usize> {
    let mut reader = Reader::new(bytes);
    reader.skip_white_spaces_and_comments();
    let invalid = || Error::malformed("the object being saved does not begin with a PDF value");
    if reader.peek_bytes(2) == Some(b"<<") {
        let start = reader.offset();
        match Object::from_bytes(&bytes[start..]).ok_or_else(invalid)? {
            Object::Stream(stream) => return stream_extent(fetch, bytes, &stream, budget),
            Object::Dict(dict) => {
                reader.jump(start + dict.data().len());
                reader.skip_white_spaces_and_comments();
            }
            _ => return Err(invalid()),
        }
    } else {
        reader
            .read_with_context::<MaybeRef<Object<'_>>>(&ReaderContext::dummy())
            .ok_or_else(invalid)?;
    }
    // A dictionary reader looks past its closing delimiter for `stream`
    // and may consume following whitespace. That whitespace belongs to
    // the indirect-object wrapper, not the value itself.
    let end = bytes[..reader.offset()]
        .iter()
        .rposition(|byte| !matches!(byte, b'\0' | b'\t' | b'\n' | b'\x0c' | b'\r' | b' '))
        .map_or(0, |at| at + 1);
    Ok(end)
}

/// hayro supplies the payload's actual start and length. An indirect length
/// comes from the original base because the standalone reader cannot resolve it.
fn stream_extent(
    fetch: &dyn Fn(Reference) -> Result<Option<Bytes>>,
    bytes: &Bytes,
    stream: &Stream<'_>,
    budget: &Budget,
) -> Result<usize> {
    let raw = stream.raw_data();
    let start = bytes
        .offset_of(&raw)
        .ok_or_else(|| Error::malformed("stream data is outside its object"))?;
    let length = if let Some(length) = stream.dict().get_ref("Length") {
        let reference = Reference::new(
            length.obj_number,
            u16::try_from(length.gen_number)
                .map_err(|_| Error::malformed("invalid length object generation"))?,
        );
        let length_bytes =
            fetch(reference)?.ok_or_else(|| Error::malformed("missing stream length object"))?;
        let Value::Integer(length) = Value::parse(&length_bytes, budget)? else {
            return Err(Error::malformed(
                "a stream's indirect length is not an integer",
            ));
        };
        usize::try_from(length).map_err(|_| Error::malformed("invalid indirect stream length"))?
    } else {
        raw.len()
    };
    let end = start
        .checked_add(length)
        .filter(|&end| end <= bytes.len())
        .ok_or_else(|| Error::malformed("invalid stream length"))?;
    let mut reader = Reader::new(bytes);
    reader.jump(end);
    reader.skip_white_spaces();
    reader
        .forward_tag(b"endstream")
        .ok_or_else(|| Error::malformed("the stream length does not end at endstream"))?;
    Ok(reader.offset())
}
