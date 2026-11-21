//! Styled line captions in the annotation's oriented frame. Every measurement
//! comes from the same font and text run that emits the PDF operators.

use std::fmt::Write as _;
use std::ops::Range;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::{Dict, Value};
use pyropdf_store::{Error, Result};
use unicode_bidi::BidiInfo;

use super::text::{FACE_RESOURCE, TextFit, TextFont, text_error};
use super::{Canvas, Reader, fmt};
use crate::fonts::shape::ShapeError;
use crate::forms::WidgetError;
use crate::forms::face::{FaceText, Undrawable};
use crate::forms::rich_style::{self, Style};
use crate::forms::text_string;
use crate::standard::{Align, Base14Font, DA_FONTS, char_metric, line_metrics, winansi_byte};

#[derive(Clone)]
struct Span {
    text: Range<usize>,
    style: Style,
    line: usize,
    advance: f64,
}

#[derive(Default)]
struct Row {
    align: Option<Align>,
    width: f64,
    left: f64,
    right: f64,
    ascent: f64,
    descent: f64,
    baseline: f64,
}

pub(super) struct Caption {
    text: String,
    spans: Vec<Span>,
    rows: Vec<Row>,
    pub width: f64,
    pub height: f64,
    pub fit: TextFit,
    standard: u16,
}

fn rich_error(error: WidgetError) -> Error {
    match error {
        WidgetError::Store(error) => error,
        other => Error::malformed(other.to_string()),
    }
}

impl Caption {
    pub(super) fn read(
        reader: &Reader<'_>,
        dict: &Dict,
        face: Option<&FaceText<'_>>,
    ) -> Result<Self> {
        let size = 10.0 / reader.unit;
        let mut initial = Style::new(Base14Font::Helvetica, size, [0.; 3], Align::Center);
        initial.user_unit = reader.unit;
        if let Some(color) = reader.color(dict, "C")? {
            initial.color = [color.0, color.1, color.2];
        }
        let mut spans = BudgetVec::<Span>::new(reader.budget);
        let mut text = BudgetVec::new(reader.budget);
        let mut row_count = 1;
        let mut append = |ch: char, style: Style| -> std::result::Result<(), WidgetError> {
            if ch == '\n' {
                row_count += 1;
                return Ok(());
            }
            if spans
                .last()
                .is_none_or(|s| s.line != row_count - 1 || s.style != style)
            {
                spans.try_push(Span {
                    text: text.len()..text.len(),
                    style,
                    line: row_count - 1,
                    advance: 0.0,
                })?;
            }
            text.try_extend_from_slice(ch.encode_utf8(&mut [0; 4]).as_bytes())?;
            if let Some(span) = spans.last_mut() {
                span.text.end = text.len();
            }
            Ok(())
        };
        let visible = matches!(
            reader.entry(dict, "Cap")?.as_deref(),
            Some(Value::Boolean(true))
        );
        if let Some(value) = reader.entry(dict, "RC")? {
            let runs = rich_style::read(&value, reader.budget).map_err(rich_error)?;
            if visible {
                rich_style::walk(&runs, initial, reader.budget, &mut append).map_err(rich_error)?;
            }
        } else if visible
            && let Some(Value::String(value)) = reader.entry(dict, "Contents")?.as_deref()
        {
            let bytes = value.as_bytes();
            reader
                .budget
                .check(LimitCode::AnnotationTextSize, bytes.len() as u64)?;
            reader.budget.charge(
                LimitCode::MemorySize,
                (bytes.len() as u64).saturating_mul(6),
            )?;
            let text = text_string(bytes);
            let mut chars = text.chars().peekable();
            while let Some(ch) = chars.next() {
                if ch == '\r' && chars.peek() == Some(&'\n') {
                    chars.next();
                }
                append(if ch == '\r' { '\n' } else { ch }, initial).map_err(rich_error)?;
            }
        }
        let mut rows = BudgetVec::new(reader.budget);
        rows.try_reserve_exact(row_count)?;
        for _ in 0..row_count {
            rows.try_push(Row::default())?;
        }
        let mut caption = Self {
            text: String::from_utf8(text.into_vec())
                .map_err(|_| Error::malformed("invalid caption text"))?,
            spans: spans.into_vec(),
            rows: rows.into_vec(),
            width: 0.0,
            height: 0.0,
            fit: TextFit::Fits,
            standard: 0,
        };
        if face.is_some() && !caption.text.is_ascii() {
            caption.order_spans(reader.budget)?;
        }
        caption.measure(face, size, reader.budget)?;
        Ok(caption)
    }

