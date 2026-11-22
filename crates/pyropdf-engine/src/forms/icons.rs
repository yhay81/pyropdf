//! Pushbutton appearance characteristics and icon fitting (ISO 32000-2 §12.5.6.19).

use pyropdf_store::budget::BudgetVec;
use pyropdf_store::value::{Dict, Name, Reference, Value};

use super::appearance::{TextLayout, drawn, integer, value_text};
use super::settle::{Drawing, WidgetAppearance, has_normal_stream};
use super::style::{WidgetStyle, fmt};
use super::{WidgetError, malformed};
use crate::annotations::Reader;
use crate::standard::{self, Align};

type Bounds = [f64; 4];

fn numbers<const N: usize>(
    reader: &Reader<'_>,
    dict: &Dict,
    key: &str,
    default: [f64; N],
) -> Result<[f64; N], WidgetError> {
    let Some(value) = dict.get(key) else {
        return Ok(default);
    };
    reader
        .numbers_of(value, N)?
        .as_deref()
        .and_then(|v| v.try_into().ok())
        .ok_or_else(|| malformed("invalid pushbutton icon coordinates"))
}

struct Fit {
    when: u8,
    proportional: bool,
    anchor: [f64; 2],
    full: bool,
}

impl Fit {
    fn read(reader: &Reader<'_>, mark: &Dict) -> Result<Self, WidgetError> {
        let value = reader.entry(mark, "IF")?;
        let empty = Dict::new();
        let fit = match value.as_deref() {
            Some(Value::Dict(dict)) => dict,
            None => &empty,
            _ => return Err(malformed("invalid pushbutton /IF")),
        };
        let code = |key, default, valid: &[u8]| -> Result<u8, WidgetError> {
            match reader.entry(fit, key)?.as_deref() {
                None => Ok(default),
                Some(Value::Name(n))
                    if n.as_bytes().len() == 1 && valid.contains(&n.as_bytes()[0]) =>
                {
                    Ok(n.as_bytes()[0])
                }
                _ => Err(malformed("invalid pushbutton icon scaling mode")),
            }
        };
        let anchor = numbers(reader, fit, "A", [0.5, 0.5])?;
        if anchor.iter().any(|v| !(0.0..=1.0).contains(v)) {
            return Err(malformed("invalid pushbutton icon alignment"));
        }
        let full = match reader.entry(fit, "FB")?.as_deref() {
            None => false,
            Some(Value::Boolean(v)) => *v,
            _ => return Err(malformed("invalid /FB in pushbutton /IF")),
        };
        Ok(Self {
            when: code("SW", b'A', b"ABSN")?,
            proportional: code("S", b'P', b"PA")? == b'P',
            anchor,
            full,
        })
    }

    fn paint(
        &self,
        reader: &Reader<'_>,
        mark: &Dict,
        key: &str,
        area: Bounds,
        content: &mut BudgetVec<'_, u8>,
    ) -> Result<Option<Reference>, WidgetError> {
        let Some(raw) = mark.get(key).or_else(|| mark.get("I")) else {
            return Ok(None);
        };
        let Value::Reference(reference) = raw else {
            return Err(malformed("pushbutton icons must be indirect Form XObjects"));
        };
        let obj = reader.resolve(raw)?;
        let Value::Stream(icon) = &*obj else {
            return Err(malformed("pushbutton icon must be a Form stream"));
        };
        if !matches!(reader.entry(icon.dict(),"Subtype")?.as_deref(), Some(Value::Name(n)) if n.as_bytes()==b"Form")
        {
            return Err(malformed("pushbutton icon must be a Form XObject"));
        }
        let [x0, y0, x1, y1] = numbers(reader, icon.dict(), "BBox", [0.0; 4])?;
        if x1 <= x0 || y1 <= y0 {
            return Err(malformed("pushbutton icon needs a positive bounding box"));
        }
        let [a, b, c, d, e, f] = numbers(reader, icon.dict(), "Matrix", [1., 0., 0., 1., 0., 0.])?;
        let determinant = a.mul_add(d, -(b * c));
        if !determinant.is_finite() || determinant == 0.0 {
            return Err(malformed("degenerate pushbutton icon matrix"));
        }
        let [mut left, mut bottom, mut right, mut top] = [
            f64::INFINITY,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::NEG_INFINITY,
        ];
        for x in [x0, x1] {
            for y in [y0, y1] {
                let u = a.mul_add(x, c.mul_add(y, e));
                let v = b.mul_add(x, d.mul_add(y, f));
                left = left.min(u);
                bottom = bottom.min(v);
                right = right.max(u);
                top = top.max(v);
            }
        }
        let (iw, ih) = (right - left, top - bottom);
        if !(iw.is_finite() && ih.is_finite() && iw > 0. && ih > 0.) {
            return Err(malformed("degenerate transformed icon box"));
        }
        let [x, y, xmax, ymax] = area;
        let (w, h) = (xmax - x, ymax - y);
        if w <= 0. || h <= 0. {
            return Ok(None);
        }
        let (sx, sy) = (w / iw, h / ih);
        let fit = sx.min(sy);
        let scale = match self.when {
            b'A' => true,
            b'B' => fit < 1.0,
            b'S' => sx >= 1.0 && sy >= 1.0 && (sx > 1.0 || sy > 1.0),
            _ => false,
        };
        let (sx, sy) = if !scale {
            (1., 1.)
        } else if self.proportional {
            (fit, fit)
        } else {
            (sx, sy)
        };
        let [ax, ay] = if self.proportional {
            self.anchor
        } else {
            [0.5, 0.5]
        };
        let (tx, ty) = (
            x + (w - iw * sx) * ax - left * sx,
            y + (h - ih * sy) * ay - bottom * sy,
        );
        if ![sx, sy, tx, ty].iter().all(|v| v.is_finite()) {
            return Err(malformed("nonfinite icon placement"));
        }
        let ops = format!(
            "q {} {} {} {} re W n {} 0 0 {} {} {} cm /Icon Do Q\n",
            fmt(x),
            fmt(y),
            fmt(w),
            fmt(h),
            fmt(sx),
            fmt(sy),
            fmt(tx),
            fmt(ty)
        );
        content.try_extend_from_slice(ops.as_bytes())?;
        Ok(Some(*reference))
    }
}

