//! The Type 3 fonts of an embedded face (DESIGN.md section 7), for what a
//! CIDFont cannot paint: a glyph procedure paints it in its own colours, and
//! a ToUnicode CMap reads it back as text.
//!
//! A font holds up to [`GLYPHS_PER_FONT`] glyphs, one for each one-byte code
//! in the order that drawings first used them, and a document holds as many
//! fonts as a face's drawn glyphs need. What the fonts hold is read from the
//! document each time ([`Fonts::read`]), so a drawing adds its glyphs in its
//! own transaction, and undoing or dropping it removes them. Only what a
//! drawing used is ever written, so a save has nothing to subset.
//!
//! The writer takes each glyph's name, procedure, and resources, and knows
//! nothing of what the procedure paints.

use pyropdf_store::Result;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::save::deflate;
use pyropdf_store::value::{Array, Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::{Transaction, Version};

#[cfg(test)]
mod tests;

/// The glyphs of one font: its codes have one byte.
pub const GLYPHS_PER_FONT: usize = 256;

/// UTF-16 code units of the longest text that a ToUnicode entry maps to,
/// as for a Type0 font.
pub const MAX_MAPPED_UNITS: usize = 256;

/// The text of a glyph without text of its own, as for a Type0 font: WORD
/// JOINER, invisible and no word break. Real text replaces it.
pub const NO_TEXT: &str = "\u{2060}";

/// How much of the operation's memory a held glyph's name and text cost,
/// beyond their bytes.
const GLYPH_COST: u64 = 128;

/// The font and code that show a glyph.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Code {
    /// The object number of the font.
    pub font: u32,
    /// The code in the font.
    pub code: u8,
}

/// One glyph to add to a face's fonts.
#[derive(Clone, Debug)]
pub struct Glyph {
    /// The glyph's name in `CharProcs`, which identifies it among the fonts.
    pub name: Name,
    /// The advance, in glyph space.
    pub width: f64,
    /// The ink, in glyph space: `[x0, y0, x1, y1]`. `None` for no ink.
    pub ink: Option<[f64; 4]>,
    /// The procedure, which starts with `d0` or `d1` and this width.
    pub procedure: Vec<u8>,
    /// What the procedure names, as `(category, name, value)`: an image is
    /// `(XObject, name, reference)`. A font's glyphs share one dictionary,
    /// so a name stands for the same value in all of them.
    pub resources: Vec<(Name, Name, Value)>,
}

/// One font as the document holds it.
struct Font {
    number: u32,
    /// The font dictionary, whose other entries stay as they are.
    dict: Dict,
    to_unicode: Reference,
    names: Vec<Name>,
    widths: Vec<f64>,
    procedures: Dict,
    resources: Dict,
    ink: Option<[f64; 4]>,
    /// The text of each code; empty until a drawing gives it one.
    texts: Vec<String>,
    /// Whether the dictionary and the CMap differ from the document's.
    changed: bool,
    text_changed: bool,
}

/// The Type 3 fonts of one face in one version.
pub struct Fonts<'b> {
    budget: &'b Budget,
    em: u16,
    fonts: BudgetVec<'b, Font>,
    /// Every held name with its font's index and code, sorted by name.
    index: BudgetVec<'b, (Name, u32, u8)>,
    /// The memory charged for what the fonts hold, returned on drop.
    charged: u64,
}

impl Drop for Fonts<'_> {
    fn drop(&mut self) {
        self.budget.release(LimitCode::MemorySize, self.charged);
    }
}

