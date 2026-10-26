//! A Windows output file keeps its target's discretionary access control
//! list and inheritance, as pylopdf keeps them (rust/src/windows_permissions.rs
//! at 884af0db). `Document.save` copies the target's DACL to the sibling it
//! writes before writing any PDF data.

use std::fs::OpenOptions;
use std::io;
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::AsRawHandle;
use std::path::{Path, PathBuf};
use std::ptr;

use pyo3::prelude::*;
use windows_sys::Win32::Foundation::ERROR_INSUFFICIENT_BUFFER;
use windows_sys::Win32::Security::{
    DACL_SECURITY_INFORMATION, GetKernelObjectSecurity, SetKernelObjectSecurity,
};
use windows_sys::Win32::Storage::FileSystem::{READ_CONTROL, WRITE_DAC};

/// Copies the DACL of `source` to `destination`, verbatim.
fn copy_dacl(source: &Path, destination: &Path) -> io::Result<()> {
    // Rust's file opening already supports extended-length and UNC paths.
    let source = OpenOptions::new().access_mode(READ_CONTROL).open(source)?;
    let destination = OpenOptions::new()
        .access_mode(WRITE_DAC)
        .open(destination)?;
    let mut size = 0_u32;
    // SAFETY: both handles are owned files. A null, zero-length buffer asks
    // only for the size of the descriptor; the output pointer is valid.
    let success = unsafe {
        GetKernelObjectSecurity(
            source.as_raw_handle(),
            DACL_SECURITY_INFORMATION,
            ptr::null_mut(),
            0,
            &mut size,
        )
    };
    if success == 0 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() != Some(ERROR_INSUFFICIENT_BUFFER as i32) {
            return Err(error);
        }
    }
    if size == 0 {
        return Err(io::Error::other("empty file security descriptor"));
    }
    // A self-relative SECURITY_DESCRIPTOR needs DWORD alignment.
    let mut descriptor = Vec::<u32>::new();
    let words = (size as usize).div_ceil(size_of::<u32>());
    descriptor
        .try_reserve_exact(words)
        .map_err(io::Error::other)?;
    descriptor.resize(words, 0);
    // SAFETY: the aligned buffer holds at least `size` bytes; on success
    // Windows writes a complete self-relative descriptor into it.
    if unsafe {
        GetKernelObjectSecurity(
            source.as_raw_handle(),
            DACL_SECURITY_INFORMATION,
            descriptor.as_mut_ptr().cast(),
            size,
            &mut size,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    // Copy the stored descriptor verbatim, inheritance protection included:
    // Get/SetNamedSecurityInfo can merge the parent's entries.
    // SAFETY: the handle has WRITE_DAC and the descriptor stays alive.
    if unsafe {
        SetKernelObjectSecurity(
            destination.as_raw_handle(),
            DACL_SECURITY_INFORMATION,
            descriptor.as_mut_ptr().cast(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// Copies the DACL of `source` to `destination`, with the GIL released.
#[pyfunction]
pub fn _copy_windows_dacl(py: Python<'_>, source: PathBuf, destination: PathBuf) -> PyResult<()> {
    py.detach(|| copy_dacl(&source, &destination))?;
    Ok(())
}
