//! Open an embedded font as the outline format its PDF font declares.
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, LimitCode};
use read_fonts::ps::type1::Type1Font;
use skrifa::raw::TableProvider;
use skrifa::raw::ps::cff::CffFontRef;
use skrifa::{FontRef, GlyphId, Tag};

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) enum ProgramFormat {
    Type1,
    TrueType,
    Cff { composite: bool },
    OpenTypeCff { composite: bool },
    Incompatible,
}

impl ProgramFormat {
    pub(crate) fn declared(font: &[u8], key: &str, subtype: Option<&[u8]>) -> Self {
        match (font, key, subtype) {
            (b"Type1" | b"MMType1", "FontFile", _) => Self::Type1,
            (b"TrueType" | b"CIDFontType2", "FontFile2", _) => Self::TrueType,
            (b"TrueType" | b"CIDFontType2", "FontFile3", Some(b"OpenType")) => Self::TrueType,
            (b"Type1" | b"MMType1", "FontFile3", Some(b"Type1C")) => Self::Cff { composite: false },
            (b"CIDFontType0", "FontFile3", Some(b"CIDFontType0C")) => Self::Cff { composite: true },
            (b"Type1" | b"MMType1", "FontFile3", Some(b"OpenType")) => {
                Self::OpenTypeCff { composite: false }
            }
            (b"CIDFontType0", "FontFile3", Some(b"OpenType")) => {
                Self::OpenTypeCff { composite: true }
            }
            _ => Self::Incompatible,
        }
    }

    pub(crate) fn readable(self, data: &[u8], budget: &Budget) -> Result<bool, LimitExceeded> {
        let size = data.len() as u64;
        budget.check(LimitCode::FontInputSize, size)?;
        match self {
            Self::Type1 => {
                // The same Fontations parser as Type 1 subsetting retains the decrypted
                // bytes, names, charstrings and encoding while opening the program.
                let scratch = size.saturating_mul(32);
                budget.charge(LimitCode::MemorySize, scratch)?;
                let valid = Type1Font::new(data).is_ok_and(|font| font.num_glyphs() != 0);
                budget.release(LimitCode::MemorySize, scratch);
                budget.poll()?;
                Ok(valid)
            }
            Self::Cff { composite } => cff(data, composite, budget),
            Self::TrueType | Self::OpenTypeCff { .. } => self.sfnt(data, budget),
            Self::Incompatible => Ok(false),
        }
    }

    fn sfnt(self, data: &[u8], budget: &Budget) -> Result<bool, LimitExceeded> {
        let Ok(font) = FontRef::new(data) else {
            return Ok(false);
        };
        // A readable directory can still declare a table beyond a truncated file.
        for table in font.table_directory().table_records() {
            budget.poll()?;
            if u64::from(table.offset()) + u64::from(table.length()) > data.len() as u64 {
                return Ok(false);
            }
        }
        if let Self::OpenTypeCff { composite } = self {
            // PDF permits a CIDFontType0 to use a name-keyed OpenType CFF.
            // Only the CFF and cmap tables are required for this embedding.
            let Some(table) = font.table_data(Tag::new(b"CFF ")) else {
                return Ok(false);
            };
            if font.cmap().is_err() {
                return Ok(false);
            }
            return cff(table.as_bytes(), composite, budget);
        }
        let (Ok(head), Ok(maxp)) = (font.head(), font.maxp()) else {
            return Ok(false);
        };
        let count = maxp.num_glyphs();
        if head.magic_number() != 0x5F0F3CF5 || count == 0 || font.hmtx().is_err() {
            return Ok(false);
        }
        let (Ok(loca), Ok(glyf)) = (font.loca(None), font.glyf()) else {
            return Ok(false);
        };
        if loca.len() < usize::from(count) || !matches!(head.index_to_loc_format(), 0 | 1) {
            return Ok(false);
        }
        let mut previous = 0;
        for i in 0..=usize::from(count) {
            if i % 256 == 0 {
                budget.poll()?;
            }
            let Some(offset) = loca.get_raw(i) else {
                return Ok(false);
            };
            if offset < previous
                || u64::from(offset) > glyf.offset_data().len() as u64
                || (i != 0 && loca.get(GlyphId::new((i - 1) as u32), &glyf).is_none())
            {
                return Ok(false);
            }
            previous = offset;
        }
        Ok(true)
    }
}

fn cff(data: &[u8], composite: bool, budget: &Budget) -> Result<bool, LimitExceeded> {
    let Ok(font) = CffFontRef::new_cff(data, 0, None) else {
        return Ok(false);
    };
    let count = font.num_glyphs();
    if count == 0 || (!composite && font.is_cid()) {
        return Ok(false);
    }
    for i in 0..count {
        if i % 256 == 0 {
            budget.poll()?;
        }
        if font
            .subfont_index(GlyphId::new(i))
            .is_none_or(|f| f >= font.num_subfonts())
            || font
                .charstrings()
                .get(i as usize)
                .is_none_or(<[u8]>::is_empty)
        {
            return Ok(false);
        }
    }
    for i in 0..font.num_subfonts() {
        budget.poll()?;
        if font.subfont(i, &[]).is_err() {
            return Ok(false);
        }
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyropdf_store::budget::{CancellationToken, DocumentLimits};

    #[test]
    fn opening_a_program_honours_the_font_input_limit() {
        let font = include_bytes!("testdata/NotoSansHebrew-subset.ttf");
        let budget = Budget::new(&DocumentLimits::web())
            .with_limit(LimitCode::FontInputSize, Some(font.len() as u64 - 1));
        let error = ProgramFormat::TrueType.readable(font, &budget).unwrap_err();
        assert_eq!(error.code(), LimitCode::FontInputSize);
    }

    #[test]
    fn type1_parser_memory_and_cancellation_are_checked_before_opening() {
        let budget = Budget::new(&DocumentLimits::web()).with_limit(LimitCode::MemorySize, Some(4));
        let error = ProgramFormat::Type1
            .readable(b"fontdata", &budget)
            .unwrap_err();
        assert_eq!(error.code(), LimitCode::MemorySize);
        let token = CancellationToken::new();
        let budget = budget.with_cancellation(token.clone());
        token.cancel();
        let error = ProgramFormat::TrueType.readable(b"", &budget).unwrap_err();
        assert_eq!(error.code(), LimitCode::Cancelled);
    }
}
