//! Resolve fonts and colour where the content actually uses them, including
//! invoked Forms and the graphics state inherited at each invocation.
use super::profiles::{PathReads, entry, font_points_to, named};
use super::*;
use crate::content::{Operations, PageContent};
use pyropdf_store::budget::StreamKind;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::{Page, PageIndex};

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

#[derive(Clone, Copy)]
struct GraphicsState {
    glyphs: [bool; 256],
    // Fill and stroke begin in DeviceGray even without a colour operator.
    initial_gray: [bool; 2],
}

#[derive(Clone, Copy)]
pub(super) enum Use<'a> {
    Colour(Device),
    Descriptor(u32),
    Procedure { owner: u32, name: &'a [u8] },
}

impl Use<'_> {
    fn glyphs(
        self,
        version: &Version,
        font: Option<&Value>,
        budget: &Budget,
    ) -> Result<[bool; 256]> {
        let mut matches = [false; 256];
        let Some(font) = font else { return Ok(matches) };
        match self {
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
            Self::Colour(_) => {}
        }
        Ok(matches)
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
                    let xobjects = path.resolve(resources.and_then(|d| d.get("XObject")))?;
                    let form = xobjects
                        .as_deref()
                        .and_then(value_dict)
                        .and_then(|d| d.get(name.as_bytes()));
                    let Some(Value::Reference(id)) = form else {
                        continue;
                    };
                    if stack.contains(id) {
                        continue;
                    }
                    let form = path.resolve(form)?;
                    let Some(Value::Stream(form)) = form.as_deref() else {
                        continue;
                    };
                    if !named(form.dict(), "Subtype", b"Form") {
                        continue;
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
                        Err(e) if e.limit().is_none() => continue,
                        Err(e) => return Err(e.into()),
                    };
                    let ops = Operations::parse(data.into(), path.budget)?;
                    let local = path.resolve(form.dict().get("Resources"))?;
                    let resources = local.as_deref().and_then(value_dict).or(resources);
                    stack.try_push(*id)?;
                    let found = self.stream(path, &ops, resources, state, stack)?;
                    stack.pop();
                    if found {
                        return Ok(true);
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
                    let fonts = path.resolve(resources.and_then(|d| d.get("Font")))?;
                    state.glyphs = self.glyphs(
                        path.version,
                        fonts
                            .as_deref()
                            .and_then(value_dict)
                            .and_then(|d| d.get(name.as_bytes())),
                        path.budget,
                    )?;
                }
                b"Tj" | b"TJ" | b"'" | b"\"" if !matches!(self, Self::Colour(_)) => {
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
        Ok(false)
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
