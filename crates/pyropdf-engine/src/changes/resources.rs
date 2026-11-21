//! Resolve fonts and colour where the content actually uses them, including
//! invoked Forms and the graphics state inherited at each invocation.
use super::profiles::{PathReads, entry, font_points_to, named};
use super::programs::Selector;
use super::*;
use crate::content::{Operations, PageContent};
use pyropdf_store::budget::StreamKind;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::{Page, PageIndex};
use pyropdf_store::value::Name;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Device {
    Rgb,
    Cmyk,
    Gray,
}

impl Device {
    pub(super) fn covered(self, profile: Option<[u8; 4]>) -> bool {
        match self {
            Self::Rgb => profile == Some(*b"RGB "),
            Self::Cmyk => profile == Some(*b"CMYK"),
            Self::Gray => profile.is_some(),
        }
    }

    fn default_name(self) -> &'static str {
        match self {
            Self::Rgb => "DefaultRGB",
            Self::Cmyk => "DefaultCMYK",
            Self::Gray => "DefaultGray",
        }
    }
}

// Indirect fonts keep their PDF identity. For a direct font without an
// indirect descriptor use its enclosing resource and the name that selected it.
#[derive(PartialOrd, Ord, PartialEq, Eq)]
pub(super) enum AppearanceFont {
    Indirect(Reference),
    Descriptor(Reference),
    Direct(u32, Name),
}

pub(super) struct FontUse {
    pub(super) font: AppearanceFont,
    pub(super) missing: bool,
    pub(super) shown: bool,
}

