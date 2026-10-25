//! Locate a value's exact original bytes with hayro's public reader.

use std::panic::{self, AssertUnwindSafe};

use hayro_syntax::object::{MaybeRef, Object};
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
    let object = reader
        .read_with_context::<MaybeRef<Object<'_>>>(&ReaderContext::dummy())
        .ok_or_else(|| {
            Error::malformed("the object being saved does not begin with a PDF value")
        })?;
    if let MaybeRef::NotRef(Object::Stream(stream)) = object
        && let Some(length) = stream.dict().get_ref("Length")
    {
        // The dummy reader cannot resolve an indirect /Length. Follow
        // exactly one reference in the original base, never recursively,
        // and retain embedded `endstream` tokens and trailing data bytes.
        let reference = Reference::new(
            u32::try_from(length.obj_number)
                .map_err(|_| Error::malformed("negative length object number"))?,
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
        let raw = stream.raw_data();
        let start = bytes
            .offset_of(&raw)
            .ok_or_else(|| Error::malformed("stream data is outside its object"))?;
        let end = usize::try_from(length)
            .ok()
            .and_then(|len| start.checked_add(len))
            .filter(|&end| end <= bytes.len())
            .ok_or_else(|| Error::malformed("invalid indirect stream length"))?;
        reader.jump(end);
        reader.skip_white_spaces();
        reader.forward_tag(b"endstream").ok_or_else(|| {
            Error::malformed("the indirect stream length does not end at endstream")
        })?;
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
