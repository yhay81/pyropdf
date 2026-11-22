//! Resolve producer-owned fonts in the AcroForm default resources. Open the
//! program and its PDF encoding only when a rich-text family requests it.
use super::rich_codes::{self, Code, Codes};
use super::{WidgetError, text};
use crate::annotations::Reader;
use crate::fonts::embed::{Face, Measure};
use crate::fonts::shape::{ShapeError, ShapedGlyph, ShapingFace};
use crate::standard::{self, Base14Font};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::{Dict, Name, Reference, Value};

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum PaintFont {
    Standard(Base14Font),
    Unavailable,
    Embedded(usize),
}

pub(super) struct RichFonts<'r, 'd> {
    pub(super) reader: &'r Reader<'d>,
    records: Vec<Record>,
    work: u64,
}

struct Record {
    resource: String,
    reference: Reference,
    base: String,
    family: String,
    postscript: String,
    bold: bool,
    italic: bool,
    loaded: Option<Option<Loaded>>,
}

struct Loaded {
    face: Face,
    shaper: ShapingFace,
    codes: Codes,
}

fn label(value: Option<&Value>, budget: &Budget) -> Result<String, WidgetError> {
    let value = match value {
        Some(Value::Name(n)) => n.as_bytes(),
        Some(Value::String(s)) => s.as_bytes(),
        _ => return Ok(String::new()),
    };
    budget.charge(
        LimitCode::MemorySize,
        (value.len() as u64).saturating_mul(4),
    )?;
    Ok(text::text_string(value))
}

fn unprefixed(name: &str) -> &str {
    let bytes = name.as_bytes();
    if bytes.get(6) == Some(&b'+') && bytes[..6].iter().all(u8::is_ascii_uppercase) {
        &name[7..]
    } else {
        name
    }
}

fn matches_name(record: &Record, name: &str) -> bool {
    [
        &record.resource,
        &record.base,
        &record.family,
        &record.postscript,
    ]
    .iter()
    .any(|n| unprefixed(n).eq_ignore_ascii_case(name))
}

fn kind(dict: &Dict) -> Option<&[u8]> {
    match dict.get("Subtype")? {
        Value::Name(n) => Some(n.as_bytes()),
        _ => None,
    }
}

/// A simple font owns its descriptor directly; a Type 0 font owns it through
/// its CIDFont. Keep indirect and direct dictionaries borrowed while reading.
fn with_descriptor<T>(
    reader: &Reader<'_>,
    font: &Dict,
    read: impl FnOnce(&Dict, Option<&Dict>) -> Result<Option<T>, WidgetError>,
) -> Result<Option<T>, WidgetError> {
    let descendants;
    let value;
    let descendant = match kind(font) {
        Some(b"TrueType" | b"Type1") => None,
        Some(b"Type0") => {
            descendants = reader.entry(font, "DescendantFonts")?;
            let Some(Value::Array(children)) = descendants.as_deref() else {
                return Ok(None);
            };
            let Some(child) = children.get(0) else {
                return Ok(None);
            };
            value = reader.resolve(child)?;
            let Value::Dict(d) = &*value else {
                return Ok(None);
            };
            if !matches!(kind(d), Some(b"CIDFontType2" | b"CIDFontType0")) {
                return Ok(None);
            }
            Some(d)
        }
        _ => return Ok(None),
    };
    let descriptor = reader.entry(descendant.unwrap_or(font), "FontDescriptor")?;
    let Some(Value::Dict(desc)) = descriptor.as_deref() else {
        return Ok(None);
    };
    if !["FontFile", "FontFile2", "FontFile3"]
        .iter()
        .any(|k| desc.get(k).is_some())
    {
        return Ok(None);
    }
    read(desc, descendant)
}

fn optional_font<T>(result: Result<Option<T>, WidgetError>) -> Result<Option<T>, WidgetError> {
    match result {
        Err(WidgetError::Store(e)) if e.limit().is_none() => Ok(None),
        Err(WidgetError::Malformed(_)) => Ok(None),
        result => result,
    }
}

