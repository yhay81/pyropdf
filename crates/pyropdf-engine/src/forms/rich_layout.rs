//! Paint scoped XHTML text in the coordinate system of the widget appearance.

use super::WidgetError;
use super::appearance::TextLayout;
use super::rich_codes::Code;
use super::rich_fonts::{PaintFont, RichFonts};
use super::rich_style::{self, Style};
use super::rich_text::Run;
use super::settle::{Drawing, WidgetAppearance};
use super::style::{TextStyle, WidgetStyle, fmt};
use crate::standard::{self, Align, Base14Font};
use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::value::Name;
use std::ops::Range;
use unicode_segmentation::UnicodeSegmentation;

#[derive(Clone, Copy)]
struct Glyph<'s> {
    cell: usize,
    byte: u8,
    embedded: Option<Embedded>,
    width: f64,
    style: Style<'s>,
}
#[derive(Clone, Copy)]
struct Embedded {
    id: usize,
    code: Code,
    actual: Option<(usize, usize)>,
    offset: (f64, f64),
    metrics: (f64, f64),
}
impl Glyph<'_> {
    fn same_style(&self, other: &Self) -> bool {
        self.style == other.style && self.embedded.map(|e| e.id) == other.embedded.map(|e| e.id)
    }

    fn metrics(&self) -> (f64, f64) {
        self.embedded.map_or_else(
            || standard::vertical_metrics(self.style.font()),
            |e| e.metrics,
        )
    }
}
struct Line {
    start: usize,
    end: usize,
    width: f64,
    ascent: f64,
    descent: f64,
    align: Align,
}

fn line(glyphs: &[Glyph], start: usize, end: usize, base: Style) -> Line {
    let mut ascent = base.size * 0.8;
    let mut descent = base.size * 0.2;
    let mut width = 0.;
    for glyph in &glyphs[start..end] {
        let style = glyph.style;
        let (up, down) = glyph.metrics();
        ascent = ascent.max(up * style.size + style.rise);
        descent = descent.max(down * style.size - style.rise);
        width += glyph.width;
    }
    Line {
        start,
        end,
        width,
        ascent,
        descent,
        align: glyphs.get(start).map_or(base.align, |g| g.style.align),
    }
}

fn lines<'b>(
    glyphs: &[Glyph],
    width: f64,
    multiline: bool,
    base: Style,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, Line>, WidgetError> {
    let mut lines = BudgetVec::new(budget);
    let mut start = 0;
    while start < glyphs.len() {
        budget.poll()?;
        if lines.len() >= standard::MAX_LINES {
            return Err(WidgetError::Lines);
        }
        let mut end = start;
        let mut advance = 0.;
        let mut last_space = None;
        while let Some(glyph) = glyphs.get(end) {
            if end % 1024 == 0 {
                budget.poll()?;
            }
            if glyph.byte == b'\n' {
                break;
            }
            if multiline && end > start && advance + glyph.width > width {
                if let Some(space) = last_space {
                    end = space;
                }
                break;
            }
            advance += glyph.width;
            if glyph.byte == b' ' {
                last_space = Some(end);
            }
            end += 1;
        }
        lines.try_push(line(glyphs, start, end, base))?;
        start = end;
        if glyphs
            .get(start)
            .is_some_and(|g| matches!(g.byte, b' ' | b'\n'))
        {
            start += 1;
        }
    }
    Ok(lines)
}

/// Maximum number of shrink iterations during rich-text auto-fit (DESIGN.md F6).
const MAX_FIT_ITERATIONS: usize = 256;

