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
    let mut adjustable = glyphs.iter().any(|g| g.style.auto_size);
    loop {
        let lines = lines(glyphs, size.0, multiline, base, budget)?;
        let fits_width = lines.iter().all(|l| l.width <= size.0);
        let total_height: f64 = lines.iter().map(|l| l.ascent + l.descent).sum();
        if !adjustable || (fits_width && total_height <= size.1) {
            return Ok(lines);
        }
        if base.auto_size {
            base.size *= standard::FIT_STEP;
        }
        adjustable = false;
        for (i, glyph) in glyphs.iter_mut().enumerate() {
            if i % 1024 == 0 {
                budget.poll()?;
            }
            if glyph.style.auto_size && glyph.style.size * unit >= standard::MIN_FIT_SIZE {
                glyph.style.size *= standard::FIT_STEP;
                glyph.style.rise *= standard::FIT_STEP;
                glyph.width *= standard::FIT_STEP;
                adjustable = true;
            }
        }
    }
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
    if let Some(embedded) = group[0].embedded {
        let settings = format!(
            "BT /PyroRich{} {} Tf {} {} {} rg {} Tz\n",
            embedded.id,
            fmt(style.size),
            fmt(r),
            fmt(g),
            fmt(b),
            fmt(style.stretch * 100.)
        );
        out.try_extend_from_slice(settings.as_bytes())?;
        let mut pen = x;
        for glyph in group {
            let e = glyph.embedded.expect("a rich text group shares its font");
            if let Some((start, end)) = e.actual {
                let original =
                    std::str::from_utf8(&actual_text[start..end]).expect("shaped Unicode");
                out.try_extend_from_slice(b"/Span <</ActualText <FEFF")?;
                for unit in original.encode_utf16() {
                    out.try_extend_from_slice(format!("{unit:04X}").as_bytes())?;
                }
                out.try_extend_from_slice(b">>> BDC\n")?;
            }
            let (dx, dy) = e.offset;
            let n = usize::from(e.code.length) * 2;
            let show = format!(
                "1 0 0 1 {} {} Tm <{:0n$X}> Tj\n",
                fmt(pen + dx * style.size * style.stretch),
                fmt(y + dy * style.size),
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
            fmt(style.stretch * 100.),
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
        let stroke = format!(
            "{} {} {} RG {} w {} {} m {} {} l S\n",
            fmt(r),
            fmt(g),
            fmt(b),
            fmt(style.size / 18.),
            fmt(x),
            fmt(at),
            fmt(x + width),
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
        let scale = (cell_width / width)
            .min((bottom - top) / (up + down))
            .min(1.);
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
    let mut fonts = BudgetVec::<Base14Font>::new(budget);
    let mut embedded = BudgetVec::new(budget);
    let mut base = Style::new(
        text.font,
        if text.size > 0. {
            text.size
        } else {
            (standard::MAX_FIT_SIZE / widget.unit)
                .min(height.max(standard::MIN_FIT_SIZE / widget.unit))
        },
        [text.color.0, text.color.1, text.color.2],
        align,
    );
    base.auto_size = text.size <= 0.;
    base.user_unit = widget.unit;
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
                    glyphs.try_push(Glyph {
                        cell,
                        byte,
                        style,
                        width: f64::from(g.x_advance) * style.size * style.stretch / em,
                        embedded: Some(Embedded {
                            id,
                            code,
                            actual,
                            offset: (f64::from(g.x_offset) / em, f64::from(g.y_offset) / em),
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
        let clip = format!(
            "q {} {} {} {} re W n\n/Tx BMC\n",
            fmt(x0),
            fmt(widget.height - y1),
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
            let lines = fit(
                &mut glyphs,
                base,
                (width, height),
                multiline,
                widget.unit,
                budget,
            )?;
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