impl<'b> Fonts<'b> {
    /// Reads the fonts `numbers` of a face of `em` units per em from
    /// `version`. A number that no longer holds a font of this shape, which
    /// an undo or another write may leave, is passed over.
    pub fn read(version: &Version, numbers: &[u32], em: u16, budget: &'b Budget) -> Result<Self> {
        let mut fonts = Self {
            budget,
            em,
            fonts: BudgetVec::new(budget),
            index: BudgetVec::new(budget),
            charged: 0,
        };
        for &number in numbers {
            budget.poll()?;
            if fonts.fonts.iter().any(|font| font.number == number) {
                continue;
            }
            let Some(font) = Font::read(version, number, budget)? else {
                continue;
            };
            let at = u32::try_from(fonts.fonts.len()).unwrap_or(u32::MAX);
            fonts.charge(&font)?;
            fonts.index.try_reserve(font.names.len())?;
            for (code, name) in font.names.iter().enumerate() {
                let code = u8::try_from(code).unwrap_or(u8::MAX);
                fonts.index.try_push((name.clone(), at, code))?;
            }
            fonts.fonts.try_push(font)?;
        }
        fonts.index.sort_unstable_by(|a, b| a.0.cmp(&b.0));
        Ok(fonts)
    }

    fn charge(&mut self, font: &Font) -> Result<()> {
        let text: usize = font.texts.iter().map(String::len).sum();
        let names: usize = font.names.iter().map(|name| name.as_bytes().len()).sum();
        let count = u64::try_from(font.names.len()).unwrap_or(u64::MAX);
        let size = count
            .saturating_mul(GLYPH_COST)
            .saturating_add(u64::try_from(text.saturating_add(names)).unwrap_or(u64::MAX));
        self.budget.charge(LimitCode::MemorySize, size)?;
        self.charged = self.charged.saturating_add(size);
        Ok(())
    }

    /// Returns the object numbers of the fonts, in the order they fill.
    pub fn numbers(&self) -> impl Iterator<Item = u32> + '_ {
        self.fonts.iter().map(|font| font.number)
    }

    /// Returns the font and code of the glyph `name`, if a font holds it.
    #[must_use]
    pub fn find(&self, name: &[u8]) -> Option<Code> {
        let at = self
            .index
            .binary_search_by(|(held, ..)| held.as_bytes().cmp(name))
            .ok()?;
        let (_, font, code) = &self.index[at];
        let font = self.fonts.get(usize::try_from(*font).ok()?)?;
        Some(Code {
            font: font.number,
            code: *code,
        })
    }

    /// Returns the text of `code`, if a drawing gave it one.
    #[must_use]
    pub fn text(&self, code: Code) -> Option<&str> {
        self.font_of(code.font)
            .and_then(|at| self.fonts[at].texts.get(usize::from(code.code)))
            .map(String::as_str)
            .filter(|text| !text.is_empty())
    }

    fn font_of(&self, number: u32) -> Option<usize> {
        self.fonts.iter().position(|font| font.number == number)
    }

    /// Adds `glyphs`, whose names are distinct and which no font holds yet,
    /// in the first font that has room and then in new ones. Each procedure
    /// becomes a stream object of `transaction`. Refuses with what the budget
    /// refuses.
    pub fn add(&mut self, transaction: &mut Transaction, glyphs: Vec<Glyph>) -> Result<()> {
        self.index.try_reserve(glyphs.len())?;
        for glyph in glyphs {
            self.budget.poll()?;
            let at = self.with_room(transaction)?;
            let font = &mut self.fonts[at];
            let code = u8::try_from(font.names.len()).unwrap_or(u8::MAX);
            let procedure = transaction.allocate()?;
            let stream = stream_of(&glyph.procedure, self.budget)?;
            transaction.write(procedure.number(), &Value::from(stream), self.budget)?;
            font.procedures
                .insert(glyph.name.clone(), Value::Reference(procedure));
            for (category, name, value) in glyph.resources {
                insert_resource(&mut font.resources, category, name, value);
            }
            font.ink = union(font.ink, glyph.ink);
            font.widths.push(glyph.width);
            font.names.push(glyph.name.clone());
            font.texts.push(String::new());
            font.changed = true;
            let size = GLYPH_COST.saturating_add(glyph.name.as_bytes().len() as u64);
            self.budget.charge(LimitCode::MemorySize, size)?;
            self.charged = self.charged.saturating_add(size);
            let index = u32::try_from(at).unwrap_or(u32::MAX);
            self.index.try_push((glyph.name, index, code))?;
        }
        self.index.sort_unstable_by(|a, b| a.0.cmp(&b.0));
        Ok(())
    }

    /// Returns the index of the font that takes the next glyph.
    fn with_room(&mut self, transaction: &mut Transaction) -> Result<usize> {
        if let Some(at) = self
            .fonts
            .iter()
            .position(|font| font.names.len() < GLYPHS_PER_FONT)
        {
            return Ok(at);
        }
        self.fonts.try_push(Font::new(transaction, self.em)?)?;
        Ok(self.fonts.len() - 1)
    }

    /// Maps the glyph `code` to `text` in the ToUnicode CMap, unless it maps
    /// to real text already, or `text` is empty or too long for an entry.
    pub fn map_text(&mut self, code: Code, text: &str) -> Result<()> {
        let Some(at) = self.font_of(code.font) else {
            return Ok(());
        };
        let font = &mut self.fonts[at];
        let Some(slot) = font.texts.get_mut(usize::from(code.code)) else {
            return Ok(());
        };
        if !(slot.is_empty() || slot == NO_TEXT)
            || slot == text
            || text.is_empty()
            || text.encode_utf16().count() > MAX_MAPPED_UNITS
        {
            return Ok(());
        }
        self.budget
            .charge(LimitCode::MemorySize, text.len() as u64)?;
        self.charged = self.charged.saturating_add(text.len() as u64);
        slot.clear();
        slot.push_str(text);
        font.text_changed = true;
        Ok(())
    }

    /// Writes the dictionaries and CMaps that differ from the document's.
    pub fn write(&mut self, transaction: &mut Transaction) -> Result<()> {
        for font in self.fonts.iter_mut() {
            self.budget.poll()?;
            font.write(transaction, self.budget)?;
        }
        Ok(())
    }
}

