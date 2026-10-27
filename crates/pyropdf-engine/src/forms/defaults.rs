//! The form defaults that imported pages bring into the receiving form:
//! the default resources (`/DR`) that join the receiving ones, renamed
//! where a receiving resource of another value has the name, and the
//! default appearance strings (`/DA`) that follow those renames. Ported
//! from pylopdf's `rust/src/acroform_import.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321.

use std::collections::{HashMap, HashSet};

use pyropdf_store::Result;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::value::{Dict, Name, Reference, Value};

/// The most objects that comparing two resources reads; a larger pair
/// counts as different.
pub const MAX_COMPARED_OBJECTS: usize = 4_096;

/// The most name, string, key, and stream bytes that comparing two
/// resources reads; a larger pair counts as different.
pub const MAX_COMPARED_BYTES: usize = 1024 * 1024;

/// The renames of source resources, by `/DR` category and then by the
/// source name.
pub type Renames = HashMap<Name, HashMap<Name, Name>>;

/// What entry `key` of a dictionary holds.
pub enum Slot {
    /// Nothing: no entry, null, or a reference to null or to a missing
    /// object.
    Absent,
    /// A dictionary or an array, held by the object that the number names
    /// or, with `None`, by the entry itself.
    Found(Option<u32>, Value),
    /// Any other value, or a reference chain that is cyclic or longer
    /// than [`MAX_CHAIN`].
    Unusable,
}

/// Reads entry `key` of `dict` through `reads`.
///
/// # Errors
///
/// A failed read.
pub fn slot(reads: &mut Reads<'_>, dict: &Dict, key: &[u8]) -> Result<Slot> {
    let mut current = match dict.get(key) {
        None | Some(Value::Null) => return Ok(Slot::Absent),
        Some(value @ (Value::Dict(_) | Value::Array(_))) => {
            return Ok(Slot::Found(None, value.clone()));
        }
        Some(&Value::Reference(reference)) => reference,
        Some(_) => return Ok(Slot::Unusable),
    };
    for _ in 0..MAX_CHAIN {
        match object(reads, current)? {
            None | Some(Value::Null) => return Ok(Slot::Absent),
            Some(Value::Reference(next)) => current = next,
            Some(value @ (Value::Dict(_) | Value::Array(_))) => {
                return Ok(Slot::Found(Some(current.number()), value));
            }
            Some(_) => return Ok(Slot::Unusable),
        }
    }
    Ok(Slot::Unusable)
}

/// A source default resource that joins the receiving `/DR`.
pub struct Joined {
    /// The category of the resource.
    pub category: Name,
    /// The name of the resource in the receiving category, or `None` for a
    /// category that the receiving `/DR` lacks, which joins whole.
    pub name: Option<Name>,
    /// The source value, for the import to copy.
    pub value: Value,
}

/// Plans how the source default resources `source_resources` join the
/// receiving ones, `resources`, and returns the resources that join, the
/// entries of each category together, and the renames.
///
/// A source resource that is [`equivalent`] to the receiving resource of
/// its name is shared. Any other under a name that the receiving category
/// holds takes the smallest `_N` suffix that neither category uses. A
/// receiving category that is no dictionary keeps its value.
///
/// # Errors
///
/// A failed read.
pub fn merge(
    receiving: &mut Reads<'_>,
    resources: &Dict,
    source: &mut Reads<'_>,
    source_resources: &Dict,
) -> Result<(Vec<Joined>, Renames)> {
    let mut joined = Vec::new();
    let mut whole = Vec::new();
    let mut renames = Renames::new();
    for (category, value) in source_resources.iter() {
        let Some(source_category) = resolve(source, value.clone())? else {
            continue;
        };
        let receiving_category = match slot(receiving, resources, category.as_bytes())? {
            Slot::Absent => {
                whole.push(Joined {
                    category: category.clone(),
                    name: None,
                    value: value.clone(),
                });
                continue;
            }
            Slot::Found(_, Value::Dict(dict)) => dict,
            Slot::Found(..) | Slot::Unusable => continue,
        };
        let Value::Dict(source_category) = source_category else {
            continue;
        };
        let mut assigned = HashSet::new();
        let mut category_renames = HashMap::new();
        for (name, entry) in source_category.iter() {
            if resolve(source, entry.clone())?.is_none() {
                continue;
            }
            let occupied = match receiving_category.get(name.as_bytes()) {
                Some(existing) if resolve(receiving, existing.clone())?.is_some() => Some(existing),
                _ => None,
            };
            if let Some(existing) = occupied
                && equivalent(receiving, existing, source, entry)?
            {
                // The receiving resource already is this one, so imported
                // default appearance strings can keep naming it.
                continue;
            }
            let key = if occupied.is_some() {
                let renamed = unique(name, &receiving_category, &source_category, &assigned);
                assigned.insert(renamed.clone());
                category_renames.insert(name.clone(), renamed.clone());
                renamed
            } else {
                name.clone()
            };
            joined.push(Joined {
                category: category.clone(),
                name: Some(key),
                value: entry.clone(),
            });
        }
        if !category_renames.is_empty() {
            renames.insert(category.clone(), category_renames);
        }
    }
    joined.extend(whole);
    Ok((joined, renames))
}

