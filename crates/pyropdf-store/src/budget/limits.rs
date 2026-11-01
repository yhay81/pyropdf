//! The limits of a document's operations (DESIGN.md sections 9 and 10).

use std::time::Duration;

use super::{CancellationToken, LimitCode};

/// The limits that every operation on a document starts from: the Rust side
/// of Python's `DocumentLimits`.
///
/// The fields keep the names of pylopdf's `DocumentLimits`
/// (src/pylopdf/_limits.py and rust/src/document/limits.rs at
/// 884af0dbf21973cfd434a9eca4e769de62fcc25c) for the limits that Phase 0
/// enforces; `max_text_size` and `max_text_glyphs` come with text
/// extraction. `max_duration` and `cancellation` are new (DESIGN.md section
/// 10). `None` leaves a limit unset. [`Budget::new`](super::Budget::new)
/// gives each limit to the code that its documentation names.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DocumentLimits {
    /// The size of the input, in bytes (`file_size`).
    pub max_file_size: Option<u64>,
    /// The number of pages (`page_count`).
    pub max_pages: Option<u64>,
    /// The number of objects (`object_count`).
    pub max_objects: Option<u64>,
    /// The decoded size of any one stream, in bytes (`decompressed_size`).
    pub max_decompressed_size: Option<u64>,
    /// The decoded size of one content stream of a page, in bytes
    /// (`page_content_size`).
    pub max_page_content_size: Option<u64>,
    /// The decoded size of all the streams that one operation decodes, in
    /// bytes (`total_decompressed_size`).
    pub max_total_decompressed_size: Option<u64>,
    /// The nesting of arrays and dictionaries in one object, in levels
    /// (`object_depth`).
    pub max_object_depth: Option<u64>,
    /// The size of the PDF data handed to the interpreter, in bytes
    /// (`interpretation_size`).
    pub max_interpretation_size: Option<u64>,
    /// The memory that the collections of one operation may hold, in bytes
    /// (`memory_size`).
    pub max_memory_size: Option<u64>,
    /// The time that one operation may take (`deadline`).
    pub max_duration: Option<Duration>,
    /// A token that cancels every operation on the document (`cancelled`).
    pub cancellation: Option<CancellationToken>,
}

impl DocumentLimits {
    /// Returns pylopdf's conservative profile for user uploads in bounded
    /// workers, `DocumentLimits.web()`, for the limits that Phase 0 enforces.
    #[must_use]
    pub fn web() -> Self {
        const MIB: u64 = 1024 * 1024;
        Self {
            max_file_size: Some(10 * MIB),
            max_pages: Some(200),
            max_objects: Some(100_000),
            max_decompressed_size: Some(64 * MIB),
            max_page_content_size: Some(10 * MIB),
            max_total_decompressed_size: Some(128 * MIB),
            max_object_depth: Some(64),
            max_interpretation_size: Some(64 * MIB),
            // pylopdf's profile has no memory budget.
            max_memory_size: None,
            // pylopdf's profile has no time budget.
            max_duration: None,
            cancellation: None,
        }
    }

    /// Returns each counted limit of the document with the code that it
    /// sets.
    pub(super) const fn code_limits(&self) -> [(LimitCode, Option<u64>); 9] {
        [
            (LimitCode::FileSize, self.max_file_size),
            (LimitCode::PageCount, self.max_pages),
            (LimitCode::ObjectCount, self.max_objects),
            (LimitCode::DecompressedSize, self.max_decompressed_size),
            (LimitCode::PageContentSize, self.max_page_content_size),
            (
                LimitCode::TotalDecompressedSize,
                self.max_total_decompressed_size,
            ),
            (LimitCode::ObjectDepth, self.max_object_depth),
            (LimitCode::InterpretationSize, self.max_interpretation_size),
            (LimitCode::MemorySize, self.max_memory_size),
        ]
    }
}