impl Font {
    /// Returns a font with no glyphs, whose dictionary and CMap
    /// [`Font::write`] writes.
    fn new(transaction: &mut Transaction, em: u16) -> Result<Self> {
        let number = transaction.allocate()?.number();
        let to_unicode = transaction.allocate()?;
        let scale = 1.0 / f64::from(em.max(1));
        let mut dict = Dict::new();
        dict.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        dict.insert(Name::from("Subtype"), Value::Name(Name::from("Type3")));
        dict.insert(
            Name::from("FontMatrix"),
            array([scale, 0.0, 0.0, scale, 0.0, 0.0].map(Value::from)),
        );
        dict.insert(Name::from("FirstChar"), Value::Integer(0));
        Ok(Self {
            number,
            dict,
            to_unicode,
            names: Vec::new(),
            widths: Vec::new(),
            procedures: Dict::new(),
            resources: Dict::new(),
            ink: None,
            texts: Vec::new(),
            changed: true,
            text_changed: true,
        })
    }

    /// Returns the font of object `number`, `None` when it no longer has
    /// the shape that [`Font::write`] gives it.
    fn read(version: &Version, number: u32, budget: &Budget) -> Result<Option<Self>> {
        let Some(object) = version.get(number, budget)? else {
            return Ok(None);
        };
        let Value::Dict(dict) = &**object.value() else {
            return Ok(None);
        };
        if !is_name(dict.get("Subtype"), b"Type3") {
            return Ok(None);
        }
        let Some(Value::Dict(encoding)) = dict.get("Encoding") else {
            return Ok(None);
        };
        let Some(Value::Array(differences)) = encoding.get("Differences") else {
            return Ok(None);
        };
        if differences.get(0) != Some(&Value::Integer(0)) || differences.len() > GLYPHS_PER_FONT + 1
        {
            return Ok(None);
        }
        let mut names = Vec::with_capacity(differences.len() - 1);
        for value in differences.iter().skip(1) {
            let Value::Name(name) = value else {
                return Ok(None);
            };
            names.push(name.clone());
        }
        let Some(Value::Array(widths)) = dict.get("Widths") else {
            return Ok(None);
        };
        if widths.len() != names.len() {
            return Ok(None);
        }
        let mut advances = Vec::with_capacity(names.len());
        for width in widths.iter() {
            advances.push(match width {
                Value::Integer(width) => *width as f64,
                Value::Real(width) => width.value(),
                _ => return Ok(None),
            });
        }
        let Some(Value::Dict(procedures)) = dict.get("CharProcs") else {
            return Ok(None);
        };
        if !names
            .iter()
            .all(|name| matches!(procedures.get(name.as_bytes()), Some(Value::Reference(_))))
        {
            return Ok(None);
        }
        let Some(Value::Dict(resources)) = dict.get("Resources") else {
            return Ok(None);
        };
        let Some(Value::Reference(to_unicode)) = dict.get("ToUnicode") else {
            return Ok(None);
        };
        let Some(cmap) = version
            .get(to_unicode.number(), budget)?
            .filter(|object| object.generation() == to_unicode.generation())
        else {
            return Ok(None);
        };
        let Value::Stream(cmap) = &**cmap.value() else {
            return Ok(None);
        };
        let Some(texts) = read_cmap(&cmap.decoded(budget)?, names.len()) else {
            return Ok(None);
        };
        Ok(Some(Self {
            number,
            dict: dict.clone(),
            to_unicode: *to_unicode,
            names,
            widths: advances,
            procedures: procedures.clone(),
            resources: resources.clone(),
            ink: read_box(dict.get("FontBBox")),
            texts,
            changed: false,
            text_changed: false,
        }))
    }

