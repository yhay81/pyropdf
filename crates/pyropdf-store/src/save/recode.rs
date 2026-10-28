//! Recoding: the stream data that a save compresses or decompresses on
//! request (DESIGN.md section 4.4).

use flate2::{Compress, Compression, FlushCompress, Status};

use super::Result;
use crate::ErrorKind;
use crate::budget::{Budget, BudgetVec};
use crate::value::{Array, Bytes, Name, Stream, Value};

/// How a save writes the data of streams.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Streams {
    /// As they are.
    #[default]
    Keep,
    /// Compresses with Flate, at the level from 0 to 9, the data of each
    /// stream that has no filter, where that makes the object shorter. A
    /// level above 9 is 9. XMP metadata stays unfiltered, so that a tool
    /// that does not parse PDF finds it.
    Compress(u8),
    /// Decodes the leading filters that hayro-syntax decodes, so that a
    /// stream keeps only the filters from its first image filter on, such
    /// as `/DCTDecode`. A stream that does not decode stays as it is, and
    /// so does one whose data decodes to nothing, as hayro-syntax decodes
    /// data that is not of its filter.
    Decompress,
}

/// The filters that hayro-syntax decodes without its image decoders.
const DECODED: [&[u8]; 5] = [
    b"ASCIIHexDecode",
    b"ASCII85Decode",
    b"LZWDecode",
    b"FlateDecode",
    b"RunLengthDecode",
];

/// Returns `value` with its stream recoded as `streams` asks, or `None`
/// when it leaves `value` as it is.
pub(super) fn recoded(value: &Value, streams: Streams, budget: &Budget) -> Result<Option<Value>> {
    let Value::Stream(stream) = value else {
        return Ok(None);
    };
    // A stream with `/F` keeps its data in another file.
    if stream.dict().get("F").is_some() {
        return Ok(None);
    }
    let recoded = match streams {
        Streams::Keep => None,
        Streams::Compress(level) => compressed(stream, level, budget)?,
        Streams::Decompress => decompressed(stream, budget)?,
    };
    Ok(recoded.map(|stream| Value::Stream(Box::new(stream))))
}

pub(super) fn compressed(stream: &Stream, level: u8, budget: &Budget) -> Result<Option<Stream>> {
    let dict = stream.dict();
    let metadata =
        matches!(dict.get("Type"), Some(Value::Name(name)) if name.as_bytes() == b"Metadata");
    // `/DecodeParms` without a filter would apply to the new one.
    if metadata
        || dict.get("Filter").is_some()
        || dict.get("DecodeParms").is_some()
        || stream.data().is_empty()
    {
        return Ok(None);
    }
    budget.poll()?;
    let data = stream.data().as_slice();
    // Output that would not be shorter than the data is not kept, so the
    // buffer holds no more than the data.
    let mut out = BudgetVec::new(budget);
    out.try_extend(data.iter().map(|_| 0u8))?;
    let mut encoder = Compress::new(Compression::new(u32::from(level.min(9))), true);
    loop {
        let read = usize::try_from(encoder.total_in()).unwrap_or(usize::MAX);
        let written = usize::try_from(encoder.total_out()).unwrap_or(usize::MAX);
        let (Some(input), Some(output)) = (data.get(read..), out.as_mut_slice().get_mut(written..))
        else {
            return Ok(None);
        };
        if output.is_empty() {
            return Ok(None);
        }
        match encoder.compress(input, output, FlushCompress::Finish) {
            Ok(Status::StreamEnd) => break,
            Ok(Status::Ok) => budget.poll()?,
            Ok(Status::BufError) | Err(_) => return Ok(None),
        }
    }
    out.truncate(usize::try_from(encoder.total_out()).unwrap_or(usize::MAX));
    let mut dict = dict.clone();
    #[expect(
        clippy::disallowed_methods,
        reason = "the stream's dictionary gains one entry, its `/Filter`"
    )]
    dict.insert(Name::from("Filter"), Value::Name(Name::from("FlateDecode")));
    Ok(Some(Stream::new(dict, Bytes::new(out.into_vec()))))
}

fn decompressed(stream: &Stream, budget: &Budget) -> Result<Option<Stream>> {
    let dict = stream.dict();
    let decoded =
        |value: &Value| matches!(value, Value::Name(name) if DECODED.contains(&name.as_bytes()));
    // The filters and their parameters, which a lone filter gives directly.
    let (filters, parameters): (&[Value], Option<&[Value]>) = match dict.get("Filter") {
        Some(Value::Array(filters)) => (
            filters.as_slice(),
            match dict.get("DecodeParms") {
                Some(Value::Array(parameters)) => Some(parameters.as_slice()),
                _ => None,
            },
        ),
        Some(filter) => (std::slice::from_ref(filter), None),
        None => return Ok(None),
    };
    let count = filters.iter().take_while(|filter| decoded(filter)).count();
    if count == 0 {
        return Ok(None);
    }
    // The leading filters decode a copy of the stream; the rest stay.
    let mut leading = dict.clone();
    let mut rest = dict.clone();
    rest.remove("Filter");
    rest.remove("DecodeParms");
    if let Some(Value::Array(_)) = dict.get("Filter") {
        let split = parameters.map(|parameters| count.min(parameters.len()));
        #[expect(
            clippy::disallowed_methods,
            reason = "the dictionaries take parts of the stream's own arrays"
        )]
        {
            leading.insert(Name::from("Filter"), array(&filters[..count], budget)?);
            if let (Some(parameters), Some(split)) = (parameters, split) {
                leading.insert(
                    Name::from("DecodeParms"),
                    array(&parameters[..split], budget)?,
                );
            }
            if count < filters.len() {
                rest.insert(Name::from("Filter"), array(&filters[count..], budget)?);
                if let (Some(parameters), Some(split)) = (parameters, split) {
                    rest.insert(
                        Name::from("DecodeParms"),
                        array(&parameters[split..], budget)?,
                    );
                }
            }
        }
    }
    let data = match Stream::new(leading, stream.data().clone()).decoded(budget) {
        Ok(data) => data,
        Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    if data.is_empty() && !stream.data().is_empty() {
        return Ok(None);
    }
    Ok(Some(Stream::new(rest, Bytes::new(data))))
}

/// Returns the array of `values`, grown through `budget`.
fn array(values: &[Value], budget: &Budget) -> Result<Value> {
    let mut items = BudgetVec::new(budget);
    items.try_extend(values.iter().cloned())?;
    Ok(Value::Array(Array::from(items.into_vec())))
}