    /// Resolve direction for the whole line before shaping separately styled
    /// segments. Reversing only the characters within each segment leaves
    /// differently colored words in a right-to-left paragraph in the wrong order.
    fn order_spans(&mut self, budget: &Budget) -> Result<()> {
        let mut ordered = BudgetVec::new(budget);
        for spans in self.spans.chunk_by(|a, b| a.line == b.line) {
            let (Some(first), Some(last)) = (spans.first(), spans.last()) else {
                continue;
            };
            let start = first.text.start;
            let text = &self.text[start..last.text.end];
            if spans.len() == 1 || text.is_ascii() {
                ordered.try_extend_from_slice(spans)?;
                continue;
            }
            // Bound the byte-indexed classes, levels, and ranges held by unicode-bidi.
            budget.charge(
                LimitCode::MemorySize,
                (text.len() as u64).saturating_mul(64),
            )?;
            let bidi = BidiInfo::new(text, None);
            for para in &bidi.paragraphs {
                let (levels, runs) = bidi.visual_runs(para, para.range.clone());
                for run in runs {
                    budget.poll()?;
                    let from = start + run.start;
                    let to = start + run.end;
                    let i = spans.partition_point(|s| s.text.end <= from);
                    let j = spans.partition_point(|s| s.text.start < to);
                    let at = ordered.len();
                    for span in &spans[i..j] {
                        ordered.try_push(Span {
                            text: span.text.start.max(from)..span.text.end.min(to),
                            ..span.clone()
                        })?;
                    }
                    if levels[run.start].is_rtl() {
                        ordered[at..].reverse();
                    }
                }
            }
        }
        self.spans = ordered.into_vec();
        Ok(())
    }

    fn measure(
        &mut self,
        face: Option<&FaceText<'_>>,
        base_size: f64,
        budget: &Budget,
    ) -> Result<()> {
        for span in &mut self.spans {
            budget.poll()?;
            let style = span.style;
            let text = &self.text[span.text.clone()];
            let (advance, [left, descent, right, ascent]) = if let Some(face) = face {
                match face.undrawable(text) {
                    Ok(None) => (),
                    Ok(Some(Undrawable::NoGlyph(ch))) => {
                        self.fit = TextFit::NoGlyph(ch);
                        return Ok(());
                    }
                    Ok(Some(Undrawable::NoOutline(ch))) => {
                        self.fit = TextFit::NoOutline(ch);
                        return Ok(());
                    }
                    Err(ShapeError::Limit(limit)) => return Err(limit.into()),
                    Err(ShapeError::Font(reason)) => {
                        return Err(Error::malformed(format!("invalid caption font: {reason}")));
                    }
                }
                face.line_metrics(text, style.size).map_err(text_error)?
            } else {
                let font = style.font();
                let scale = style.size / 1000.0;
                let mut width = 0.0;
                let mut left = 0.0_f64;
                let mut right = 0.0_f64;
                for ch in text.chars() {
                    budget.poll()?;
                    let Some(metric) = char_metric(font, ch) else {
                        self.fit = TextFit::NotWinAnsi;
                        return Ok(());
                    };
                    if let Some(bbox) = &metric.bbox {
                        left = left.min(width + f64::from(bbox.llx) * scale);
                        right = right.max(width + f64::from(bbox.urx) * scale);
                    }
                    width += f64::from(metric.width_x) * scale;
                }
                let i = DA_FONTS
                    .iter()
                    .position(|&(_, base)| base == font.pdf_base_name())
                    .ok_or_else(|| Error::malformed("unknown caption font"))?;
                self.standard |= 1 << i;
                let m = line_metrics(font);
                (
                    width,
                    [
                        left,
                        m.descender * style.size,
                        right,
                        m.ascender * style.size,
                    ],
                )
            };
            span.advance = advance * style.stretch;
            let row = &mut self.rows[span.line];
            row.align.get_or_insert(style.align);
            let name = style.font().pdf_base_name();
            let slant = if face.is_some() && (name.contains("Italic") || name.contains("Oblique")) {
                0.2
            } else {
                0.0
            };
            let stroke = if face.is_some() && name.contains("Bold") {
                style.size / 80.0
            } else {
                0.0
            };
            row.left = row
                .left
                .min(row.width + left * style.stretch + slant * descent - stroke);
            row.right = row
                .right
                .max(row.width + right * style.stretch + slant * ascent + stroke);
            row.width += span.advance;
            row.ascent = row.ascent.max(ascent + style.rise + stroke);
            row.descent = row.descent.min(descent + style.rise - stroke);
        }
        if !self.spans.is_empty() {
            for row in &mut self.rows {
                if row.ascent == row.descent {
                    row.ascent = base_size * 0.8;
                    row.descent = -base_size * 0.2;
                }
                self.width = self.width.max(row.right.max(row.width) - row.left);
                row.baseline = -self.height - row.ascent;
                self.height += row.ascent - row.descent;
            }
        }
        if !self.width.is_finite() || !self.height.is_finite() {
            return Err(Error::malformed("nonfinite line caption extents"));
        }
        Ok(())
    }