    fn write(&mut self, transaction: &mut Transaction, budget: &Budget) -> Result<()> {
        if self.text_changed {
            let stream = stream_of(&write_cmap(&self.texts, budget)?, budget)?;
            transaction.write(self.to_unicode.number(), &Value::from(stream), budget)?;
            self.text_changed = false;
        }
        if !self.changed {
            return Ok(());
        }
        let mut differences = Array::default();
        differences.push(Value::Integer(0));
        for name in &self.names {
            differences.push(Value::Name(name.clone()));
        }
        let mut encoding = Dict::new();
        encoding.insert(Name::from("Type"), Value::Name(Name::from("Encoding")));
        encoding.insert(Name::from("Differences"), Value::Array(differences));
        let dict = &mut self.dict;
        let last = i64::try_from(self.names.len()).unwrap_or(i64::MAX) - 1;
        dict.insert(Name::from("Encoding"), Value::Dict(encoding));
        dict.insert(Name::from("LastChar"), Value::Integer(last));
        dict.insert(
            Name::from("Widths"),
            array(self.widths.iter().copied().map(number)),
        );
        dict.insert(
            Name::from("FontBBox"),
            array(self.ink.unwrap_or_default().map(number)),
        );
        dict.insert(
            Name::from("CharProcs"),
            Value::Dict(self.procedures.clone()),
        );
        dict.insert(Name::from("Resources"), Value::Dict(self.resources.clone()));
        dict.insert(Name::from("ToUnicode"), Value::Reference(self.to_unicode));
        transaction.write(self.number, &Value::Dict(dict.clone()), budget)?;
        self.changed = false;
        Ok(())
    }
}

fn is_name(value: Option<&Value>, name: &[u8]) -> bool {
    matches!(value, Some(Value::Name(value)) if value.as_bytes() == name)
}

/// Returns `values` as an array.
fn array(values: impl IntoIterator<Item = Value>) -> Value {
    let mut array = Array::default();
    for value in values {
        array.push(value);
    }
    Value::Array(array)
}

/// Returns `value` as an integer when it is one, as advances mostly are.
fn number(value: f64) -> Value {
    if value.fract() == 0.0 && value.abs() < 1e15 {
        Value::Integer(value as i64)
    } else {
        Value::from(value)
    }
}

/// Returns the stream of `data`, compressed when that is shorter.
fn stream_of(data: &[u8], budget: &Budget) -> Result<Stream> {
    let mut dict = Dict::new();
    let encoded = deflate(data, 6, data.len().saturating_sub(1), budget)?;
    let data = match encoded {
        Some(encoded) => {
            dict.insert(Name::from("Filter"), Value::Name(Name::from("FlateDecode")));
            encoded
        }
        None => {
            let mut copy = BudgetVec::new(budget);
            copy.try_extend_from_slice(data)?;
            copy.into_vec()
        }
    };
    Ok(Stream::new(dict, Bytes::from(data)))
}