/// Returns a finite positive initial font size for auto-fit rich text in user space.
fn auto_size(height: f64, unit: f64) -> f64 {
    let height = if height.is_finite() && height > 0.0 {
        height.min(rich_style::MAX_USER_SIZE)
    } else {
        0.0
    };
    let max_fit = (standard::MAX_FIT_SIZE / unit).min(rich_style::MAX_USER_SIZE);
    let min_fit = standard::MIN_FIT_SIZE / unit;
    let floor = if min_fit.is_finite() && min_fit <= max_fit {
        min_fit
    } else {
        0.0
    };
    let size = height.max(floor).min(max_fit);
    if size.is_finite() && size > 0.0 {
        size
    } else if height > 0.0 {
        height
    } else {
        standard::MIN_FIT_SIZE
    }
}

/// Fit only text that inherits an automatic default size. Explicit CSS sizes
/// retain their point size, even when the widget clips their content.
fn fit<'b>(
    glyphs: &mut [Glyph],
    mut base: Style,
    size: (f64, f64),
    multiline: bool,
    unit: f64,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, Line>, WidgetError> {
    if !(size.0.is_finite()
        && size.1.is_finite()
        && unit.is_finite()
        && unit > 0.0
        && base.size.is_finite()
        && base.size > 0.0)
    {
        return Err(WidgetError::Malformed(
            "rich text layout requires finite positive geometry".into(),
        ));
    }
    let mut adjustable = glyphs.iter().any(|g| g.style.auto_size);
    for iteration in 0..=MAX_FIT_ITERATIONS {
        let lines = lines(glyphs, size.0, multiline, base, budget)?;
        let fits_width = lines
            .iter()
            .all(|l| l.width.is_finite() && l.width <= size.0);
        let total_height: f64 = lines.iter().map(|l| l.ascent + l.descent).sum();
        if !adjustable
            || iteration == MAX_FIT_ITERATIONS
            || (fits_width && total_height.is_finite() && total_height <= size.1)
        {
            return Ok(lines);
        }
        if base.auto_size {
            let next = base.size * standard::FIT_STEP;
            if next.is_finite() && next > 0.0 && next < base.size {
                base.size = next;
            }
        }
        adjustable = false;
        for (i, glyph) in glyphs.iter_mut().enumerate() {
            if i % 1024 == 0 {
                budget.poll()?;
            }
            if !glyph.style.auto_size {
                continue;
            }
            let current = glyph.style.size;
            if !(current.is_finite() && current > 0.0 && current * unit >= standard::MIN_FIT_SIZE) {
                continue;
            }
            let next_size = current * standard::FIT_STEP;
            let next_rise = glyph.style.rise * standard::FIT_STEP;
            let next_width = glyph.width * standard::FIT_STEP;
            if next_size.is_finite()
                && next_size > 0.0
                && next_size < current
                && next_rise.is_finite()
                && next_width.is_finite()
            {
                glyph.style.size = next_size;
                glyph.style.rise = next_rise;
                glyph.width = next_width;
                adjustable = true;
            }
        }
    }
    unreachable!("bounded fit loop returns at iteration == MAX_FIT_ITERATIONS")
}

