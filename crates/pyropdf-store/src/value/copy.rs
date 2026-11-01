//! Fallible, budgeted copies of owned values, including their lexemes.

use std::alloc::{Layout, alloc};
use std::mem::MaybeUninit;
use std::ptr::{self, NonNull};

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::{LimitExceeded, Result};

use super::{Array, Dict, Name, PdfString, Stream, Value};

/// Copies a value for editing. Each container consumes at most one of the
/// parser's 64 levels of nesting, including for values constructed by callers.
pub(crate) fn copy_for_edit(value: &Value, budget: &Budget) -> Result<Value> {
    copy_value(value, budget, 1)
}

/// Copies a dictionary for editing with the same depth accounting as a root
/// dictionary value.
pub(crate) fn copy_dict_for_edit(dict: &Dict, budget: &Budget) -> Result<Dict> {
    check_depth(budget, 1)?;
    copy_dict(dict, budget, 1)
}

fn check_depth(budget: &Budget, depth: u64) -> Result<()> {
    budget.check(LimitCode::ObjectDepth, depth)?;
    if depth > 64 {
        return Err(LimitExceeded::over(LimitCode::ObjectDepth, 64, depth).into());
    }
    Ok(())
}

fn copy_value(value: &Value, budget: &Budget, depth: u64) -> Result<Value> {
    budget.poll()?;
    Ok(match value {
        Value::Null => Value::Null,
        Value::Boolean(value) => Value::Boolean(*value),
        Value::Integer(value) => Value::Integer(*value),
        Value::Real(value) => Value::Real(value.clone()),
        Value::Name(name) => Value::Name(copy_name(name, budget)?),
        Value::String(string) => Value::String(PdfString {
            bytes: copy_bytes(&string.bytes, budget)?,
            form: string.form,
        }),
        Value::Reference(reference) => Value::Reference(*reference),
        Value::Array(array) => {
            check_depth(budget, depth)?;
            let mut items = BudgetVec::new(budget);
            items.try_reserve_exact(array.items.len())?;
            for child in &array.items {
                items.try_push(copy_value(child, budget, depth + 1)?)?;
            }
            Value::Array(Array {
                items: items.into_vec(),
                lexeme: array.lexeme.clone(),
            })
        }
        Value::Dict(dict) => {
            check_depth(budget, depth)?;
            Value::Dict(copy_dict(dict, budget, depth)?)
        }
        Value::Stream(stream) => {
            check_depth(budget, depth)?;
            let stream = Stream {
                dict: copy_dict(&stream.dict, budget, depth)?,
                data: stream.data.clone(),
            };
            Value::Stream(try_box(stream, budget)?)
        }
    })
}

fn copy_dict(dict: &Dict, budget: &Budget, depth: u64) -> Result<Dict> {
    // Existing edit callers may add inherited attributes or a Parent entry.
    // Reserve the same headroom that the former preflight charged, so those
    // inserts do not allocate after the fallible copy has returned.
    let capacity = dict
        .entries
        .len()
        .checked_add(8)
        .and_then(|n| n.checked_mul(2))
        .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
    let mut entries = BudgetVec::new(budget);
    entries.try_reserve_exact(capacity)?;
    for (key, child) in &dict.entries {
        entries.try_push((
            copy_name(key, budget)?,
            copy_value(child, budget, depth + 1)?,
        ))?;
    }
    Ok(Dict {
        entries: entries.into_vec(),
        lexeme: dict.lexeme.clone(),
    })
}

fn copy_name(name: &Name, budget: &Budget) -> Result<Name> {
    Ok(Name(copy_bytes(&name.0, budget)?))
}

/// Allocate with the global allocator without its infallible OOM handler.
/// The caller owns the resulting allocation and must initialize and free it.
fn allocate(layout: Layout, budget: &Budget) -> Result<NonNull<u8>> {
    let size = u64::try_from(layout.size()).unwrap_or(u64::MAX);
    budget.charge(LimitCode::MemorySize, size)?;
    // SAFETY: Every caller supplies a valid nonzero layout. A null result is
    // handled as a typed limit error, and successful pointers are transferred
    // to a Box whose layout exactly matches this allocation.
    let Some(ptr) = NonNull::new(unsafe { alloc(layout) }) else {
        budget.release(LimitCode::MemorySize, size);
        let amount = budget.used(LimitCode::MemorySize).saturating_add(size);
        return Err(LimitExceeded::new(LimitCode::MemorySize)
            .with_amount(amount)
            .into());
    };
    Ok(ptr)
}

fn try_box(value: Stream, budget: &Budget) -> Result<Box<Stream>> {
    let ptr = allocate(Layout::new::<Stream>(), budget)?.cast::<Stream>();
    // SAFETY: `ptr` is a fresh, aligned allocation of exactly this layout.
    // Writing initializes its only value and Box takes over its allocation.
    unsafe {
        ptr.as_ptr().write(value);
        Ok(Box::from_raw(ptr.as_ptr()))
    }
}

fn copy_bytes(bytes: &[u8], budget: &Budget) -> Result<Box<[u8]>> {
    budget.poll()?;
    if bytes.is_empty() {
        return Ok(Box::default());
    }
    let layout =
        Layout::array::<u8>(bytes.len()).map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
    let ptr = allocate(layout, budget)?.cast::<MaybeUninit<u8>>();
    let slice = ptr::slice_from_raw_parts_mut(ptr.as_ptr(), bytes.len());
    // SAFETY: The slice covers the fresh allocation exactly, and
    // MaybeUninit<u8> may hold uninitialized bytes. Taking ownership now also
    // frees the allocation if cancellation interrupts the copy.
    let mut out = unsafe { Box::from_raw(slice) };
    const CHUNK: usize = 64 * 1024;
    for (index, chunk) in bytes.chunks(CHUNK).enumerate() {
        if let Err(refusal) = budget.poll() {
            drop(out);
            budget.release(LimitCode::MemorySize, bytes.len() as u64);
            return Err(refusal.into());
        }
        // SAFETY: The chunk belongs to the source, is disjoint from our new
        // allocation, and the offset and length lie within its allocation.
        unsafe {
            ptr::copy_nonoverlapping(
                chunk.as_ptr(),
                out.as_mut_ptr().add(index * CHUNK).cast::<u8>(),
                chunk.len(),
            );
        }
    }
    // SAFETY: Every byte in `out` was initialized by the chunks above.
    Ok(unsafe { out.assume_init() })
}

#[cfg(test)]
mod tests {
    use std::alloc::dealloc;

    use super::*;

    #[test]
    fn an_allocator_refusal_is_typed_and_releases_its_charge() {
        let budget = Budget::default().with_limit(LimitCode::MemorySize, None);
        // This valid layout is too large for most allocators to provide. If a
        // platform can provide it, free it without touching its pages.
        let layout = Layout::from_size_align(isize::MAX as usize, 1).unwrap();
        let used = budget.used(LimitCode::MemorySize);
        match allocate(layout, &budget) {
            Err(error) => {
                assert_eq!(
                    error.limit().map(LimitExceeded::code),
                    Some(LimitCode::MemorySize)
                );
                assert_eq!(budget.used(LimitCode::MemorySize), used);
            }
            Ok(ptr) => {
                // SAFETY: `ptr` came from `allocate` with this exact layout
                // and has not been freed or transferred to another owner.
                unsafe { dealloc(ptr.as_ptr(), layout) };
                budget.release(LimitCode::MemorySize, layout.size() as u64);
            }
        }
    }
}