enum Inherited<'a> {
    Page(&'a Page),
    Resources(Option<&'a Dict>),
}

#[derive(Clone, Copy)]
struct GraphicsState {
    glyphs: [bool; 256],
    // Fill and stroke begin in DeviceGray even without a colour operator.
    initial_gray: [bool; 2],
    invisible_text: bool,
    font_path: (usize, usize),
    selected_font: Option<usize>,
    program_only: bool,
}

#[derive(Clone, Copy)]
pub(super) enum Use<'a> {
    Colour(Device),
    Descriptor(u32),
    DamagedProgram(&'a programs::Check<'a>),
    MissingProgram(Option<&'a [(AppearanceFont, bool)]>),
    Procedure { owner: u32, name: &'a [u8] },
}

impl Use<'_> {
    fn glyphs(self, path: &mut PathReads<'_>, font: Option<&Value>) -> Result<[bool; 256]> {
        let mut matches = [false; 256];
        let Some(font) = font else { return Ok(matches) };
        let (version, budget) = (path.version, path.budget);
        match self {
            Self::MissingProgram(_) => matches.fill(missing_program(path, font)?),
            Self::Descriptor(number) => {
                matches.fill(font_points_to(version, font, number, budget)?)
            }
            Self::Procedure { owner, name } => {
                let resolved_font = resolved(version, font, budget)?;
                let Some(dict) = resolved_font.as_deref().and_then(value_dict) else {
                    return Ok(matches);
                };
                if !named(dict, "Subtype", b"Type3")
                    || reference_number(dict.get("CharProcs"))
                        .or_else(|| reference_number(Some(font)))
                        != Some(owner)
                {
                    return Ok(matches);
                }
                let encoding = entry(version, dict, "Encoding", budget)?;
                let Some(encoding) = encoding.as_deref().and_then(value_dict) else {
                    return Ok(matches);
                };
                let differences = entry(version, encoding, "Differences", budget)?;
                if let Some(Value::Array(items)) = differences.as_deref() {
                    let mut code = None;
                    for item in items.iter() {
                        budget.poll()?;
                        match item {
                            Value::Integer(n) => code = u8::try_from(*n).ok(),
                            Value::Name(glyph) => {
                                if let Some(index) = code {
                                    matches[usize::from(index)] = glyph.as_bytes() == name;
                                    code = index.checked_add(1);
                                }
                            }
                            _ => code = None,
                        }
                    }
                }
            }
            Self::Colour(_) | Self::DamagedProgram(_) => {}
        }
        Ok(matches)
    }

    // A selection can be replaced, saved by q, or inherited by a Form before
    // any text is shown. Keep its indirect owners out of the reported path
    // until a text-show operation actually uses that graphics state.
    fn select_font(
        self,
        path: &mut PathReads<'_>,
        state: &mut GraphicsState,
        font: Option<&Value>,
        before: usize,
        selector: Selector<'_>,
        form: Option<&Reference>,
    ) -> Result<()> {
        let name = selector.name();
        if let Self::DamagedProgram(check) = self {
            state.selected_font = None;
            if let Some(font) = font
                && let Some(pair) = check.select(path, font, selector)?
            {
                state.selected_font = Some(path.embedded_fonts.len());
                path.embedded_fonts.try_push(pair)?;
            }
            path.owners.truncate(before);
            return Ok(());
        }
        let program = path.owners.len();
        state.glyphs = self.glyphs(path, font)?;
        if let Self::MissingProgram(existing) = self {
            state.selected_font = None;
            state.program_only = false;
            let start = path.font_owners.len();
            if let Some(font) = font.filter(|_| {
                existing.is_none()
                    || (state.glyphs[0] && existing.is_some_and(|fonts| !fonts.is_empty()))
            }) {
                let key = match font {
                    Value::Reference(id) => AppearanceFont::Indirect(*id),
                    Value::Dict(d) if let Some(Value::Reference(id)) = d.get("FontDescriptor") => {
                        AppearanceFont::Descriptor(*id)
                    }
                    _ => {
                        let owner = path.owners[before..program]
                            .last()
                            .copied()
                            .or_else(|| form.map(|r| r.number()))
                            .unwrap_or(0);
                        path.budget
                            .charge(LimitCode::MemorySize, name.as_bytes().len() as u64)?;
                        AppearanceFont::Direct(owner, name.clone())
                    }
                };
                if let Some(existing) = existing {
                    if let Ok(index) = existing.binary_search_by(|(font, _)| font.cmp(&key)) {
                        state.glyphs.fill(!existing[index].1);
                        // This font was already painted in the opened appearance.
                        // Only a change to its embedding can introduce a defect.
                        state.program_only = !matches!(key, AppearanceFont::Direct(..));
                    }
                } else {
                    state.selected_font = Some(path.appearance_fonts.len());
                    path.appearance_fonts.try_push(FontUse {
                        font: key,
                        missing: state.glyphs[0],
                        shown: false,
                    })?;
                }
            }
            if state.glyphs[0] {
                let first = if state.program_only { program } else { before };
                path.font_owners
                    .try_extend_from_slice(&path.owners[first..])?;
            }
            state.font_path = (start, path.font_owners.len());
            path.owners.truncate(before);
        }
        Ok(())
    }

    fn stream(
        self,
        path: &mut PathReads<'_>,
        ops: &Operations,
        resources: Option<&Dict>,
        mut state: GraphicsState,
        stack: &mut BudgetVec<'_, Reference>,
    ) -> Result<bool> {
        let mut saved = BudgetVec::new(path.budget);
        let mut found = false;
        for op in ops.operations() {
            path.budget.charge(LimitCode::InterpretationFuel, 1)?;
            let operands = ops.operands(op);
            let operator = ops.operator(op);
            if let Self::Colour(device) = self {
                let selected = match operator {
                    b"rg" | b"RG" => Some(Device::Rgb),
                    b"k" | b"K" => Some(Device::Cmyk),
                    b"g" | b"G" => Some(Device::Gray),
                    b"cs" | b"CS" => match operands.first() {
                        Some(Value::Name(name)) => match name.as_bytes() {
                            b"DeviceRGB" => Some(Device::Rgb),
                            b"DeviceCMYK" => Some(Device::Cmyk),
                            b"DeviceGray" => Some(Device::Gray),
                            _ => None,
                        },
                        _ => None,
                    },
                    _ => None,
                };
                if selected.is_some() || matches!(operator, b"cs" | b"CS") {
                    state.initial_gray[usize::from(operator[0].is_ascii_uppercase())] = false;
                }
                let gray_paint = device == Device::Gray
                    && match operator {
                        b"f" | b"F" | b"f*" | b"Tj" | b"TJ" | b"'" | b"\"" => state.initial_gray[0],
                        b"S" | b"s" => state.initial_gray[1],
                        b"B" | b"B*" | b"b" | b"b*" => state.initial_gray.contains(&true),
                        _ => false,
                    };
                if selected == Some(device) || gray_paint {
                    let spaces = path.resolve(resources.and_then(|d| d.get("ColorSpace")))?;
                    if spaces
                        .as_deref()
                        .and_then(value_dict)
                        .and_then(|d| d.get(device.default_name()))
                        .is_none()
                    {
                        return Ok(true);
                    }
                }
            }
            match operator {
                b"Do" => {
                    let Some(Value::Name(name)) = operands.first() else {
                        continue;
                    };
                    let before = path.owners.len();
                    let xobjects = path.resolve(resources.and_then(|d| d.get("XObject")))?;
                    let form = xobjects
                        .as_deref()
                        .and_then(value_dict)
                        .and_then(|d| d.get(name.as_bytes()));
                    if self.form(path, form, Inherited::Resources(resources), state, stack)? {
                        if !matches!(self, Self::MissingProgram(_) | Self::DamagedProgram(_)) {
                            return Ok(true);
                        }
                        found = true;
                    } else if matches!(self, Self::MissingProgram(_) | Self::DamagedProgram(_)) {
                        path.owners.truncate(before);
                    }
                }
                b"Tr" if matches!(self, Self::MissingProgram(_) | Self::DamagedProgram(_)) => {
                    if let Some(Value::Integer(mode @ 0..=7)) = operands.first() {
                        state.invisible_text = *mode == 3;
                    }
                }
                b"gs" if matches!(self, Self::MissingProgram(_) | Self::DamagedProgram(_)) => {
                    if let Some(Value::Name(name)) = operands.first() {
                        let before = path.owners.len();
                        let states = path.resolve(resources.and_then(|d| d.get("ExtGState")))?;
                        let graphics = path.resolve(
                            states
                                .as_deref()
                                .and_then(value_dict)
                                .and_then(|d| d.get(name.as_bytes())),
                        )?;
                        let spec = path.resolve(
                            graphics
                                .as_deref()
                                .and_then(value_dict)
                                .and_then(|d| d.get("Font")),
                        )?;
                        if let Some(Value::Array(font)) = spec.as_deref() {
                            self.select_font(
                                path,
                                &mut state,
                                font.get(0),
                                before,
                                Selector::State(name),
                                stack.last(),
                            )?;
                        } else {
                            path.owners.truncate(before);
                        }
                    }
                }
                b"q" => saved.try_push(state)?,
                b"Q" => {
                    if let Some(previous) = saved.pop() {
                        state = previous
                    }
                }
                b"Tf" if !matches!(self, Self::Colour(_)) => {
                    let Some(Value::Name(name)) = operands.first() else {
                        continue;
                    };
                    let before = path.owners.len();
                    let fonts = path.resolve(resources.and_then(|d| d.get("Font")))?;
                    self.select_font(
                        path,
                        &mut state,
                        fonts
                            .as_deref()
                            .and_then(value_dict)
                            .and_then(|d| d.get(name.as_bytes())),
                        before,
                        Selector::Font(name),
                        stack.last(),
                    )?;
                }
                b"Tj" | b"TJ" | b"'" | b"\"" if !matches!(self, Self::Colour(_)) => {
                    if let Self::DamagedProgram(check) = self {
                        // A text-show with an empty string still uses the selected
                        // font for the embedding rule (veraPDF 6.2.11.4.1-1).
                        if !state.invisible_text
                            && let Some(i) = state.selected_font
                        {
                            check.shown(path, i)?;
                        }
                        continue;
                    }
                    if matches!(self, Self::MissingProgram(_)) {
                        if state.invisible_text {
                            continue;
                        }
                        if let Some(index) = state.selected_font {
                            path.appearance_fonts[index].shown = true;
                        }
                        if state.glyphs[0] {
                            let (start, end) = state.font_path;
                            let owners = &path.font_owners[start..end];
                            let target = if state.program_only {
                                &mut path.changed_programs
                            } else {
                                found = true;
                                &mut path.owners
                            };
                            if !target.ends_with(owners) {
                                target.try_extend_from_slice(owners)?;
                            }
                        }
                        continue;
                    }
                    if matches!(self, Self::Descriptor(_)) && state.glyphs[0] {
                        return Ok(true);
                    }
                    let shown = |v: &Value| matches!(v,Value::String(s) if s.as_bytes().iter().any(|b| state.glyphs[usize::from(*b)]));
                    if match operands.last() {
                        Some(Value::Array(items)) => items.iter().any(shown),
                        Some(v) => shown(v),
                        None => false,
                    } {
                        return Ok(true);
                    }
                }
                _ => {}
            }
        }
        Ok(found)
    }

    fn form(
        self,
        path: &mut PathReads<'_>,
        raw: Option<&Value>,
        inherited: Inherited<'_>,
        state: GraphicsState,
        stack: &mut BudgetVec<'_, Reference>,
    ) -> Result<bool> {
        let Some(Value::Reference(id)) = raw else {
            return Ok(false);
        };
        if stack.contains(id) {
            return Ok(false);
        }
        let form = path.resolve(raw)?;
        let Some(Value::Stream(form)) = form.as_deref() else {
            return Ok(false);
        };
        if !named(form.dict(), "Subtype", b"Form") {
            return Ok(false);
        }
        if stack.len() >= MAX_DEPTH {
            return Err(LimitExceeded::over(
                LimitCode::ObjectDepth,
                MAX_DEPTH as u64,
                stack.len() as u64 + 1,
            )
            .into());
        }
        let data = match form.decoded_as(StreamKind::PageContent, path.budget) {
            Ok(data) => data,
            Err(e) if e.limit().is_none() => return Ok(false),
            Err(e) => return Err(e.into()),
        };
        let ops = Operations::parse(data.into(), path.budget)?;
        let local = path.resolve(form.dict().get("Resources"))?;
        let fallback;
        let resources = match local.as_deref().and_then(value_dict) {
            Some(resources) => Some(resources),
            None => match inherited {
                Inherited::Resources(resources) => resources,
                Inherited::Page(page) => {
                    fallback = path.resources(page)?;
                    fallback.as_deref().and_then(value_dict)
                }
            },
        };
        stack.try_push(*id)?;
        let found = self.stream(path, &ops, resources, state, stack)?;
        stack.pop();
        Ok(found)
    }

    pub(super) fn page(self, path: &mut PathReads<'_>, page: &Page) -> Result<bool> {
        let mut reads = Reads::new(path.version, path.budget);
        let content = PageContent::read(&mut reads, page)?;
        for part in content.parts() {
            path.owners.try_push(part.stream.number())?
        }
        let resources = path.resources(page)?;
        self.stream(
            path,
            content.operations(),
            resources.as_deref().and_then(value_dict),
            GraphicsState {
                glyphs: [false; 256],
                initial_gray: [true; 2],
                invisible_text: false,
                font_path: (0, 0),
                selected_font: None,
                program_only: false,
            },
            &mut BudgetVec::new(path.budget),
        )
    }

    pub(super) fn in_document(self, version: &Version, budget: &Budget) -> Result<bool> {
        let pages = PageIndex::build(&mut Reads::new(version, budget))?;
        for page in pages.pages() {
            if self.page(&mut PathReads::new(version, budget), page)? {
                return Ok(true);
            }
        }
        Ok(false)
    }
}

/// Annotations may use one normal Form or a dictionary of named normal Forms.
/// The archival and accessibility font rules apply to each stored normal state.
pub(super) fn missing_appearance_font(
    path: &mut PathReads<'_>,
    annotation: &Dict,
    page: &Page,
    existing: Option<&[(AppearanceFont, bool)]>,
) -> Result<bool> {
    Use::MissingProgram(existing).appearance(path, annotation, page)
}

impl Use<'_> {
    pub(super) fn appearance(
        self,
        path: &mut PathReads<'_>,
        annotation: &Dict,
        page: &Page,
    ) -> Result<bool> {
        let ap = path.resolve(annotation.get("AP"))?;
        let normal = ap.as_deref().and_then(value_dict).and_then(|d| d.get("N"));
        let resolved = path.resolve(normal)?;
        let state = GraphicsState {
            glyphs: [false; 256],
            initial_gray: [true; 2],
            invisible_text: false,
            font_path: (0, 0),
            selected_font: None,
            program_only: false,
        };
        let mut stack = BudgetVec::new(path.budget);
        match resolved.as_deref() {
            Some(Value::Stream(_)) => {
                self.form(path, normal, Inherited::Page(page), state, &mut stack)
            }
            Some(Value::Dict(states)) => {
                let mut found = false;
                for (_, item) in states.iter() {
                    path.budget.poll()?;
                    let previous = path.owners.len();
                    if self.form(path, Some(item), Inherited::Page(page), state, &mut stack)? {
                        found = true;
                    } else {
                        path.owners.truncate(previous);
                    }
                }
                Ok(found)
            }
            _ => Ok(false),
        }
    }
}