/// Paint consecutive glyphs with identical formatting as one PDF text operator.
fn show(
    out: &mut BudgetVec<'_, u8>,
    group: &[Glyph],
    x: f64,
    baseline: f64,
    actual_text: &[u8],
) -> Result<(), WidgetError> {
    let style = group[0].style;
    let [r, g, b] = style.color;
    let y = baseline + style.rise;
    let width: f64 = group.iter().map(|g| g.width).sum();
    let stretch = style.stretch * 100.;
    if !(style.size.is_finite()
        && style.size > 0.0
        && r.is_finite()
        && g.is_finite()
        && b.is_finite()
        && stretch.is_finite()
        && x.is_finite()
        && y.is_finite()
        && width.is_finite())
    {
        return Err(WidgetError::Malformed(
            "non-finite rich text appearance coordinate".into(),
        ));
    }
    if let Some(embedded) = group[0].embedded {
        let settings = format!(
            "BT /PyroRich{} {} Tf {} {} {} rg {} Tz\n",
            embedded.id,
            fmt(style.size),
            fmt(r),
            fmt(g),
            fmt(b),
            fmt(stretch)
        );
        out.try_extend_from_slice(settings.as_bytes())?;
        let mut pen = x;
        for glyph in group {
            let e = glyph.embedded.expect("a rich text group shares its font");
            let (dx, dy) = e.offset;
            let gx = pen + dx * style.size * style.stretch;
            let gy = y + dy * style.size;
            if !(gx.is_finite() && gy.is_finite() && glyph.width.is_finite()) {
                return Err(WidgetError::Malformed(
                    "non-finite rich text glyph position".into(),
                ));
            }
            if let Some((start, end)) = e.actual {
                let original =
                    std::str::from_utf8(&actual_text[start..end]).expect("shaped Unicode");
                out.try_extend_from_slice(b"/Span <</ActualText <FEFF")?;
                for unit in original.encode_utf16() {
                    out.try_extend_from_slice(format!("{unit:04X}").as_bytes())?;
                }
                out.try_extend_from_slice(b">>> BDC\n")?;
            }
            let n = usize::from(e.code.length) * 2;
            let show = format!(
                "1 0 0 1 {} {} Tm <{:0n$X}> Tj\n",
                fmt(gx),
                fmt(gy),
                e.code.value
            );
            out.try_extend_from_slice(show.as_bytes())?;
            if e.actual.is_some() {
                out.try_extend_from_slice(b"EMC\n")?;
            }
            pen += glyph.width;
        }
        out.try_extend_from_slice(b"ET\n")?;
    } else {
        let font = style.font().pdf_base_name();
        let settings = format!(
            "BT /{font} {} Tf {} {} {} rg {} Tz 1 0 0 1 {} {} Tm <",
            fmt(style.size),
            fmt(r),
            fmt(g),
            fmt(b),
            fmt(stretch),
            fmt(x),
            fmt(y)
        );
        out.try_extend_from_slice(settings.as_bytes())?;
        const HEX: &[u8; 16] = b"0123456789ABCDEF";
        for (index, glyph) in group.iter().enumerate() {
            if index % 1024 == 0 {
                out.budget().poll()?;
            }
            out.try_extend_from_slice(&[
                HEX[usize::from(glyph.byte >> 4)],
                HEX[usize::from(glyph.byte & 15)],
            ])?;
        }
        out.try_extend_from_slice(b"> Tj ET\n")?;
    }
    if let Some(underline) = style.decoration {
        let at = y + style.size * if underline { -0.12 } else { 0.30 };
        let stroke_width = style.size / 18.;
        let end_x = x + width;
        if !(at.is_finite() && stroke_width.is_finite() && end_x.is_finite()) {
            return Err(WidgetError::Malformed(
                "non-finite rich text decoration coordinate".into(),
            ));
        }
        let stroke = format!(
            "{} {} {} RG {} w {} {} m {} {} l S\n",
            fmt(r),
            fmt(g),
            fmt(b),
            fmt(stroke_width),
            fmt(x),
            fmt(at),
            fmt(end_x),
            fmt(at)
        );
        out.try_extend_from_slice(stroke.as_bytes())?;
    }
    Ok(())
}

