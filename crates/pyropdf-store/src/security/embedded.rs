//! The /Type entry of an embedded file stream is optional (Table 44).
//! Its file specification's /EF or /RF still assigns it the /EFF default.

use std::sync::Arc;

use super::Handler;
use crate::budget::{Budget, BudgetVec};
use crate::index::Index;
use crate::value::{Dict, Reference, Value};
use crate::version::{Slot, Version};
use crate::{Error, Result};

/// Discover file-specification roles only for a document where they affect
/// crypt-filter selection. File-level dictionary names and references are
/// clear. Compressed file specifications are read after their container.
pub(crate) fn discover(
    index: &Index,
    version: Option<&Version>,
    budget: &Budget,
) -> Result<Arc<[Reference]>> {
    let mut files = BudgetVec::new(budget);
    let source = Source {
        index,
        version,
        budget,
    };
    for (number, entry) in index.entries() {
        budget.poll()?;
        if version.is_some_and(|v| v.slot(number).is_some()) {
            continue;
        }
        if let Some(value) = source.read(Reference::new(number, entry.generation()))? {
            source.visit(&value, &mut files, 0)?;
        }
    }
    if let Some(v) = version {
        for (_, slot) in v.slots() {
            budget.poll()?;
            if let Slot::Written(written) = slot {
                source.visit(written.value(budget)?.as_ref(), &mut files, 0)?;
            }
        }
    }
    let mut files = files.into_vec();
    files.sort_unstable();
    files.dedup();
    Ok(files.into())
}

struct Source<'a> {
    index: &'a Index,
    version: Option<&'a Version>,
    budget: &'a Budget,
}

impl Source<'_> {
    fn read(&self, reference: Reference) -> Result<Option<Arc<Value>>> {
        if let Some(v) = self.version
            && let Some(slot) = v.slot(reference.number())
        {
            return match slot {
                Slot::Written(written) if written.generation() == reference.generation() => {
                    written.value(self.budget).map(Some)
                }
                _ => Ok(None),
            };
        }
        let Some(bytes) = self.index.object(reference, self.budget)? else {
            return Ok(None);
        };
        Ok(Some(Arc::new(Value::parse(&bytes, self.budget)?)))
    }

    fn visit(&self, value: &Value, out: &mut BudgetVec<'_, Reference>, depth: usize) -> Result<()> {
        self.budget.poll()?;
        if depth > 256 {
            return Err(Error::malformed(
                "file-specification containers nest too deeply",
            ));
        }
        let dict = match value {
            Value::Dict(dict) => dict,
            Value::Stream(stream) => stream.dict(),
            Value::Array(array) => {
                for child in array {
                    self.visit(child, out, depth + 1)?;
                }
                return Ok(());
            }
            _ => return Ok(()),
        };
        if matches!(dict.get("Type"),Some(Value::Name(n)) if n.as_bytes()==b"Filespec")
            || dict.get("Type").is_none() && (dict.get("F").is_some() || dict.get("UF").is_some())
        {
            for field in ["EF", "RF"] {
                if let Some(entry) = dict.get(field) {
                    self.resolved(entry, |value| self.platform_files(value, field, out))?;
                }
            }
        }
        for (_, child) in dict.iter() {
            self.visit(child, out, depth + 1)?;
        }
        Ok(())
    }

    fn resolved(&self, value: &Value, mut apply: impl FnMut(&Value) -> Result<()>) -> Result<()> {
        let Value::Reference(mut reference) = *value else {
            return apply(value);
        };
        for _ in 0..64 {
            self.budget.poll()?;
            let item = self.read(reference)?.ok_or_else(|| {
                Error::malformed("a file-specification role has a dangling reference")
            })?;
            match item.as_ref() {
                Value::Reference(next) => reference = *next,
                found => return apply(found),
            }
        }
        Err(Error::malformed(
            "a file-specification role has a cyclic or excessive reference chain",
        ))
    }

    fn platform_files(
        &self,
        value: &Value,
        field: &str,
        out: &mut BudgetVec<'_, Reference>,
    ) -> Result<()> {
        let Value::Dict(links) = value else {
            return Err(Error::malformed(
                "a file specification's /EF or /RF must be a dictionary",
            ));
        };
        for name in ["F", "UF", "DOS", "Mac", "Unix"] {
            let Some(target) = links.get(name) else {
                continue;
            };
            if field == "EF" {
                let Value::Reference(reference) = target else {
                    return Err(Error::malformed(
                        "an /EF entry must reference an embedded stream",
                    ));
                };
                out.try_push(*reference)?;
            } else {
                self.resolved(target, |value| {
                    let Value::Array(array) = value else {
                        return Err(Error::malformed(
                            "a related-file platform entry must be an array",
                        ));
                    };
                    let (pairs, remainder) = array.as_slice().as_chunks::<2>();
                    if !remainder.is_empty() {
                        return Err(Error::malformed(
                            "a related-file array must contain name and stream pairs",
                        ));
                    }
                    for pair in pairs {
                        let Value::Reference(reference) = pair[1] else {
                            return Err(Error::malformed(
                                "a related-file pair must reference an embedded stream",
                            ));
                        };
                        out.try_push(reference)?;
                    }
                    Ok(())
                })?;
            }
        }
        Ok(())
    }
}

impl Handler {
    pub(crate) fn distinguishes_embedded_files(&self) -> bool {
        self.filters.embedded != self.filters.streams
    }

    pub(crate) fn remember_file_roles(&mut self, files: Arc<[Reference]>) {
        self.filters.file_roles = files;
    }

    pub(crate) fn embedded_stream(&self, reference: Reference, dict: &Dict) -> bool {
        match dict.get("Type") {
            Some(Value::Name(n)) => n.as_bytes() == b"EmbeddedFile",
            None | Some(Value::Null) => self.filters.file_roles.binary_search(&reference).is_ok(),
            _ => false,
        }
    }
}
