//! Caption and leader geometry in the line's oriented default-user-space frame.

use std::fmt::Write as _;

use pyropdf_store::value::Dict;
use pyropdf_store::{Error, Result};

use super::line_caption::Caption;
use super::shapes::draw_ending;
use super::text::{TextFit, TextFont};
use super::{Canvas, Paint, Reader, fmt};
use crate::forms::face::FaceText;

type Point = (f64, f64);

pub(super) struct Dimension {
    origin: Point,
    axis: Point,
    length: f64,
    offset: f64,
    height: f64,
    extension: f64,
    caption: Caption,
    font_size: f64,
    text_center: Point,
    inline: bool,
}

impl Dimension {
    pub(super) fn read(
        reader: &Reader<'_>,
        dict: &Dict,
        vertices: &[Point],
        face: Option<&FaceText<'_>>,
    ) -> Result<Option<Self>> {
        if face.is_none()
            && !["LL", "LLE", "LLO", "Cap", "RC"]
                .iter()
                .any(|k| dict.get(*k).is_some())
        {
            return Ok(None);
        }
        let [start, end] = vertices else {
            return Ok(None);
        };
        let delta = (end.0 - start.0, end.1 - start.1);
        let length = delta.0.hypot(delta.1);
        if !(length.is_finite() && length > 0.0) {
            return Ok(None);
        }
        let number = |key: &str| -> Result<f64> {
            match dict.get(key) {
                Some(value) => reader
                    .number(value)?
                    .ok_or_else(|| Error::malformed("invalid annotation leader length")),
                None => Ok(0.0),
            }
        };
        let leader = number("LL")?;
        let offset = number("LLO")?;
        let extension = number("LLE")?;
        if offset < 0.0 || extension < 0.0 {
            return Err(Error::malformed(
                "negative annotation leader offset or extension",
            ));
        }
        let direction = if leader < 0.0 { -1.0 } else { 1.0 };
        let offset = if leader == 0.0 {
            0.0
        } else {
            direction * offset
        };
        let height = offset + leader;
        let extension = if leader == 0.0 {
            0.0
        } else {
            direction * extension
        };
        let caption = Caption::read(reader, dict, face)?;
        let font_size = 10.0 / reader.unit;
        let inline = reader.name(dict, "CP")?.as_deref() != Some(b"Top");
        let co = reader.numbers(dict, "CO", 2)?;
        let [along, across] = co
            .as_deref()
            .and_then(|v| v.try_into().ok())
            .unwrap_or([0.0, 0.0]);
        Ok(Some(Self {
            origin: *start,
            axis: (delta.0 / length, delta.1 / length),
            length,
            offset,
            height,
            extension,
            font_size,
            text_center: (
                length / 2.0 + along,
                height
                    + across
                    + if inline {
                        0.0
                    } else {
                        caption.height / 2.0 + font_size * 0.2
                    },
            ),
            inline,
            caption,
        }))
    }

    fn at(&self, along: f64, across: f64) -> Point {
        let (x, y) = self.axis;
        (
            x.mul_add(along, (-y).mul_add(across, self.origin.0)),
            y.mul_add(along, x.mul_add(across, self.origin.1)),
        )
    }

    pub(super) fn expand(&self, rect: &mut [f64; 4]) {
        let mut include = |(x, y): Point| {
            rect[0] = rect[0].min(x);
            rect[1] = rect[1].min(y);
            rect[2] = rect[2].max(x);
            rect[3] = rect[3].max(y);
        };
        for x in [0.0, self.length] {
            include(self.at(x, self.offset));
            include(self.at(x, self.height + self.extension));
        }
        if self.caption.height > 0.0 {
            for x in [-self.caption.width / 2.0, self.caption.width / 2.0] {
                for y in [-self.caption.height / 2.0, self.caption.height / 2.0] {
                    include(self.at(self.text_center.0 + x, self.text_center.1 + y));
                }
            }
        }
    }

    pub(super) fn draw(
        &self,
        canvas: &mut Canvas<'_>,
        paint: &Paint,
        endings: [usize; 2],
        rect: [f64; 4],
        face: Option<&mut FaceText<'_>>,
    ) -> Result<(TextFit, Option<TextFont>)> {
        let to_canvas = |(x, y): Point| (x - rect[0], rect[3] - y);
        if let Some(stroke) = paint.stroke(0, 1) {
            let mut segment = |a: Point, b: Point| {
                let (x0, y0) = to_canvas(a);
                let (x1, y1) = to_canvas(b);
                canvas.path("ml", &[x0, y0, x1, y1]);
                canvas.draw(None, Some(&stroke));
            };
            if self.height != self.offset {
                for x in [0.0, self.length] {
                    segment(
                        self.at(x, self.offset),
                        self.at(x, self.height + self.extension),
                    );
                }
            }
            let gap = self.caption.width / 2.0 + self.font_size * 0.25;
            let left = (self.text_center.0 - gap).clamp(0.0, self.length);
            let right = (self.text_center.0 + gap).clamp(0.0, self.length);
            if self.inline
                && self.caption.height > 0.0
                && (self.text_center.1 - self.height).abs() < self.caption.height / 2.0
            {
                if left > 0.0 {
                    segment(self.at(0.0, self.height), self.at(left, self.height));
                }
                if right < self.length {
                    segment(
                        self.at(right, self.height),
                        self.at(self.length, self.height),
                    );
                }
            } else {
                segment(self.at(0.0, self.height), self.at(self.length, self.height));
            }
        }
        let start = self.at(0.0, self.height);
        let end = self.at(self.length, self.height);
        draw_ending(canvas, paint, endings[0], start, end, to_canvas)?;
        draw_ending(canvas, paint, endings[1], end, start, to_canvas)?;
        if self.caption.fit != TextFit::Fits {
            return Ok((self.caption.fit, None));
        }
        let (x, y) = self.at(self.text_center.0, self.text_center.1);
        let (a, b) = self.axis;
        let _ = writeln!(
            canvas.out,
            "q {} {} {} {} {} {} cm",
            fmt(a),
            fmt(b),
            fmt(-b),
            fmt(a),
            fmt(x - rect[0]),
            fmt(y - rect[1])
        );
        let font = self.caption.draw(canvas, face)?;
        canvas.out.push_str("Q\n");
        Ok((TextFit::Fits, font))
    }
}