/// Place each visible grapheme in its comb cell, using its own rich text style.
#[expect(
    clippy::cast_precision_loss,
    reason = "cell positions use PDF user-space coordinates"
)]
fn comb(
    out: &mut BudgetVec<'_, u8>,
    glyphs: &mut [Glyph],
    count: usize,
    rect: [f64; 4],
    height: f64,
    align: Align,
    actual_text: &[u8],
) -> Result<(), WidgetError> {
    let [left, top, right, bottom] = rect;
    let cell_width = (right - left) / count as f64;
    let used = glyphs.last().map_or(0, |glyph| glyph.cell + 1);
    let unused = count.saturating_sub(used);
    let first = match align {
        Align::Left => 0,
        Align::Center => unused / 2,
        Align::Right => unused,
    };
    for cell in glyphs.chunk_by_mut(|a, b| a.cell == b.cell) {
        out.budget().poll()?;
        let width = cell.iter().map(|glyph| glyph.width).sum::<f64>();
        let (up, down) = cell.iter().fold((0_f64, 0_f64), |(up, down), glyph| {
            let (a, d) = glyph.metrics();
            (up.max(a * glyph.style.size), down.max(d * glyph.style.size))
        });
        let raw_scale = (cell_width / width)
            .min((bottom - top) / (up + down))
            .min(1.);
        let scale = if raw_scale.is_finite() && raw_scale > 0.0 {
            raw_scale
        } else {
            1.0
        };
        for glyph in cell.iter_mut().filter(|g| g.style.auto_size) {
            glyph.width *= scale;
            glyph.style.size *= scale;
            glyph.style.rise *= scale;
        }
        let width = cell.iter().map(|g| g.width).sum::<f64>();
        let ascent = cell
            .iter()
            .map(|g| g.metrics().0 * g.style.size)
            .fold(0., f64::max);
        let mut x =
            left + (first + cell[0].cell) as f64 * cell_width + (cell_width - width).max(0.) / 2.;
        for group in cell.chunk_by(Glyph::same_style) {
            show(out, group, x, height - top - ascent, actual_text)?;
            x += group.iter().map(|g| g.width).sum::<f64>();
        }
    }
    Ok(())
}

type Character<'s> = (char, Style<'s>, PaintFont, usize);

fn selected_spans<'c, 's, 'b>(
    chars: &'c [Character<'s>],
    comb: bool,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, &'c [Character<'s>]>, WidgetError> {
    let mut ordered = BudgetVec::new(budget);
    let same = |a: &Character, b: &Character| a.1 == b.1 && a.2 == b.2 && a.3 == b.3;
    for row in chars.split_inclusive(|c| c.0 == '\n') {
        let n = row.len() - usize::from(row.last().is_some_and(|c| c.0 == '\n'));
        let line = &row[..n];
        if comb || line.iter().all(|c| c.0.is_ascii()) {
            for group in line.chunk_by(same) {
                ordered.try_push(group)?;
            }
        } else {
            let mut bytes = BudgetVec::new(budget);
            let mut positions = BudgetVec::new(budget);
            let mut spans = BudgetVec::new(budget);
            for group in line.chunk_by(same) {
                let start = bytes.len();
                for &(ch, _, _, _) in group {
                    positions.try_push(bytes.len())?;
                    bytes.try_extend_from_slice(ch.encode_utf8(&mut [0; 4]).as_bytes())?;
                }
                spans.try_push(start..bytes.len())?;
            }
            positions.try_push(bytes.len())?;
            let text = std::str::from_utf8(&bytes).expect("rich text characters");
            for (_, part) in &*rich_style::visual_spans(text, 0, &spans, Range::clone, budget)? {
                let start = positions
                    .binary_search(&part.start)
                    .expect("bidi character boundary");
                let end = positions
                    .binary_search(&part.end)
                    .expect("bidi character boundary");
                ordered.try_push(&line[start..end])?;
            }
        }
        if n < row.len() {
            ordered.try_push(&row[n..])?;
        }
    }
    Ok(ordered)
}