fn missing_program(path: &mut PathReads<'_>, font: &Value) -> Result<bool> {
    let value = path.resolve(Some(font))?;
    let Some(font) = value.as_deref().and_then(value_dict) else {
        return Ok(false);
    };
    if named(font, "Subtype", b"Type0") {
        let children = path.resolve(font.get("DescendantFonts"))?;
        let Some(Value::Array(children)) = children.as_deref() else {
            return Ok(false);
        };
        let child = path.resolve(children.get(0))?;
        let Some(child) = child.as_deref().and_then(value_dict) else {
            return Ok(false);
        };
        return missing_descriptor_program(path, child);
    }
    if named(font, "Subtype", b"Type3") {
        return Ok(false);
    }
    missing_descriptor_program(path, font)
}

fn missing_descriptor_program(path: &mut PathReads<'_>, font: &Dict) -> Result<bool> {
    let descriptor = path.resolve(font.get("FontDescriptor"))?;
    let desc = descriptor.as_deref().and_then(value_dict);
    for key in ["FontFile", "FontFile2", "FontFile3"] {
        let file = path.resolve(desc.and_then(|d| d.get(key)))?;
        if matches!(file.as_deref(), Some(Value::Stream(_))) {
            return Ok(false);
        }
    }
    Ok(true)
}