impl Record {
    fn read(reader: &Reader<'_>, name: &Name, value: &Value) -> Result<Option<Self>, WidgetError> {
        let Some((reference, font)) = reader.resolve_object(value)? else {
            return Ok(None);
        };
        let Value::Dict(font) = &*font else {
            return Ok(None);
        };
        with_descriptor(reader, font, |desc, _| {
            let base = label(font.get("BaseFont"), reader.budget)?;
            let family = label(desc.get("FontFamily"), reader.budget)?;
            let postscript = label(desc.get("FontName"), reader.budget)?;
            let bold = matches!(desc.get("FontWeight"), Some(Value::Integer(weight)) if *weight >= 600)
                || base
                    .as_bytes()
                    .windows(4)
                    .any(|w| w.eq_ignore_ascii_case(b"bold"))
                || matches!(desc.get("Flags"), Some(Value::Integer(flags)) if flags & (1 << 18) != 0);
            let italic = reader
                .number(desc.get("ItalicAngle").unwrap_or(&Value::Null))?
                .is_some_and(|v| v != 0.)
                || matches!(desc.get("Flags"), Some(Value::Integer(flags)) if flags & 64 != 0);
            reader
                .budget
                .charge(LimitCode::MemorySize, name.as_bytes().len() as u64)?;
            let Ok(resource) = std::str::from_utf8(name.as_bytes()) else {
                return Ok(None);
            };
            Ok(Some(Record {
                resource: resource.to_owned(),
                reference,
                base,
                family,
                postscript,
                bold,
                italic,
                loaded: None,
            }))
        })
    }
}

pub(super) struct Candidates {
    order: Vec<PaintFont>,
    fallback: PaintFont,
}

impl<'r, 'd> RichFonts<'r, 'd> {
    pub(super) fn read(reader: &'r Reader<'d>, form: Option<&Dict>) -> Result<Self, WidgetError> {
        let mut records = BudgetVec::new(reader.budget);
        let resources = form.map(|f| reader.entry(f, "DR")).transpose()?.flatten();
        if let Some(Value::Dict(dr)) = resources.as_deref()
            && let Some(Value::Dict(fonts)) = reader.entry(dr, "Font")?.as_deref()
        {
            for (name, value) in fonts.iter() {
                reader.budget.poll()?;
                let record = optional_font(Record::read(reader, name, value))?;
                if let Some(record) = record {
                    records.try_push(record)?;
                }
            }
        }
        Ok(Self {
            reader,
            records: records.into_vec(),
            work: 0,
        })
    }

    fn load(&mut self, reader: &Reader<'_>, index: usize) -> Result<Option<&Loaded>, WidgetError> {
        let record = &mut self.records[index];
        if record.loaded.is_none() {
            record.loaded = Some(optional_font(Loaded::read(reader, record.reference))?);
        }
        Ok(record.loaded.as_ref().and_then(Option::as_ref))
    }

    /// Bound the product of declared families, resources and requested glyphs
    /// even when the caller has not configured an operation deadline.
    fn charge(&mut self, amount: u64) -> Result<(), WidgetError> {
        const MAX_LOOKUP_WORK: u64 = 16 * 1024 * 1024;
        self.work = self.work.saturating_add(amount);
        if self.work > MAX_LOOKUP_WORK {
            return Err(LimitExceeded::new(LimitCode::InterpretationFuel)
                .with_limit(MAX_LOOKUP_WORK)
                .with_amount(self.work)
                .into());
        }
        self.reader
            .budget
            .charge(LimitCode::InterpretationFuel, amount)?;
        Ok(())
    }

    /// Resolve the CSS list and style once per span, preserving its order.
    pub(super) fn candidates(
        &mut self,
        families: &str,
        style: super::rich_style::Style,
    ) -> Result<Candidates, WidgetError> {
        let mut order = BudgetVec::new(self.reader.budget);
        let mut matched = false;
        for family in families
            .split(',')
            .map(|f| f.trim().trim_matches(['\'', '"']))
        {
            self.charge(
                (family.len().max(1) as u64).saturating_mul(1 + 2 * self.records.len() as u64),
            )?;
            for exact in [true, false] {
                for (i, record) in self.records.iter().enumerate() {
                    if ((record.bold, record.italic) == (style.bold, style.italic)) == exact
                        && matches_name(record, family)
                    {
                        matched = true;
                        order.try_push(PaintFont::Embedded(i))?;
                    }
                }
            }
            if let Some(base) = super::rich_style::standard_family(family) {
                order.try_push(PaintFont::Standard(style.with_family(base).font()))?;
            }
        }
        let fallback = if matched {
            PaintFont::Unavailable
        } else {
            PaintFont::Standard(style.font())
        };
        Ok(Candidates {
            order: order.into_vec(),
            fallback,
        })
    }