/// Returns the icon box and caption box in the rotated widget's Form space.
fn areas(rect: Bounds, position: i64, caption: &str, unit: f64) -> (Bounds, Bounds) {
    let [x0, y0, x1, y1] = rect;
    let mut icon = rect;
    let mut text = rect;
    if !caption.is_empty() {
        let vertical = (14. / unit).min((y1 - y0) * 0.5);
        let horizontal = (x1 - x0) * 0.5;
        match position {
            2 => {
                icon[1] += vertical;
                text[3] = y0 + vertical;
            }
            3 => {
                icon[3] -= vertical;
                text[1] = y1 - vertical;
            }
            4 => {
                icon[2] -= horizontal;
                text[0] = x1 - horizontal;
            }
            5 => {
                icon[0] += horizontal;
                text[2] = x0 + horizontal;
            }
            _ => {}
        }
    }
    (icon, text)
}

pub(super) fn appearances(
    reader: &Reader<'_>,
    widget: &Dict,
    mark: &Dict,
    form: Option<&Dict>,
    style: &WidgetStyle,
) -> Result<Drawing, WidgetError> {
    let fit = Fit::read(reader, mark)?;
    let position = match reader.entry(mark, "TP")?.as_deref() {
        None => 0,
        Some(v) => integer(Some(v))
            .filter(|v| (0..=6).contains(v))
            .ok_or_else(|| malformed("invalid pushbutton /TP"))?,
    };
    let inset = if fit.full { 0. } else { style.border_width };
    let rect = [inset, inset, style.width - inset, style.height - inset];
    let mut modes = BudgetVec::new(reader.budget);
    for (mode, icon_key, caption_key) in [("N", "I", "CA"), ("R", "RI", "RC"), ("D", "IX", "AC")] {
        if mark.get(icon_key).is_none()
            && mark.get(caption_key).is_none()
            && (mode != "N" || has_normal_stream(reader, widget)?)
        {
            continue;
        }
        let caption_value = reader.entry(mark, caption_key)?;
        let fallback = reader.entry(mark, "CA")?;
        let caption = value_text(reader, caption_value.as_deref().or(fallback.as_deref()))?;
        let caption = if position == 1 || !standard::is_winansi(&caption) {
            ""
        } else {
            &caption
        };
        let (icon_area, text_area) = areas(rect, position, caption, style.unit);
        let mut ops = BudgetVec::new(reader.budget);
        style.decoration(&mut ops)?;
        let icon = if position == 0 {
            None
        } else {
            fit.paint(reader, mark, icon_key, icon_area, &mut ops)?
        };
        let mut font = None;
        if !caption.is_empty() {
            let [x0, y0, x1, y1] = text_area;
            let text_style = style.undecorated(x1 - x0, y1 - y0);
            if text_style.width > 0. && text_style.height > 0. {
                let Drawing::Drawn(text) = drawn(
                    reader,
                    widget,
                    form,
                    &text_style,
                    caption,
                    TextLayout::SingleLine,
                    Align::Center,
                    None,
                )?
                else {
                    return Err(malformed("missing pushbutton caption appearance"));
                };
                ops.try_extend_from_slice(
                    format!("q 1 0 0 1 {} {} cm\n", fmt(x0), fmt(y0)).as_bytes(),
                )?;
                ops.try_extend_from_slice(&text.content)?;
                ops.try_extend_from_slice(b"Q\n")?;
                font = text.font;
            }
        }
        modes.try_push((
            Name::from(mode),
            WidgetAppearance {
                content: ops.into_vec(),
                size: (style.width, style.height),
                matrix: style.matrix,
                font,
                icon,
            },
        ))?;
    }
    Ok(Drawing::Modes(modes.into_vec()))
}