/// Returns `name` followed by `_` and the smallest positive integer that
/// no entry of `receiving` or `source` and no name of `assigned` has.
fn unique(name: &Name, receiving: &Dict, source: &Dict, assigned: &HashSet<Name>) -> Name {
    let mut suffix = 1_usize;
    loop {
        let mut candidate = name.as_bytes().to_vec();
        candidate.push(b'_');
        candidate.extend_from_slice(suffix.to_string().as_bytes());
        let candidate = Name::new(candidate);
        if receiving.get(candidate.as_bytes()).is_none()
            && source.get(candidate.as_bytes()).is_none()
            && !assigned.contains(&candidate)
        {
            return candidate;
        }
        suffix += 1;
    }
}

/// Returns whether receiving resource `left` and source resource `right`
/// are alike.
///
/// References are followed on both sides, a pair of references that is
/// already under comparison counts as alike, and streams compare their
/// dictionaries and encoded data. Reading more than
/// [`MAX_COMPARED_OBJECTS`] objects or [`MAX_COMPARED_BYTES`] bytes counts
/// as different, so that an expensive pair is renamed rather than shared.
///
/// # Errors
///
/// A failed read.
pub fn equivalent(
    receiving: &mut Reads<'_>,
    left: &Value,
    source: &mut Reads<'_>,
    right: &Value,
) -> Result<bool> {
    let mut pending = vec![(left.clone(), right.clone())];
    let mut pairs: HashSet<(Reference, Reference)> = HashSet::new();
    let mut objects = 0_usize;
    let mut bytes = 0_usize;
    while let Some((left, right)) = pending.pop() {
        objects += 1;
        if objects > MAX_COMPARED_OBJECTS {
            return Ok(false);
        }
        if let (&Value::Reference(l), &Value::Reference(r)) = (&left, &right)
            && !pairs.insert((l, r))
        {
            continue;
        }
        let (left, right) = match (resolve(receiving, left)?, resolve(source, right)?) {
            (Some(left), Some(right)) => (left, right),
            (None, None) => continue,
            _ => return Ok(false),
        };
        let fits = |pending: &[(Value, Value)], more: usize| {
            objects.saturating_add(pending.len()).saturating_add(more) <= MAX_COMPARED_OBJECTS
        };
        let mut charge = |amount: usize| {
            bytes = bytes.saturating_add(amount);
            bytes <= MAX_COMPARED_BYTES
        };
        let alike = match (&left, &right) {
            (Value::Boolean(l), Value::Boolean(r)) => l == r,
            (Value::Integer(l), Value::Integer(r)) => l == r,
            (Value::Real(l), Value::Real(r)) => l == r,
            (Value::Name(l), Value::Name(r)) => charge(l.as_bytes().len()) && l == r,
            (Value::String(l), Value::String(r)) => {
                charge(l.as_bytes().len()) && l.as_bytes() == r.as_bytes()
            }
            (Value::Array(l), Value::Array(r)) => {
                let alike = l.len() == r.len() && fits(&pending, l.len());
                if alike {
                    pending.extend(l.iter().cloned().zip(r.iter().cloned()));
                }
                alike
            }
            (Value::Dict(l), Value::Dict(r)) => {
                fits(&pending, l.len()) && entries(l, r, &mut pending, &mut charge)
            }
            (Value::Stream(l), Value::Stream(r)) => {
                fits(&pending, l.dict().len())
                    && charge(l.data().len())
                    && l.data() == r.data()
                    && entries(l.dict(), r.dict(), &mut pending, &mut charge)
            }
            _ => false,
        };
        if !alike {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Queues the entry pairs of two dictionaries with the same keys, and
/// returns whether they have the same keys within the byte budget.
fn entries(
    left: &Dict,
    right: &Dict,
    pending: &mut Vec<(Value, Value)>,
    charge: &mut impl FnMut(usize) -> bool,
) -> bool {
    if left.len() != right.len() {
        return false;
    }
    for (key, value) in left.iter() {
        let Some(other) = right.get(key.as_bytes()) else {
            return false;
        };
        if !charge(key.as_bytes().len()) {
            return false;
        }
        pending.push((value.clone(), other.clone()));
    }
    true
}

/// Reads the object that `reference` names, of its generation.
fn object(reads: &mut Reads<'_>, reference: Reference) -> Result<Option<Value>> {
    Ok(reads
        .get(reference.number())?
        .filter(|object| object.generation() == reference.generation())
        .map(|object| (**object.value()).clone()))
}

/// Follows references from `value` through at most [`MAX_CHAIN`]; null,
/// a reference to null or to a missing object, and a cyclic or longer
/// chain are `None`.
fn resolve(reads: &mut Reads<'_>, value: Value) -> Result<Option<Value>> {
    let mut current = match value {
        Value::Null => return Ok(None),
        Value::Reference(reference) => reference,
        value => return Ok(Some(value)),
    };
    for _ in 0..MAX_CHAIN {
        match object(reads, current)? {
            None | Some(Value::Null) => return Ok(None),
            Some(Value::Reference(next)) => current = next,
            Some(value) => return Ok(Some(value)),
        }
    }
    Ok(None)
}

const fn is_whitespace(byte: u8) -> bool {
    matches!(byte, b'\0' | b'\t' | b'\n' | b'\x0c' | b'\r' | b' ')
}

const fn is_delimiter(byte: u8) -> bool {
    matches!(
        byte,
        b'(' | b')' | b'<' | b'>' | b'[' | b']' | b'{' | b'}' | b'/' | b'%'
    )
}

const fn is_regular(byte: u8) -> bool {
    !is_whitespace(byte) && !is_delimiter(byte)
}

/// Decodes the `#xx` escapes of a name token without its slash.
fn decode_name(token: &[u8]) -> Vec<u8> {
    let mut decoded = Vec::with_capacity(token.len());
    let mut index = 0;
    while index < token.len() {
        let hex = token
            .get(index + 1..index + 3)
            .filter(|_| token[index] == b'#')
            .and_then(|hex| std::str::from_utf8(hex).ok())
            .filter(|hex| hex.bytes().all(|byte| byte.is_ascii_hexdigit()))
            .and_then(|hex| u8::from_str_radix(hex, 16).ok());
        if let Some(byte) = hex {
            decoded.push(byte);
            index += 3;
        } else {
            decoded.push(token[index]);
            index += 1;
        }
    }
    decoded
}

/// Appends `name` as a name token, escaping its `#`, delimiter,
/// whitespace, and non-printable bytes.
fn encode_name(out: &mut Vec<u8>, name: &[u8]) {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    out.push(b'/');
    for &byte in name {
        if byte == b'#' || !is_regular(byte) || !(b'!'..=b'~').contains(&byte) {
            out.extend_from_slice(&[
                b'#',
                HEX[usize::from(byte >> 4)],
                HEX[usize::from(byte & 0x0f)],
            ]);
        } else {
            out.push(byte);
        }
    }
}

/// Renames the resources that default appearance string `appearance`
/// names, and returns the string again, or `None` when nothing changes.
///
/// The string is a content-stream fragment (ISO 32000-2 section 12.7.4.3).
/// Only an operand that names a `/DR` resource changes: the font of `Tf`,
/// the graphics state of `gs`, the colour space of `cs` and `CS`, and the
/// pattern of `scn` and `SCN`. Every other byte is copied. A UTF-16 string
/// is read as its text when that text is ASCII, and its rewrite is ASCII.
#[must_use]
pub fn rewrite(appearance: &[u8], renames: &Renames) -> Option<Vec<u8>> {
    if let Some(units) = appearance.strip_prefix(&[0xfe, 0xff]) {
        let (units, rest) = units.as_chunks::<2>();
        if !rest.is_empty() {
            return None;
        }
        let text = char::decode_utf16(units.iter().map(|unit| u16::from_be_bytes(*unit)))
            .collect::<std::result::Result<String, _>>()
            .ok()
            .filter(|text| text.is_ascii())?;
        return rewrite_bytes(text.as_bytes(), renames);
    }
    rewrite_bytes(appearance, renames)
}

fn rewrite_bytes(appearance: &[u8], renames: &Renames) -> Option<Vec<u8>> {
    #[derive(Clone, Copy)]
    enum Operand {
        Name(usize, usize),
        Other,
    }
    let mut replacements: Vec<(usize, usize, &Name)> = Vec::new();
    // The last two operands, the last first.
    let mut operands: [Option<Operand>; 2] = [None, None];
    fn operand(value: Operand, operands: &mut [Option<Operand>; 2]) {
        *operands = [Some(value), operands[0]];
    }
    let len = appearance.len();
    let mut index = 0;
    while index < len {
        let byte = appearance[index];
        if is_whitespace(byte) {
            index += 1;
            continue;
        }
        match byte {
            b'%' => {
                while index < len && !matches!(appearance[index], b'\r' | b'\n') {
                    index += 1;
                }
            }
            b'/' => {
                let start = index;
                index += 1;
                while index < len && is_regular(appearance[index]) {
                    index += 1;
                }
                operand(Operand::Name(start, index), &mut operands);
            }
            b'(' => {
                let mut depth = 0_usize;
                while index < len {
                    match appearance[index] {
                        b'\\' => index += 1,
                        b'(' => depth += 1,
                        b')' => {
                            depth -= 1;
                            if depth == 0 {
                                index += 1;
                                break;
                            }
                        }
                        _ => {}
                    }
                    index += 1;
                }
                operand(Operand::Other, &mut operands);
            }
            b'<' if appearance.get(index + 1) == Some(&b'<') => {
                index += 2;
                operand(Operand::Other, &mut operands);
            }
            b'<' => {
                while index < len && appearance[index] != b'>' {
                    index += 1;
                }
                index += 1;
                operand(Operand::Other, &mut operands);
            }
            b'>' | b')' | b'[' | b']' | b'{' | b'}' => {
                index += if appearance.get(index..index + 2) == Some(b">>") {
                    2
                } else {
                    1
                };
                operand(Operand::Other, &mut operands);
            }
            _ => {
                let start = index;
                while index < len && is_regular(appearance[index]) {
                    index += 1;
                }
                let token = &appearance[start..index];
                if matches!(token.first(), Some(b'0'..=b'9' | b'+' | b'-' | b'.'))
                    || matches!(token, b"true" | b"false" | b"null")
                {
                    operand(Operand::Other, &mut operands);
                    continue;
                }
                let [last, before_last] = operands;
                let named = match token {
                    b"Tf" => before_last.map(|operand| ("Font", operand)),
                    b"gs" => last.map(|operand| ("ExtGState", operand)),
                    b"cs" | b"CS" => last.map(|operand| ("ColorSpace", operand)),
                    b"scn" | b"SCN" => last.map(|operand| ("Pattern", operand)),
                    _ => None,
                };
                if let Some((category, Operand::Name(start, end))) = named {
                    let name = decode_name(&appearance[start + 1..end]);
                    let device = category == "ColorSpace"
                        && matches!(
                            name.as_slice(),
                            b"DeviceGray" | b"DeviceRGB" | b"DeviceCMYK" | b"Pattern"
                        );
                    if !device
                        && let Some(renamed) = renames
                            .get(&Name::from(category))
                            .and_then(|category| category.get(&Name::new(name)))
                    {
                        replacements.push((start, end, renamed));
                    }
                }
                operands = [None, None];
            }
        }
    }
    if replacements.is_empty() {
        return None;
    }
    let mut rewritten = Vec::with_capacity(appearance.len());
    let mut copied = 0;
    for (start, end, renamed) in replacements {
        rewritten.extend_from_slice(&appearance[copied..start]);
        encode_name(&mut rewritten, renamed.as_bytes());
        copied = end;
    }
    rewritten.extend_from_slice(&appearance[copied..]);
    Some(rewritten)
}