    pub(super) fn select(
        &mut self,
        candidates: &Candidates,
        character: char,
    ) -> Result<PaintFont, WidgetError> {
        for &candidate in &candidates.order {
            self.charge(1)?;
            let supported = match candidate {
                PaintFont::Embedded(i) => self.load(self.reader, i)?.is_some_and(|font| {
                    Measure::new(&font.face)
                        .glyph(character)
                        .is_some_and(|gid| font.codes.code(gid.to_u32()).is_some())
                }),
                PaintFont::Standard(font) => standard::glyph_code(font, character).is_some(),
                PaintFont::Unavailable => false,
            };
            if supported {
                return Ok(candidate);
            }
        }
        Ok(candidates.fallback)
    }

    fn loaded(&self, id: usize) -> &Loaded {
        self.records[id]
            .loaded
            .as_ref()
            .and_then(Option::as_ref)
            .expect("selected a loaded form font")
    }

    pub(super) fn shape<'b>(
        &self,
        id: usize,
        text: &str,
        budget: &'b Budget,
    ) -> Result<BudgetVec<'b, ShapedGlyph>, WidgetError> {
        self.loaded(id)
            .shaper
            .shape(text, budget)
            .map_err(|e| match e {
                ShapeError::Limit(e) => WidgetError::from(e),
                ShapeError::Font(reason) => WidgetError::Malformed(reason.into()),
            })
    }
    pub(super) fn code(
        &self,
        id: usize,
        gid: u32,
        text: &str,
    ) -> Result<Option<(Code, bool)>, WidgetError> {
        self.loaded(id).codes.select(gid, text, self.reader.budget)
    }
    pub(super) fn em(&self, id: usize) -> f64 {
        f64::from(self.loaded(id).face.units_per_em)
    }
    pub(super) fn metrics(&self, id: usize) -> (f64, f64) {
        let face = &self.loaded(id).face;
        let em = f64::from(face.units_per_em);
        (face.ascender / em, -face.descender / em)
    }
    pub(super) fn reference(&self, id: usize) -> Reference {
        self.records[id].reference
    }
}

impl Loaded {
    fn read(reader: &Reader<'_>, reference: Reference) -> Result<Option<Self>, WidgetError> {
        let Some((_, font)) = reader.resolve_object(&Value::Reference(reference))? else {
            return Ok(None);
        };
        let Value::Dict(font) = &*font else {
            return Ok(None);
        };
        with_descriptor(reader, font, |desc, descendant| {
            let Some(raw) = desc.get("FontFile2").or_else(|| desc.get("FontFile3")) else {
                return Ok(None);
            };
            let stream = reader.resolve(raw)?;
            let Value::Stream(stream) = &*stream else {
                return Ok(None);
            };
            let data = match stream.decoded(reader.budget) {
                Ok(data) => data,
                Err(e) if e.limit().is_none() => return Ok(None),
                Err(e) => return Err(e.into()),
            };
            let n = data.len() as u64;
            reader.budget.check(LimitCode::FontInputSize, n)?;
            reader
                .budget
                .charge(LimitCode::MemorySize, n.saturating_mul(3))?;
            let Ok(face) = Face::read(&data, 0) else {
                return Ok(None);
            };
            let Ok(shaper) = ShapingFace::of(&face) else {
                return Ok(None);
            };
            let codes = match descendant {
                Some(descendant) => rich_codes::read(reader, font, descendant, &face)?,
                None => rich_codes::simple(reader, font, &face)?,
            };
            let Some(mut codes) = codes else {
                return Ok(None);
            };
            codes.read_unicode(reader, font)?;
            Ok(Some(Self {
                face,
                shaper,
                codes,
            }))
        })
    }
}
