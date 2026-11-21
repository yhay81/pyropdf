//! Paint scoped XHTML text in the coordinate system of the widget appearance.

use super::WidgetError;
use super::rich_style::{self, Style};
use super::rich_text::Run;
use super::settle::{Drawing, WidgetAppearance};
use super::style::{TextStyle, WidgetStyle, fmt};
use crate::standard::{self, Align, Base14Font};
use pyropdf_store::budget::{Budget, BudgetVec};

#[derive(Clone, Copy)]
struct Glyph {
    byte: u8,
    width: f64,
    style: Style,
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
        let (up, down) = standard::vertical_metrics(style.font());
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
) -> Result<(), WidgetError> {
    let style = group[0].style;
    let [r, g, b] = style.color;
    let y = baseline + style.rise;
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
    let mut width = 0.;
    for (index, glyph) in group.iter().enumerate() {
        if index % 1024 == 0 {
            out.budget().poll()?;
        }
        out.try_extend_from_slice(&[
            HEX[usize::from(glyph.byte >> 4)],
            HEX[usize::from(glyph.byte & 15)],
        ])?;
        width += glyph.width;
    }
    out.try_extend_from_slice(b"> Tj ET\n")?;
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

pub(super) fn draw(
    runs: &[Run],
    css: &str,
    widget: &WidgetStyle,
    text: &TextStyle,
    align: Align,
    multiline: bool,
    budget: &Budget,
) -> Result<Drawing, WidgetError> {
    let mut content = BudgetVec::new(budget);
    widget.decoration(&mut content)?;
    let [x0, y0, x1, y1] = widget.content_rect();
    let (width, height) = (x1 - x0, y1 - y0);
    let mut fonts = BudgetVec::<Base14Font>::new(budget);
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
    base.css(css);
    let mut glyphs = BudgetVec::new(budget);
    let mut unsupported = false;
    rich_style::walk(runs, base, budget, |c, style| {
        let c = if !multiline && c == '\n' { ' ' } else { c };
        let font = style.font();
        let Some(byte) = (if c == '\n' {
            Some(b'\n')
        } else {
            standard::glyph_code(font, c)
        }) else {
            unsupported = true;
            return Ok(());
        };
        let advance = if byte == b'\n' {
            0.
        } else {
            standard::text_width(c.encode_utf8(&mut [0; 4]), font, style.size).unwrap_or_default()
                * style.stretch
        };
        if !fonts.contains(&font) {
            fonts.try_push(font)?;
        }
        glyphs.try_push(Glyph {
            byte,
            width: advance,
            style,
        })?;
        Ok(())
    })?;
    if unsupported {
        return Ok(Drawing::Removed);
    }
    if width > 0. && height > 0. && !glyphs.is_empty() {
        let lines = fit(
            &mut glyphs,
            base,
            (width, height),
            multiline,
            widget.unit,
            budget,
        )?;
        let clip = format!(
            "q {} {} {} {} re W n\n/Tx BMC\n",
            fmt(x0),
            fmt(widget.height - y1),
            fmt(width),
            fmt(height)
        );
        content.try_extend_from_slice(clip.as_bytes())?;
        let mut top = widget.height - y0;
        for l in &*lines {
            let mut x = x0
                + match l.align {
                    Align::Left => 0.,
                    Align::Center => (width - l.width).max(0.) / 2.,
                    Align::Right => (width - l.width).max(0.),
                };
            let baseline = top - l.ascent;
            for group in glyphs[l.start..l.end].chunk_by(|a, b| a.style == b.style) {
                show(&mut content, group, x, baseline)?;
                x += group.iter().map(|g| g.width).sum::<f64>();
            }
            top = baseline - l.descent;
        }
        content.try_extend_from_slice(b"EMC\nQ\n")?;
    }
    Ok(Drawing::Rich(
        WidgetAppearance {
            content: content.into_vec(),
            size: (widget.width, widget.height),
            matrix: widget.matrix,
            font: None,
        },
        fonts.into_vec(),
    ))
}