    pub(super) fn draw(
        &self,
        canvas: &mut Canvas<'_>,
        mut face: Option<&mut FaceText<'_>>,
    ) -> Result<Option<TextFont>> {
        let mut current = None;
        let mut pen = 0.0;
        for span in &self.spans {
            let row = &self.rows[span.line];
            if current != Some(span.line) {
                current = Some(span.line);
                let width = row.right.max(row.width) - row.left;
                let remaining = self.width - width;
                let shift = match row.align.unwrap_or(Align::Center) {
                    Align::Left => 0.0,
                    Align::Center => remaining / 2.0,
                    Align::Right => remaining,
                };
                pen = -self.width / 2.0 + shift - row.left;
            }
            let style = span.style;
            let text = &self.text[span.text.clone()];
            let y = row.baseline + self.height / 2.0 + style.rise;
            let base = style.font().pdf_base_name();
            let name = if face.is_some() {
                FACE_RESOURCE
            } else {
                DA_FONTS
                    .iter()
                    .find(|&&(_, b)| b == base)
                    .map(|&(name, _)| name)
                    .ok_or_else(|| Error::malformed("unknown caption font"))?
            };
            let [r, g, b] = style.color;
            let italic = face.is_some() && (base.contains("Italic") || base.contains("Oblique"));
            let bold = face.is_some() && base.contains("Bold");
            let _ = write!(canvas.out, "q {} {} {} rg ", fmt(r), fmt(g), fmt(b));
            if bold {
                let _ = write!(
                    canvas.out,
                    "{} {} {} RG {} w ",
                    fmt(r),
                    fmt(g),
                    fmt(b),
                    fmt(style.size / 40.0)
                );
            }
            let _ = write!(
                canvas.out,
                "BT /{name} {} Tf {} 0 {} 1 {} {} Tm ",
                fmt(style.size),
                fmt(style.stretch),
                if italic { "0.2" } else { "0" },
                fmt(pen),
                fmt(y)
            );
            if bold {
                canvas.out.push_str("2 Tr ");
            }
            if let Some(face) = face.as_deref_mut() {
                let mut buf = BudgetVec::new(canvas.out.bytes.budget());
                face.show(&mut buf, text, style.size).map_err(text_error)?;
                canvas.out.push_str(
                    std::str::from_utf8(&buf)
                        .map_err(|_| Error::malformed("invalid caption operators"))?,
                );
            } else {
                canvas.out.push('<');
                for ch in text.chars() {
                    if let Some(byte) = winansi_byte(ch) {
                        let _ = write!(canvas.out, "{byte:02X}");
                    }
                }
                canvas.out.push_str("> Tj ");
            }
            canvas.out.push_str("ET ");
            if let Some(underline) = style.decoration {
                let level = y + if underline { -0.12 } else { 0.3 } * style.size;
                let _ = write!(
                    canvas.out,
                    "{} {} {} RG {} w {} {} m {} {} l S ",
                    fmt(r),
                    fmt(g),
                    fmt(b),
                    fmt(style.size / 18.0),
                    fmt(pen),
                    fmt(level),
                    fmt(pen + span.advance),
                    fmt(level)
                );
            }
            canvas.out.push_str("Q\n");
            pen += span.advance;
        }
        Ok(match face {
            Some(face) => face.font().map(TextFont::Embedded),
            None if self.standard != 0 => Some(TextFont::Standards(self.standard)),
            None => None,
        })
    }
}
