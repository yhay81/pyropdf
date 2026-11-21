//! One-input, one-output PDF functions used by classic DeviceRGB-to-DeviceCMYK
//! rewriting. The function's parsed representation and evaluator belong to hayro.

use hayro::hayro_interpret::Function;
use hayro::hayro_syntax::object::{Array, Dict, Object, ObjectIdentifier};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::value::Reference;

use crate::bridge::Bridged;

/// One existing store-handle address step.
pub enum CurveStep<'a> {
    /// A dictionary key.
    Key(&'a [u8]),
    /// An array index.
    Index(usize),
}

/// An immutable active black-generation or undercolor-removal curve.
pub struct DeviceCurve(Function);

impl DeviceCurve {
    /// Resolves the current version's address and constructs a bounded scalar
    /// function. Unsupported, ambiguous or malformed functions stay in the PDF.
    pub fn load<'a>(
        bridged: &Bridged,
        root: Reference,
        steps: impl Iterator<Item = CurveStep<'a>>,
        budget: &Budget,
    ) -> Result<Option<Self>, LimitExceeded> {
        budget.poll()?;
        let Some(object) = resolve(bridged, root, steps) else {
            return Ok(None);
        };
        let Some(bytes) = retained_bytes(&object) else {
            return Ok(None);
        };
        budget.charge(LimitCode::MemorySize, bytes)?;
        let function =
            Function::new(&object).filter(|f| f.num_inputs() == 1 && f.num_outputs() == 1);
        budget.poll()?;
        Ok(function.map(Self))
    }

    /// Applies the PDF function, including its Domain and Range, to one sample.
    pub fn evaluate(&self, k: f32) -> Option<f32> {
        let mut value = [0.0];
        self.0.eval(&[k], &mut value)?;
        value[0].is_finite().then_some(value[0])
    }
}

fn resolve<'a, 'b>(
    bridged: &'a Bridged,
    root: Reference,
    steps: impl Iterator<Item = CurveStep<'b>>,
) -> Option<Object<'a>> {
    let id = ObjectIdentifier::new(root.number(), i32::from(root.generation()));
    let mut object = bridged.pdf().xref().get::<Object<'_>>(id)?;
    for step in steps {
        object = match (object, step) {
            (Object::Dict(d), CurveStep::Key(key)) => d.get::<Object<'_>>(key)?,
            (Object::Stream(s), CurveStep::Key(key)) => s.dict().get::<Object<'_>>(key)?,
            (Object::Array(a), CurveStep::Index(i)) if i < 4096 && a.data().len() <= 65536 => {
                a.iter::<Object<'_>>().nth(i)?
            }
            _ => return None,
        };
    }
    Some(object)
}

/// Inspect fixed arities before handing any input-sized array to the pinned
/// evaluator. At most `count + 1` objects are visited, also for malformed input.
fn numbers(d: &Dict<'_>, key: &[u8], count: usize, optional: bool) -> bool {
    let Some(value) = d.get::<Object<'_>>(key) else {
        return optional;
    };
    let Object::Array(array) = value else {
        return false;
    };
    if array.data().len() > 4096 {
        return false;
    }
    let mut values = array.iter::<Object<'_>>();
    (0..count).all(|_| matches!(values.next(), Some(Object::Number(n)) if n.as_f32().is_finite()))
        && values.next().is_none()
}

fn dictionary<'a, 'b>(object: &'b Object<'a>) -> Option<&'b Dict<'a>> {
    match object {
        Object::Dict(d) => Some(d),
        Object::Stream(s) => Some(s.dict()),
        _ => None,
    }
}

/// Includes bounded evaluator arrays, sampled values and calculator instructions.
fn leaf_bytes(object: &Object<'_>) -> Option<u64> {
    let d = dictionary(object)?;
    if !numbers(d, b"Domain", 2, false) || !numbers(d, b"Range", 2, true) {
        return None;
    }
    match (d.get::<u8>(b"FunctionType")?, object) {
        (2, Object::Dict(_)) => (numbers(d, b"C0", 1, true)
            && numbers(d, b"C1", 1, true)
            && d.get::<f32>(b"N").is_some_and(f32::is_finite))
        .then_some(512),
        (0, Object::Stream(stream)) => {
            if !numbers(d, b"Size", 1, false)
                || !numbers(d, b"Range", 2, false)
                || !numbers(d, b"Encode", 2, true)
                || !numbers(d, b"Decode", 2, true)
                || d.get::<u8>(b"Order").unwrap_or(1) != 1
                || (d.contains_key(b"Filter") || d.contains_key(b"F"))
            {
                return None;
            }
            let size = d.get::<Array<'_>>(b"Size")?.iter::<u32>().next()?;
            let length = d.get::<u32>(b"Length")?;
            ((1..=4096).contains(&size)
                && (1..=16384).contains(&length)
                && stream.raw_data().len() <= length as usize)
                .then(|| 512 + u64::from(length) + 4 * u64::from(size))
        }
        (4, Object::Stream(stream)) => {
            if !numbers(d, b"Range", 2, false)
                || (d.contains_key(b"Filter") || d.contains_key(b"F"))
                || !matches!(d.get::<usize>(b"Length"), Some(1..=256))
            {
                return None;
            }
            // A straight-line calculator has bounded parsing and evaluation.
            // #15 and #105 track general nested construction and evaluation fuel.
            let bytes = stream.raw_data();
            let body = bytes.trim_ascii().strip_prefix(b"{")?.strip_suffix(b"}")?;
            (bytes.len() <= 256 && !body.iter().any(|&c| c == b'{' || c == b'}'))
                .then(|| 512 + 64 * bytes.len() as u64)
        }
        _ => None,
    }
}

fn retained_bytes(object: &Object<'_>) -> Option<u64> {
    let d = dictionary(object)?;
    if d.get::<u8>(b"FunctionType") != Some(3) {
        return leaf_bytes(object);
    }
    let parts = d.get::<Array<'_>>(b"Functions")?;
    if parts.data().len() > 65536 {
        return None;
    }
    // One level and at most sixteen scalar leaves bound the traversal before
    // delegating to the same function implementation used by rendering.
    let mut count = 0;
    let mut bytes = 1024;
    for part in parts.iter::<Object<'_>>() {
        if count == 16 {
            return None;
        }
        bytes += leaf_bytes(&part)?;
        count += 1;
    }
    (count > 0
        && numbers(d, b"Domain", 2, false)
        && numbers(d, b"Range", 2, true)
        && numbers(d, b"Bounds", count - 1, false)
        && numbers(d, b"Encode", count * 2, false))
    .then_some(bytes)
}

/// Apply two byte-indexed output-device curves to straight, default-device
/// CMYK samples. Adding the original black channel back to each ink recovers
/// the complement of the original RGB channel before subtracting custom UCR.
pub fn apply_black_tables(
    samples: &mut [u8],
    black: &[u8; 256],
    removal: &[i16; 256],
    budget: &Budget,
) -> Result<(), LimitExceeded> {
    for block in samples.chunks_mut(16 * 1024) {
        budget.poll()?;
        for pixel in block.as_chunks_mut::<4>().0 {
            let k = usize::from(pixel[3]);
            for ink in &mut pixel[..3] {
                *ink = (i32::from(*ink) + k as i32 - i32::from(removal[k])).clamp(0, 255) as u8;
            }
            pixel[3] = black[k];
        }
    }
    budget.poll()
}