pub(super) fn draw(
    runs: &[Run],
    css: &str,
    widget: &WidgetStyle,
    text: &TextStyle,
    align: Align,
    layout: TextLayout,
    mut available: RichFonts<'_, '_>,
) -> Result<Drawing, WidgetError> {
    let budget = available.reader.budget;
    let multiline = matches!(layout, TextLayout::Multiline);
    if let TextLayout::Comb(cells) = layout {
        let value = rich_style::plain(runs, budget)?;
        let count = standard::grapheme_count(&standard::single_line(&value, budget)?, budget)?;
        if count > cells {
            return Err(WidgetError::Malformed(
                format!("comb field value has {count} characters, exceeding MaxLen {cells}").into(),
            ));
        }
    }
    let mut content = BudgetVec::new(budget);
    widget.decoration(&mut content)?;
    let [x0, y0, x1, y1] = widget.content_rect();
    let (width, height) = (x1 - x0, y1 - y0);
    let unit = if widget.unit.is_finite() && widget.unit > 0.0 {
        widget.unit
    } else {
        1.0
    };
    let mut fonts = BudgetVec::<Base14Font>::new(budget);
    let mut embedded = BudgetVec::new(budget);
    let mut base = Style::new(
        text.font,
        if text.size > 0. {
            text.size.min(rich_style::MAX_USER_SIZE)
        } else {
            auto_size(height, unit)
        },
        [text.color.0, text.color.1, text.color.2],
        align,
    );
    base.auto_size = text.size <= 0.;
    base.user_unit = unit;
    base.css(css);
    let mut glyphs = BudgetVec::new(budget);
    let mut actual_text = BudgetVec::new(budget);
    let mut unsupported = false;
    let mut chars = BudgetVec::new(budget);
    let mut previous = None;
    rich_style::walk(runs, base, budget, |c, style| {
        let c = if !multiline && c == '\n' { ' ' } else { c };
        let names = if style.families.is_empty() {
            &text.resource
        } else {
            style.families
        };
        let paint = if c == '\n' {
            PaintFont::Standard(style.font())
        } else {
            let key = (
                names.as_ptr(),
                names.len(),
                style.font(),
                style.bold,
                style.italic,
            );
            if previous.as_ref().is_none_or(|(last, _)| *last != key) {
                previous = Some((key, available.candidates(names, style)?));
            }
            available.select(&previous.as_ref().expect("resolved style").1, c)?
        };
        chars.try_push((c, style, paint, 0))?;
        Ok(())
    })?;
    if matches!(layout, TextLayout::Comb(_)) {
        // Shape each cell on its own, so a ligature cannot span a separator,
        // and a base and its marks share a cell even across style boundaries.
        let mut bytes = BudgetVec::new(budget);
        for &(ch, _, _, _) in &*chars {
            bytes.try_extend_from_slice(ch.encode_utf8(&mut [0; 4]).as_bytes())?;
        }
        let text = std::str::from_utf8(&bytes).expect("Unicode form field");
        let mut entries = chars.iter_mut();
        for (cell, cluster) in text.graphemes(true).enumerate() {
            for _ in cluster.chars() {
                entries.next().expect("graphemes cover the field").3 = cell;
            }
        }
    }
    for &span in &*selected_spans(&chars, matches!(layout, TextLayout::Comb(_)), budget)? {
        let (_, style, paint, cell) = span[0];
        match paint {
            PaintFont::Unavailable => return Ok(Drawing::Removed),
            PaintFont::Standard(font) => {
                if !fonts.contains(&font) {
                    fonts.try_push(font)?;
                }
                for &(c, style, _, _) in span {
                    let code = if c == '\n' {
                        Some(b'\n')
                    } else {
                        standard::glyph_code(font, c)
                    };
                    let Some(byte) = code else {
                        unsupported = true;
                        break;
                    };
                    let width = if byte == b'\n' {
                        0.
                    } else {
                        standard::text_width(c.encode_utf8(&mut [0; 4]), font, style.size)
                            .unwrap_or_default()
                            * style.stretch
                    };
                    if !(style.size.is_finite()
                        && style.size > 0.0
                        && style.rise.is_finite()
                        && width.is_finite())
                    {
                        return Err(WidgetError::Malformed(
                            "non-finite rich text glyph metrics".into(),
                        ));
                    }
                    glyphs.try_push(Glyph {
                        cell,
                        byte,
                        embedded: None,
                        width,
                        style: style.with_family(font),
                    })?;
                }
            }
            PaintFont::Embedded(id) => {
                let name = Name::from(format!("PyroRich{id}").as_str());
                if !embedded.iter().any(|(n, _)| *n == name) {
                    embedded.try_push((name, available.reference(id)))?;
                }
                let mut bytes = BudgetVec::new(budget);
                for &(ch, _, _, _) in span {
                    bytes.try_extend_from_slice(ch.encode_utf8(&mut [0; 4]).as_bytes())?;
                }
                let content = std::str::from_utf8(&bytes).expect("encoded Unicode field text");
                let em = available.em(id);
                for g in &*available.shape(id, content, budget)? {
                    let original = &content[g.text.clone()];
                    let Some((code, reads_as)) = available.code(id, g.glyph, original)? else {
                        unsupported = true;
                        break;
                    };
                    let actual = if reads_as {
                        None
                    } else {
                        let start = actual_text.len();
                        actual_text.try_extend_from_slice(original.as_bytes())?;
                        Some((start, actual_text.len()))
                    };
                    let byte = if content.get(g.cluster.clone()) == Some(" ") {
                        b' '
                    } else {
                        0
                    };
                    let width = f64::from(g.x_advance) * style.size * style.stretch / em;
                    let offset = (f64::from(g.x_offset) / em, f64::from(g.y_offset) / em);
                    if !(style.size.is_finite()
                        && style.size > 0.0
                        && style.rise.is_finite()
                        && width.is_finite()
                        && offset.0.is_finite()
                        && offset.1.is_finite())
                    {
                        return Err(WidgetError::Malformed(
                            "non-finite embedded rich text glyph metrics".into(),
                        ));
                    }
                    glyphs.try_push(Glyph {
                        cell,
                        byte,
                        style,
                        width,
                        embedded: Some(Embedded {
                            id,
                            code,
                            actual,
                            offset,
                            metrics: available.metrics(id),
                        }),
                    })?;
                }
            }
        }
    }
    if unsupported {
        return Ok(Drawing::Removed);
    }
    if width > 0. && height > 0. && !glyphs.is_empty() {
        let clip_y = widget.height - y1;
        if !(x0.is_finite() && clip_y.is_finite() && width.is_finite() && height.is_finite()) {
            return Err(WidgetError::Malformed(
                "non-finite rich text clip rectangle".into(),
            ));
        }
        let clip = format!(
            "q {} {} {} {} re W n\n/Tx BMC\n",
            fmt(x0),
            fmt(clip_y),
            fmt(width),
            fmt(height)
        );
        content.try_extend_from_slice(clip.as_bytes())?;
        if let TextLayout::Comb(cells) = layout {
            comb(
                &mut content,
                &mut glyphs,
                cells,
                [x0, y0, x1, y1],
                widget.height,
                base.align,
                &actual_text,
            )?;
        } else {
            let lines = fit(&mut glyphs, base, (width, height), multiline, unit, budget)?;
            let mut top = widget.height - y0;
            for l in &*lines {
                let mut x = x0
                    + match l.align {
                        Align::Left => 0.,
                        Align::Center => (width - l.width).max(0.) / 2.,
                        Align::Right => (width - l.width).max(0.),
                    };
                let baseline = top - l.ascent;
                for group in glyphs[l.start..l.end].chunk_by(Glyph::same_style) {
                    show(&mut content, group, x, baseline, &actual_text)?;
                    x += group.iter().map(|g| g.width).sum::<f64>();
                }
                top = baseline - l.descent;
            }
        }
        content.try_extend_from_slice(b"EMC\nQ\n")?;
    }
    Ok(Drawing::Rich(
        WidgetAppearance {
            content: content.into_vec(),
            size: (widget.width, widget.height),
            matrix: widget.matrix,
            font: None,
            icon: None,
        },
        fonts.into_vec(),
        embedded.into_vec(),
    ))
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use pyropdf_store::budget::Budget;
    use pyropdf_store::index::Index;
    use pyropdf_store::save::FileBase;
    use pyropdf_store::value::{Bytes, Value};
    use pyropdf_store::version::Document;

    use crate::annotations::Reader;
    use crate::forms::appearance;

    use super::*;

    fn open(objects: &[&str]) -> Document {
        let mut out = b"%PDF-1.7\n".to_vec();
        let mut offsets = Vec::new();
        for (number, object) in (1..).zip(objects) {
            offsets.push(out.len());
            out.extend_from_slice(format!("{number} 0 obj\n{object}\nendobj\n").as_bytes());
        }
        let xref = out.len();
        out.extend_from_slice(
            format!("xref\n0 {}\n0000000000 65535 f \n", offsets.len() + 1).as_bytes(),
        );
        for offset in offsets {
            out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
        }
        out.extend_from_slice(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
                objects.len() + 1
            )
            .as_bytes(),
        );
        let budget = Budget::default();
        let index = Index::open(Bytes::new(out), &budget).unwrap();
        Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None)
    }

    #[test]
    fn tiny_user_unit_terminates_and_never_emits_inf_or_nan() {
        let rv = "<body xmlns=\"http://www.w3.org/1999/xhtml\"><p>Hello <span style=\"font-size:12pt;vertical-align:4pt\">World</span></p></body>";
        let doc = open(&[
            "<< /Type /Catalog /AcroForm 2 0 R >>",
            "<< /Fields [3 0 R] /DA (/Helv 0 Tf 0 g) >>",
            &format!(
                "<< /Subtype /Widget /FT /Tx /T (rich) /Ff 33554432 /Rect [0 0 100 20] \
                 /RV ({rv}) /DS (font-size:12pt;vertical-align:3pt) >>"
            ),
        ]);
        let version = doc.current();
        let budget = Budget::default();
        let widget_obj = version.get(3, &budget).unwrap().unwrap();
        let Value::Dict(widget) = &**widget_obj.value() else {
            panic!("expected widget dict");
        };
        for unit in [1e-311, 1e-309, f64::MIN_POSITIVE, 1e-12] {
            let reader = Reader::on_page(&version, &budget, unit);
            let Some(Drawing::Rich(drawn, _, _)) = appearance(&reader, widget).unwrap() else {
                panic!("expected rich drawing for unit={unit}");
            };
            let ops = String::from_utf8(drawn.content).unwrap();
            assert!(!ops.contains("inf"), "unit={unit}: {ops}");
            assert!(!ops.contains("NaN"), "unit={unit}: {ops}");
            assert!(ops.contains(" Tf "), "unit={unit}: {ops}");
        }
    }

    #[test]
    fn fit_rejects_non_finite_geometry_and_terminates_within_iteration_cap() {
        let budget = Budget::default();
        let mut base = Style::new(Base14Font::Helvetica, f64::INFINITY, [0.0; 3], Align::Left);
        base.auto_size = true;
        let mut glyphs = [Glyph {
            cell: 0,
            byte: b'A',
            embedded: None,
            width: f64::INFINITY,
            style: base,
        }];
        let err = fit(&mut glyphs, base, (100.0, 20.0), false, 1e-311, &budget)
            .err()
            .expect("non-finite base size must be rejected");
        assert!(matches!(err, WidgetError::Malformed(_)));

        // Finite huge size that still exceeds the box after MAX_FIT_ITERATIONS
        // stops cleanly at the iteration cap rather than looping indefinitely.
        let mut huge = Style::new(Base14Font::Helvetica, 1.0e200, [0.0; 3], Align::Left);
        huge.auto_size = true;
        let mut huge_glyphs = [Glyph {
            cell: 0,
            byte: b'W',
            embedded: None,
            width: 1.0e200,
            style: huge,
        }];
        let fitted = fit(&mut huge_glyphs, huge, (10.0, 10.0), false, 1.0, &budget).unwrap();
        assert_eq!(fitted.len(), 1);
        assert!(huge_glyphs[0].style.size.is_finite());
        assert!(huge_glyphs[0].style.size < 1.0e200);
    }
}