/// Adds `value` as `name` of `category` of `resources`.
fn insert_resource(resources: &mut Dict, category: Name, name: Name, value: Value) {
    if !matches!(resources.get(category.as_bytes()), Some(Value::Dict(_))) {
        resources.insert(category.clone(), Value::Dict(Dict::new()));
    }
    if let Some(Value::Dict(named)) = resources.get_mut(category.as_bytes()) {
        named.insert(name, value);
    }
}

/// Returns the box of the `FontBBox` array `value`, `None` for no ink.
fn read_box(value: Option<&Value>) -> Option<[f64; 4]> {
    let Some(Value::Array(array)) = value else {
        return None;
    };
    let mut bounds = [0.0; 4];
    for (at, slot) in bounds.iter_mut().enumerate() {
        *slot = match array.get(at)? {
            Value::Integer(value) => *value as f64,
            Value::Real(value) => value.value(),
            _ => return None,
        };
    }
    (bounds != [0.0; 4]).then_some(bounds)
}

fn union(a: Option<[f64; 4]>, b: Option<[f64; 4]>) -> Option<[f64; 4]> {
    match (a, b) {
        (Some(a), Some(b)) => Some([
            a[0].min(b[0]),
            a[1].min(b[1]),
            a[2].max(b[2]),
            a[3].max(b[3]),
        ]),
        (a, b) => a.or(b),
    }
}

const CMAP_HEAD: &[u8] = b"/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
";
const CMAP_TAIL: &[u8] = b"endcmap
CMapName currentdict /CMap defineresource pop
end
end
";
/// The entries of one `bfchar` block, which PDF limits to 100.
const BFCHAR_BLOCK: usize = 100;

/// Returns a ToUnicode CMap of `texts`, by code.
fn write_cmap<'b>(texts: &[String], budget: &'b Budget) -> Result<BudgetVec<'b, u8>> {
    let mut out = BudgetVec::new(budget);
    out.try_extend_from_slice(CMAP_HEAD)?;
    let mapped: Vec<(usize, &String)> = texts
        .iter()
        .enumerate()
        .filter(|(_, text)| !text.is_empty())
        .collect();
    for block in mapped.chunks(BFCHAR_BLOCK) {
        out.try_extend_from_slice(format!("{} beginbfchar\n", block.len()).as_bytes())?;
        for (code, text) in block {
            out.try_extend_from_slice(format!("<{code:02X}> <").as_bytes())?;
            for unit in text.encode_utf16() {
                out.try_extend_from_slice(format!("{unit:04X}").as_bytes())?;
            }
            out.try_extend_from_slice(b">\n")?;
        }
        out.try_extend_from_slice(b"endbfchar\n")?;
    }
    out.try_extend_from_slice(CMAP_TAIL)?;
    Ok(out)
}

/// Returns the text of each of `count` codes in a CMap that [`write_cmap`]
/// wrote, `None` when it is not one.
fn read_cmap(data: &[u8], count: usize) -> Option<Vec<String>> {
    let mut texts = vec![String::new(); count];
    let text = std::str::from_utf8(data).ok()?;
    for line in text.lines() {
        let Some(entry) = line.strip_prefix('<') else {
            continue;
        };
        // The codespace range has no text of its own.
        let (code, mapped) = entry.split_once("> <")?;
        let mapped = mapped.strip_suffix('>')?;
        if code == "00" && mapped == "FF" {
            continue;
        }
        let code = usize::from_str_radix(code, 16).ok()?;
        let units = (0..mapped.len() / 4)
            .map(|at| u16::from_str_radix(mapped.get(4 * at..4 * at + 4)?, 16).ok())
            .collect::<Option<Vec<u16>>>()?;
        *texts.get_mut(code)? = String::from_utf16(&units).ok()?;
    }
    Some(texts)
}
