//! Creating, editing, drawing with embedded faces, and removing annotations.

use std::collections::HashMap;

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Memo;
use pyropdf_store::pages::{PageIndex, PageTree};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::save::deflate;
use pyropdf_store::settle::Structure;
use pyropdf_store::value::{Array, Bytes, Dict, Name, PdfString, Reference, Stream, Value};
use pyropdf_store::version::Transaction;

use crate::display::{encode_text, number, to_pdf};
use crate::fonts::embed::Face;
use crate::forms::face::{FaceFont, FaceText};
use crate::standard::DA_FONTS;

use super::geometry::{
    ADDED_SUBTYPES, CalloutPoints, FILE_ICONS, NOTE_ICONS, RECT_SUBTYPES, Rect, STAMP_ICONS,
    border_style, cloud_effect, line_endings_value, point_entry, rgb,
};
use super::read::PageAnnots;
use super::{
    AnnotationError, AnnotationResult as Result, BLEND_MODES, LINE_ENDINGS, MAX_QUADS, PointLists,
    TextFit, appearance, ending_size, face_appearance, fmt, has_points, is_text_markup,
    zigzag_points,
};

/// Executes `write` on the target page's [`PageAnnots`] inside a transaction
/// savepoint, rolling back writes if `write` fails. Returns `Ok(None)` when
/// `(number, generation)` is no longer a page.
pub(super) fn with_page_edit<T>(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    write: impl FnOnce(&PageAnnots<'_>, &mut Transaction, &Budget) -> Result<T>,
) -> Result<Option<T>> {
    let version = transaction.snapshot();
    let index = PageIndex::memoized(draft_pages, &version, budget)?;
    let Some(annots) = PageAnnots::read(version, index, budget, number, generation)? else {
        return Ok(None);
    };
    let savepoint = transaction.savepoint();
    let result = write(&annots, transaction, budget);
    if result.is_err() {
        transaction.rollback(savepoint);
    }
    result.map(Some)
}

/// Refuses a free-text annotation whose text cannot be drawn by a Standard 14
/// font or overflows its rectangle.
pub(super) fn check_text(annots: &PageAnnots<'_>, dict: &Dict) -> Result<()> {
    if !matches!(dict.get("Subtype"), Some(Value::Name(name)) if name.as_bytes() == b"FreeText") {
        return Ok(());
    }
    let drawn = appearance(&annots.reader(), dict)?;
    match drawn.map(|drawn| drawn.text_fit()) {
        Some(TextFit::Unfit) => Err(AnnotationError::Pdf(
            "the text does not fit in the free text's rectangle; enlarge it or use a smaller size"
                .to_owned(),
        )),
        Some(TextFit::NotWinAnsi) => Err(AnnotationError::Pdf(
            "free text draws in a Standard 14 font, which prints WinAnsi characters only; \
             an embedded face is known only in the session that added the free text in it"
                .to_owned(),
        )),
        _ => Ok(()),
    }
}

/// Typed patch of annotation properties passed to [`annotation_edit`] and
/// [`PageAnnots::edit`].
#[derive(Clone, Debug, Default)]
pub struct AnnotationPatch {
    /// New display-space rectangle (only for [`RECT_SUBTYPES`] without callouts).
    pub rect: Option<Rect>,
    /// `/Contents` text.
    pub contents: Option<String>,
    /// `/T` author text.
    pub author: Option<String>,
    /// `/Subj` subject text.
    pub subject: Option<String>,
    /// `/NM` unique annotation name.
    pub name: Option<String>,
    /// `/Lang` language tag.
    pub language: Option<String>,
    /// `/Name` icon for notes, stamps, and file attachments.
    pub icon: Option<String>,
    /// `/C` RGB stroke/highlight color in `[0.0, 1.0]`.
    pub color: Option<(f64, f64, f64)>,
    /// `/IC` RGB interior color in `[0.0, 1.0]`.
    pub interior_color: Option<(f64, f64, f64)>,
    /// `/CA` opacity in `(0.0, 1.0]`.
    pub opacity: Option<f64>,
    /// `/F` flags (`0..=1023`).
    pub flags: Option<i64>,
    /// Border `(width_pts, style, dashes_pts)`.
    pub border: Option<(f64, String, Vec<f64>)>,
    /// Cloudy border intensity `(0.0, 2.0]`.
    pub clouds: Option<f64>,
    /// `/BM` blend mode name.
    pub blend_mode: Option<String>,
    /// Display clockwise rotation in multiples of 90 degrees.
    pub rotate: Option<i64>,
    /// `/M` PDF date string (`D:...`).
    pub modified: Option<String>,
    /// Display-space point lists for lines, polygons, polylines, and ink.
    pub points: Option<PointLists>,
    /// `/LE` start and end styles for lines and polylines.
    pub line_endings: Option<(String, String)>,
    /// Property names to clear from the dictionary.
    pub clear: Vec<String>,
}

impl AnnotationPatch {
    /// Applies this patch to `dict`, an annotation of `subtype` on `annots`.
    #[expect(
        clippy::disallowed_methods,
        reason = "only fixed text keys and one geometry key are inserted; their text and point data are charged first"
    )]
    pub fn apply(&self, annots: &PageAnnots<'_>, dict: &mut Dict, subtype: &str) -> Result<()> {
        let texts = [
            (&self.contents, "Contents"),
            (&self.author, "T"),
            (&self.subject, "Subj"),
            (&self.name, "NM"),
            (&self.language, "Lang"),
        ];
        let size = texts
            .iter()
            .map(|(text, _)| text.as_deref().map_or(0, str::len))
            .fold(subtype.len(), usize::saturating_add);
        annots
            .budget
            .charge(LimitCode::AnnotationInputSize, size as u64)?;
        annots
            .budget
            .charge(LimitCode::MemorySize, size.saturating_mul(3) as u64)?;
        for (text, key) in texts {
            if let Some(text) = text {
                dict.insert(Name::from(key), encode_text(text));
            }
        }
        for property in &self.clear {
            let key = match property.as_str() {
                "contents" => "Contents",
                "author" => "T",
                "subject" => "Subj",
                "name" => "NM",
                "language" => "Lang",
                "color" => "C",
                "interior_color" => "IC",
                "blend_mode" => "BM",
                "border" => {
                    dict.remove("Border");
                    "BS"
                }
                "clouds" => "BE",
                other => {
                    return Err(AnnotationError::Pdf(format!(
                        "an annotation property that cannot be removed: {other:?}"
                    )));
                }
            };
            dict.remove(key);
        }
        if let Some(icon) = &self.icon {
            let known: &[&str] = match subtype {
                "Text" => &NOTE_ICONS,
                "Stamp" => &STAMP_ICONS,
                "FileAttachment" => &FILE_ICONS,
                _ => {
                    return Err(AnnotationError::Pdf(
                        "only a note, a stamp, or a file attachment has an icon".to_owned(),
                    ));
                }
            };
            if !known.contains(&icon.as_str()) {
                return Err(AnnotationError::Pdf(format!("unknown icon {icon:?}")));
            }
            dict.insert(Name::from("Name"), Value::Name(Name::from(icon.as_str())));
        }
        if let Some(color) = self.color {
            dict.insert(Name::from("C"), rgb(color, "color")?);
        }
        if let Some(color) = self.interior_color {
            dict.insert(Name::from("IC"), rgb(color, "interior color")?);
        }
        if let Some(opacity) = self.opacity {
            if !(opacity.is_finite() && opacity > 0.0 && opacity <= 1.0) {
                return Err(AnnotationError::Pdf(
                    "an annotation opacity is greater than 0 and at most 1".to_owned(),
                ));
            }
            if opacity < 1.0 {
                dict.insert(Name::from("CA"), Value::from(opacity));
            } else {
                dict.remove("CA");
            }
        }
        if let Some(flags) = self.flags {
            if !(0..=1023).contains(&flags) {
                return Err(AnnotationError::Pdf(
                    "annotation flags are bits 1 to 10".to_owned(),
                ));
            }
            dict.insert(Name::from("F"), Value::Integer(flags));
        }
        if let Some(border) = &self.border {
            dict.remove("Border");
            dict.insert(
                Name::from("BS"),
                border_style(border, annots.unit, annots.budget)?,
            );
        }
        if let Some(intensity) = self.clouds {
            dict.insert(Name::from("BE"), cloud_effect(subtype, intensity)?);
        }
        if let Some(lists) = &self.points {
            let (key, value) = point_entry(annots, subtype, lists)?;
            dict.insert(Name::from(key), value);
        }
        if let Some(endings) = &self.line_endings {
            if !matches!(subtype, "Line" | "PolyLine") {
                return Err(AnnotationError::Pdf(
                    "only lines and polylines have line endings".to_owned(),
                ));
            }
            dict.insert(Name::from("LE"), line_endings_value(endings)?);
        }
        if let Some(mode) = &self.blend_mode {
            if !BLEND_MODES.contains(&mode.as_str()) {
                return Err(AnnotationError::Pdf(format!("unknown blend mode {mode:?}")));
            }
            dict.insert(Name::from("BM"), Value::Name(Name::from(mode.as_str())));
        }
        if let Some(turn) = self.rotate {
            if !matches!(subtype, "FreeText" | "Stamp" | "Caret" | "FileAttachment")
                || turn % 90 != 0
            {
                return Err(AnnotationError::Pdf(
                    "free text, stamps, carets, and file attachments turn by multiples of 90 degrees"
                        .to_owned(),
                ));
            }
            let user_turn = (annots.rotation - turn).rem_euclid(360);
            if user_turn == 0 {
                dict.remove("Rotate");
            } else {
                dict.insert(Name::from("Rotate"), Value::Integer(user_turn));
            }
        }
        if let Some(area) = self.rect {
            if !RECT_SUBTYPES.contains(&subtype) || dict.get("CL").is_some() {
                return Err(AnnotationError::Pdf(
                    "this annotation follows its points or callout; edit those instead of its rectangle"
                        .to_owned(),
                ));
            }
            let (x0, y0, x1, y1) = area;
            if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) {
                return Err(AnnotationError::Pdf(
                    "an annotation rectangle is finite".to_owned(),
                ));
            }
            dict.insert(Name::from("Rect"), annots.to_pdf(area));
        }
        if let Some(modified) = &self.modified {
            if !modified.starts_with("D:") || !modified.is_ascii() || modified.len() > 64 {
                return Err(AnnotationError::Pdf(
                    "an annotation date is a PDF date string".to_owned(),
                ));
            }
            dict.insert(
                Name::from("M"),
                Value::String(PdfString::literal(modified.as_bytes())),
            );
        }
        Ok(())
    }
}

/// Specification of a new annotation added by [`annotation_add`] or
/// [`PageAnnots::add`].
#[derive(Clone, Debug, Default)]
pub struct AnnotationSpec<'a> {
    /// Annotation `/Subtype` name (one of [`ADDED_SUBTYPES`]).
    pub subtype: String,
    /// Display rectangles for text markup (`Highlight`, `Underline`,
    /// `StrikeOut`, `Squiggly`).
    pub rects: Option<Vec<Rect>>,
    /// Display rectangle for rectangular annotations.
    pub rect: Option<Rect>,
    /// `/C` RGB stroke/highlight color in `[0.0, 1.0]`.
    pub color: Option<(f64, f64, f64)>,
    /// `/CA` opacity in `(0.0, 1.0]`.
    pub opacity: Option<f64>,
    /// `/Contents` text.
    pub contents: Option<String>,
    /// Creation/modification PDF date string (`D:...`).
    pub created: Option<String>,
    /// `/Name` icon for notes, stamps, and file attachments.
    pub icon: Option<String>,
    /// Initial `/Open` state for notes.
    pub open: Option<bool>,
    /// Display-space point lists for lines, polygons, polylines, and ink.
    pub points: Option<PointLists>,
    /// `/LE` start and end styles for lines and polylines.
    pub line_endings: Option<(String, String)>,
    /// `/IC` RGB interior color in `[0.0, 1.0]`.
    pub interior_color: Option<(f64, f64, f64)>,
    /// Border `(width_pts, style, dashes_pts)`.
    pub border: Option<(f64, String, Vec<f64>)>,
    /// Cloudy border intensity `(0.0, 2.0]`.
    pub clouds: Option<f64>,
    /// `/BM` blend mode name.
    pub blend_mode: Option<String>,
    /// Embedded file payload for `FileAttachment`.
    pub data: Option<&'a [u8]>,
    /// Embedded file name for `FileAttachment`.
    pub filename: Option<String>,
    /// Optional file description (`/Desc`) for `FileAttachment`.
    pub description: Option<String>,
    /// Optional per-call `embedded_file_size` cap.
    pub max_size: Option<u64>,
    /// Free-text RGB text color in `[0.0, 1.0]`.
    pub text_color: Option<(f64, f64, f64)>,
    /// Free-text font size in display points.
    pub font_size: Option<f64>,
    /// Free-text Standard 14 font name.
    pub font_name: Option<String>,
    /// Free-text `/Q` alignment (`0`, `1`, or `2`).
    pub align: Option<i64>,
    /// Display clockwise rotation in multiples of 90 degrees.
    pub rotate: Option<i64>,
    /// Free-text callout line points.
    pub callout: Option<CalloutPoints>,
    /// Free-text callout line ending style.
    pub callout_ending: Option<String>,
}

impl AnnotationSpec<'_> {
    /// Builds and validates the annotation dictionary (excluding `/P` and
    /// embedded file streams).
    #[expect(
        clippy::disallowed_methods,
        reason = "inserts only fixed PDF keys after charging all input-derived text and geometry"
    )]
    pub fn dictionary(&self, annots: &PageAnnots<'_>) -> Result<Dict> {
        let subtype = self.subtype.as_str();
        let created = self.created.as_deref().unwrap_or_default();
        let size = [
            subtype,
            self.contents.as_deref().unwrap_or_default(),
            created,
            created,
        ]
        .iter()
        .map(|text| text.len())
        .fold(0, usize::saturating_add);
        annots
            .budget
            .charge(LimitCode::AnnotationInputSize, size as u64)?;
        annots
            .budget
            .charge(LimitCode::MemorySize, size.saturating_mul(3) as u64)?;
        let markup = is_text_markup(subtype.as_bytes());
        if self.rects.is_some() != markup {
            return Err(AnnotationError::Pdf(format!(
                "rectangles place text markup, not a {subtype} annotation"
            )));
        }
        if !ADDED_SUBTYPES.contains(&subtype) {
            return Err(AnnotationError::Pdf(format!(
                "pyropdf does not add {subtype} annotations"
            )));
        }
        self.check_file(annots.budget)?;
        let mut dict = Dict::new();
        dict.insert(Name::from("Type"), Value::Name(Name::from("Annot")));
        dict.insert(Name::from("Subtype"), Value::Name(Name::from(subtype)));
        if let Some(rects) = &self.rects {
            if rects.is_empty() {
                return Err(AnnotationError::Pdf(format!(
                    "text markup covers 1 to {MAX_QUADS} rectangles"
                )));
            }
            annots
                .budget
                .check(LimitCode::AnnotationQuadCount, rects.len() as u64)?;
            let mut quads = BudgetVec::new(annots.budget);
            quads.try_reserve_exact(rects.len())?;
            for &(x0, y0, x1, y1) in rects {
                if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) {
                    return Err(AnnotationError::Pdf(
                        "text markup rectangles are finite".to_owned(),
                    ));
                }
                quads.try_push(
                    [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
                        .map(|(x, y)| to_pdf(annots.crop, annots.rotation, annots.unit, x, y)),
                )?;
            }
            if subtype == "Squiggly" {
                annots.budget.check(
                    LimitCode::AnnotationPointCount,
                    zigzag_points(&quads) as u64,
                )?;
            }
            let mut points = BudgetVec::new(annots.budget);
            points.try_extend(
                quads
                    .iter()
                    .flatten()
                    .flat_map(|&(x, y)| [number(x), number(y)]),
            )?;
            dict.insert(
                Name::from("QuadPoints"),
                Value::Array(Array::from(points.into_vec())),
            );
            let (x0, y0, x1, y1) = quads.iter().flatten().fold(
                (
                    f64::INFINITY,
                    f64::INFINITY,
                    f64::NEG_INFINITY,
                    f64::NEG_INFINITY,
                ),
                |(x0, y0, x1, y1), &(x, y)| (x0.min(x), y0.min(y), x1.max(x), y1.max(y)),
            );
            dict.insert(
                Name::from("Rect"),
                Value::Array(Array::from(vec![
                    number(x0),
                    number(y0),
                    number(x1),
                    number(y1),
                ])),
            );
        } else if has_points(subtype.as_bytes()) {
            let Some(lists) = self.points.as_ref().filter(|_| self.rect.is_none()) else {
                return Err(AnnotationError::Pdf(format!(
                    "a {subtype} annotation takes points"
                )));
            };
            let (key, value) = point_entry(annots, subtype, lists)?;
            let mut bounds = (
                f64::INFINITY,
                f64::INFINITY,
                f64::NEG_INFINITY,
                f64::NEG_INFINITY,
            );
            for &(x, y) in lists.iter().flatten() {
                let (x, y) = to_pdf(annots.crop, annots.rotation, annots.unit, x, y);
                bounds = (
                    bounds.0.min(x),
                    bounds.1.min(y),
                    bounds.2.max(x),
                    bounds.3.max(y),
                );
            }
            dict.insert(Name::from(key), value);
            dict.insert(
                Name::from("Rect"),
                Value::Array(Array::from(vec![
                    number(bounds.0),
                    number(bounds.1),
                    number(bounds.2),
                    number(bounds.3),
                ])),
            );
            if let Some(endings) = &self.line_endings {
                if !matches!(subtype, "Line" | "PolyLine") {
                    return Err(AnnotationError::Pdf(format!(
                        "a {subtype} annotation has no line endings"
                    )));
                }
                dict.insert(Name::from("LE"), line_endings_value(endings)?);
            }
        } else {
            let Some(area) = self.rect.filter(|_| self.points.is_none()) else {
                return Err(AnnotationError::Pdf(format!(
                    "a {subtype} annotation takes a rectangle"
                )));
            };
            let (x0, y0, x1, y1) = area;
            if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) {
                return Err(AnnotationError::Pdf(
                    "an annotation rectangle is finite".to_owned(),
                ));
            }
            dict.insert(Name::from("Rect"), annots.to_pdf(area));
        }
        dict.insert(Name::from("F"), Value::Integer(4));
        if let Some(color) = self.color {
            dict.insert(Name::from("C"), rgb(color, "color")?);
        }
        if let Some(color) = self.interior_color {
            dict.insert(Name::from("IC"), rgb(color, "interior color")?);
        }
        if let Some(border) = &self.border {
            dict.insert(
                Name::from("BS"),
                border_style(border, annots.unit, annots.budget)?,
            );
        }
        if let Some(intensity) = self.clouds {
            dict.insert(Name::from("BE"), cloud_effect(subtype, intensity)?);
        }
        if let Some(opacity) = self.opacity {
            if !(opacity.is_finite() && opacity > 0.0 && opacity <= 1.0) {
                return Err(AnnotationError::Pdf(
                    "an annotation opacity is greater than 0 and at most 1".to_owned(),
                ));
            }
            if opacity < 1.0 {
                dict.insert(Name::from("CA"), Value::from(opacity));
            }
        }
        if let Some(mode) = &self.blend_mode {
            if !BLEND_MODES.contains(&mode.as_str()) {
                return Err(AnnotationError::Pdf(format!("unknown blend mode {mode:?}")));
            }
            dict.insert(Name::from("BM"), Value::Name(Name::from(mode.as_str())));
        }
        if let Some(contents) = &self.contents {
            dict.insert(Name::from("Contents"), encode_text(contents));
        }
        if self.open.is_some() && subtype != "Text"
            || self.icon.is_some() && !matches!(subtype, "Text" | "Stamp" | "FileAttachment")
        {
            return Err(AnnotationError::Pdf(format!(
                "an icon and an open state belong to a note, and an icon to a stamp or a file attachment, not a {subtype} annotation"
            )));
        }
        if subtype == "Stamp" {
            let icon = self.icon.as_deref().unwrap_or("Approved");
            if !STAMP_ICONS.contains(&icon) {
                return Err(AnnotationError::Pdf(format!("unknown stamp {icon:?}")));
            }
            dict.insert(Name::from("Name"), Value::Name(Name::from(icon)));
        }
        let text_style = self.text_color.is_some()
            || self.font_size.is_some()
            || self.font_name.is_some()
            || self.align.is_some()
            || self.callout.is_some()
            || self.callout_ending.is_some();
        if text_style && subtype != "FreeText" {
            return Err(AnnotationError::Pdf(format!(
                "text style and callouts belong to free text, not a {subtype} annotation"
            )));
        }
        if subtype == "FileAttachment" {
            let icon = self.icon.as_deref().unwrap_or("PushPin");
            if !FILE_ICONS.contains(&icon) {
                return Err(AnnotationError::Pdf(format!(
                    "unknown file attachment icon {icon:?}"
                )));
            }
            dict.insert(Name::from("Name"), Value::Name(Name::from(icon)));
        }
        if subtype == "Text" {
            let icon = self.icon.as_deref().unwrap_or("Note");
            if !NOTE_ICONS.contains(&icon) {
                return Err(AnnotationError::Pdf(format!("unknown note icon {icon:?}")));
            }
            dict.insert(Name::from("Name"), Value::Name(Name::from(icon)));
            dict.insert(
                Name::from("Open"),
                Value::Boolean(self.open.unwrap_or(false)),
            );
            dict.insert(Name::from("F"), Value::Integer(4 | 8 | 16));
        }
        if self.rotate.is_some() && !matches!(subtype, "FreeText" | "Stamp") {
            return Err(AnnotationError::Pdf(format!(
                "a {subtype} annotation does not turn"
            )));
        }
        let turn = self.rotate.unwrap_or(0);
        if turn % 90 != 0 {
            return Err(AnnotationError::Pdf(
                "an annotation turns by a multiple of 90 degrees".to_owned(),
            ));
        }
        let user_turn = (annots.rotation - turn).rem_euclid(360);
        if matches!(subtype, "FreeText" | "Stamp" | "Caret" | "FileAttachment") && user_turn != 0 {
            dict.insert(Name::from("Rotate"), Value::Integer(user_turn));
        }
        if subtype == "FreeText" {
            self.free_text_entries(annots, &mut dict)?;
        }
        if let Some(created) = &self.created {
            if !created.starts_with("D:") || !created.is_ascii() || created.len() > 64 {
                return Err(AnnotationError::Pdf(
                    "an annotation date is a PDF date string".to_owned(),
                ));
            }
            let date = Value::String(PdfString::literal(created.as_bytes()));
            dict.insert(Name::from("CreationDate"), date.clone());
            dict.insert(Name::from("M"), date);
        }
        Ok(dict)
    }

    #[expect(
        clippy::disallowed_methods,
        reason = "inserts only fixed free-text and callout keys; callout coordinates go through BudgetVec"
    )]
    fn free_text_entries(&self, annots: &PageAnnots<'_>, dict: &mut Dict) -> Result<()> {
        let color = self.text_color.unwrap_or((0.0, 0.0, 0.0));
        rgb(color, "text color")?;
        let size = self.font_size.unwrap_or(11.0);
        if !(size.is_finite() && size > 0.0 && size <= 10_000.0) {
            return Err(AnnotationError::Pdf(
                "a font size is greater than 0 and at most 10000".to_owned(),
            ));
        }
        let pdf_size = size / annots.unit;
        if !(pdf_size.is_finite() && pdf_size > 0.0) {
            return Err(AnnotationError::Pdf(
                "a font size is not representable in user space".to_owned(),
            ));
        }
        let name = self.font_name.as_deref().unwrap_or("Helvetica");
        let resource = DA_FONTS
            .iter()
            .find(|(_, font)| *font == name)
            .map(|(resource, _)| *resource)
            .ok_or_else(|| AnnotationError::Pdf(format!("unknown Standard 14 font {name:?}")))?;
        let da = format!(
            "/{resource} {} Tf {} {} {} rg",
            fmt(pdf_size),
            fmt(color.0),
            fmt(color.1),
            fmt(color.2),
        );
        dict.insert(
            Name::from("DA"),
            Value::String(PdfString::literal(da.into_bytes())),
        );
        let align = self.align.unwrap_or(0);
        if !(0..=2).contains(&align) {
            return Err(AnnotationError::Pdf(
                "free text aligns left (0), centre (1), or right (2)".to_owned(),
            ));
        }
        dict.insert(Name::from("Q"), Value::Integer(align));
        let Some(points) = &self.callout else {
            if self.callout_ending.is_some() {
                return Err(AnnotationError::Pdf(
                    "a callout ending needs a callout line".to_owned(),
                ));
            }
            return Ok(());
        };
        if points.is_too_many()
            || !(2..=3).contains(&points.len())
            || !points.iter().all(|(x, y)| x.is_finite() && y.is_finite())
        {
            return Err(AnnotationError::Pdf(
                "a callout line has 2 or 3 finite points: the point it shows, an optional knee, and the box"
                    .to_owned(),
            ));
        }
        let ending = self.callout_ending.as_deref().unwrap_or("None");
        if !LINE_ENDINGS.contains(&ending) {
            return Err(AnnotationError::Pdf(format!(
                "unknown line ending {ending:?}"
            )));
        }
        let Some((x0, y0, x1, y1)) = self.rect else {
            return Err(AnnotationError::Pdf(
                "a free text needs its rectangle".to_owned(),
            ));
        };
        let (a, b) = to_pdf(annots.crop, annots.rotation, annots.unit, x0, y0);
        let (c, d) = to_pdf(annots.crop, annots.rotation, annots.unit, x1, y1);
        let inner = [a.min(c), b.min(d), a.max(c), b.max(d)];
        let width = self.border.as_ref().map_or(0.0, |border| border.0).max(1.0);
        let pad = (width / 2.0
            + 1.0
            + if ending == "None" {
                0.0
            } else {
                2.0 * ending_size(width)
            })
            / annots.unit;
        let mut outer = inner;
        let mut line = BudgetVec::new(annots.budget);
        line.try_reserve_exact(points.len() * 2)?;
        for &(x, y) in points.iter() {
            let (x, y) = to_pdf(annots.crop, annots.rotation, annots.unit, x, y);
            outer = [
                outer[0].min(x - pad),
                outer[1].min(y - pad),
                outer[2].max(x + pad),
                outer[3].max(y + pad),
            ];
            line.try_extend_from_slice(&[number(x), number(y)])?;
        }
        if !outer.iter().all(|value| value.is_finite()) {
            return Err(AnnotationError::Pdf(
                "a callout line has finite points".to_owned(),
            ));
        }
        dict.insert(Name::from("CL"), Value::Array(Array::from(line.into_vec())));
        dict.insert(Name::from("IT"), Value::Name(Name::from("FreeTextCallout")));
        if ending != "None" {
            dict.insert(Name::from("LE"), Value::Name(Name::from(ending)));
        }
        dict.insert(
            Name::from("Rect"),
            Value::Array(Array::from(Vec::from(outer.map(number)))),
        );
        let differences = [
            inner[0] - outer[0],
            outer[3] - inner[3],
            outer[2] - inner[2],
            inner[1] - outer[1],
        ];
        dict.insert(
            Name::from("RD"),
            Value::Array(Array::from(Vec::from(
                differences.map(|value| number(value.max(0.0))),
            ))),
        );
        Ok(())
    }

    fn check_file(&self, budget: &Budget) -> Result<()> {
        let attachment = self.subtype == "FileAttachment";
        let (Some(data), Some(filename)) = (self.data, &self.filename) else {
            if attachment || self.data.is_some() || self.filename.is_some() {
                return Err(AnnotationError::Pdf(
                    "a file attachment, and only a file attachment, embeds data with a file name"
                        .to_owned(),
                ));
            }
            if self.description.is_some() {
                return Err(AnnotationError::Pdf(
                    "only a file attachment has a description".to_owned(),
                ));
            }
            return Ok(());
        };
        if !attachment {
            return Err(AnnotationError::Pdf(
                "a file attachment, and only a file attachment, embeds data with a file name"
                    .to_owned(),
            ));
        }
        if let Some(limit) = self.max_size
            && data.len() as u64 > limit
        {
            return Err(
                LimitExceeded::over(LimitCode::EmbeddedFileSize, limit, data.len() as u64).into(),
            );
        }
        let texts = filename
            .len()
            .saturating_add(self.description.as_deref().map_or(0, str::len));
        budget.check(LimitCode::EmbeddedFileInputSize, texts as u64)?;
        budget.charge(
            LimitCode::MemorySize,
            (data.len().saturating_mul(2)).saturating_add(texts.saturating_mul(3)) as u64,
        )?;
        Ok(())
    }

    /// Writes the compressed embedded file stream and `/Filespec` dictionary
    /// for a `FileAttachment` annotation.
    #[expect(
        clippy::disallowed_methods,
        reason = "inserts only fixed embedded-file and filespec dictionary keys"
    )]
    pub fn embed(
        &self,
        transaction: &mut Transaction,
        dict: &mut Dict,
        budget: &Budget,
    ) -> Result<()> {
        let (Some(data), Some(filename)) = (self.data, &self.filename) else {
            return Ok(());
        };
        let mut params = Dict::new();
        params.insert(
            Name::from("Size"),
            Value::Integer(i64::try_from(data.len()).unwrap_or(i64::MAX)),
        );
        if let Some(created) = &self.created {
            let date = Value::String(PdfString::literal(created.as_bytes()));
            params.insert(Name::from("CreationDate"), date.clone());
            params.insert(Name::from("ModDate"), date);
        }
        let mut entries = Dict::new();
        entries.insert(Name::from("Type"), Value::Name(Name::from("EmbeddedFile")));
        entries.insert(Name::from("Params"), Value::Dict(params));
        let encoded = deflate(data, 6, usize::MAX, budget)?
            .ok_or_else(|| AnnotationError::Value("the data could not be compressed".to_owned()))?;
        entries.insert(Name::from("Filter"), Value::Name(Name::from("FlateDecode")));
        let stream = Stream::new(entries, Bytes::from(encoded));
        let file = transaction.allocate()?;
        transaction.write(file.number(), &Value::Stream(Box::new(stream)), budget)?;
        let mut files = Dict::new();
        files.insert(Name::from("F"), Value::Reference(file));
        files.insert(Name::from("UF"), Value::Reference(file));
        let mut filespec = Dict::new();
        filespec.insert(Name::from("Type"), Value::Name(Name::from("Filespec")));
        filespec.insert(Name::from("F"), encode_text(filename));
        filespec.insert(Name::from("UF"), encode_text(filename));
        if let Some(description) = &self.description {
            filespec.insert(Name::from("Desc"), encode_text(description));
        }
        filespec.insert(Name::from("EF"), Value::Dict(files));
        let reference = transaction.allocate()?;
        transaction.write(reference.number(), &Value::Dict(filespec), budget)?;
        dict.insert(Name::from("FS"), Value::Reference(reference));
        Ok(())
    }
}

impl PageAnnots<'_> {
    /// Adds `spec` to this page, optionally skipping Standard-14 text checking
    /// and appearance staleness marking when `face` is true.
    pub fn add(
        &self,
        transaction: &mut Transaction,
        spec: &AnnotationSpec<'_>,
        face: bool,
    ) -> Result<(u32, u16)> {
        let budget = self.budget;
        budget.check(LimitCode::AnnotationCount, (self.items.len() + 1) as u64)?;
        if !matches!(
            self.resolved(self.page().dict().get("Annots"))?,
            None | Some(Value::Array(_))
        ) {
            return Err(AnnotationError::Pdf(
                "the page's /Annots is not an array".to_owned(),
            ));
        }
        let mut dict = spec.dictionary(self)?;
        if !face {
            check_text(self, &dict)?;
        }
        if let Some(page) = self.page().object() {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /P key"
            )]
            dict.insert(Name::from("P"), Value::Reference(page));
        }
        spec.embed(transaction, &mut dict, budget)?;
        let reference = transaction.allocate()?;
        transaction.write(reference.number(), &Value::Dict(dict), budget)?;
        let mut items = BudgetVec::new(budget);
        items.try_reserve_exact(self.items.len() + 1)?;
        for item in self.items.iter() {
            items.try_push(copy_for_edit(item, budget)?)?;
        }
        items.try_push(Value::Reference(reference))?;
        self.write_items(transaction, Array::from(items.into_vec()), budget)?;
        if !face {
            transaction.mark(Structure::Appearance, reference.number(), budget)?;
        }
        Ok((reference.number(), reference.generation()))
    }

    /// Applies `patch` to handle `(index, id)` on this page.
    pub fn edit(
        &self,
        transaction: &mut Transaction,
        index: u32,
        id: (u32, u16),
        patch: &AnnotationPatch,
        face: bool,
    ) -> Result<(u32, u16)> {
        let (position, mut dict) = self.find(index, id)?;
        let subtype = self.name(&dict, "Subtype")?.unwrap_or_default();
        patch.apply(self, &mut dict, &String::from_utf8_lossy(&subtype))?;
        if !face {
            check_text(self, &dict)?;
        }
        self.write(transaction, position, !face, dict, |_, _, _, _, _| {
            Ok(false)
        })
    }

    /// Shapes and (when `font` is `Some`) writes the normal appearance of a
    /// free-text annotation in embedded OpenType `face`.
    pub fn draw_face(
        &self,
        transaction: &mut Transaction,
        index: u32,
        id: (u32, u16),
        face: &Face,
        font: Option<(u32, &HashMap<u32, String>)>,
    ) -> Result<Vec<(u32, String)>> {
        let budget = self.budget;
        let (position, mut dict) = self.find(index, id)?;
        let font = match font {
            Some((number, mapped)) => {
                let generation = transaction
                    .version()
                    .get(number, budget)?
                    .map_or(0, |object| object.generation());
                Some(FaceFont {
                    font: Reference::new(number, generation),
                    mapped,
                })
            }
            None => None,
        };
        let writing = font.is_some();
        let mut text = FaceText::new(face, font, budget)?;
        let Some(drawn) = face_appearance(&self.reader(), &dict, &mut text)? else {
            return Err(AnnotationError::Pdf(
                "only a free text with a usable rectangle draws in an embedded face".to_owned(),
            ));
        };
        match drawn.text_fit() {
            TextFit::Unfit => {
                return Err(AnnotationError::Pdf(
                    "the text does not fit in the free text's rectangle; enlarge it or use a smaller size"
                        .to_owned(),
                ));
            }
            TextFit::NoGlyph(character) => {
                return Err(AnnotationError::Pdf(format!(
                    "{} does not contain all glyphs of the text: no glyph for U+{:04X} ({character:?})",
                    face.full_name,
                    u32::from(character)
                )));
            }
            TextFit::NoOutline(character) => {
                return Err(AnnotationError::Pdf(format!(
                    "{} cannot draw U+{:04X} ({character:?}): its glyph is a colour glyph with no outline",
                    face.full_name,
                    u32::from(character)
                )));
            }
            TextFit::Fits | TextFit::NotWinAnsi => {}
        }
        if writing {
            let reference = transaction.allocate()?;
            transaction.write(reference.number(), &Value::from(drawn.form()), budget)?;
            let mut appearances = Dict::new();
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /N key"
            )]
            appearances.insert(Name::from("N"), Value::Reference(reference));
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /AP key"
            )]
            dict.insert(Name::from("AP"), Value::Dict(appearances));
            self.write(transaction, position, false, dict, |_, _, _, _, _| {
                Ok(false)
            })?;
        }
        Ok(text.into_drawn())
    }

    /// Removes all annotations named by `handles` (and any `/Popup` they name
    /// on the same page) from `/Annots`, after promoting all direct
    /// dictionaries in `/Annots` to indirect objects.
    pub fn remove(
        &self,
        transaction: &mut Transaction,
        handles: &[(u32, (u32, u16))],
    ) -> Result<()> {
        let budget = self.budget;
        budget.check(LimitCode::AnnotationCount, handles.len() as u64)?;
        let mut positions = BudgetVec::new(budget);
        for &(index, id) in handles {
            let (position, dict) = self.find(index, id)?;
            budget.poll()?;
            positions.try_push(position)?;
            if let Some(Value::Reference(popup)) = dict.get("Popup")
                && let Some(position) = self
                    .items
                    .iter()
                    .position(|item| matches!(item, Value::Reference(listed) if listed == popup))
            {
                positions.try_push(position)?;
            }
        }
        if positions.is_empty() {
            return Ok(());
        }
        let mut positions = positions.into_vec();
        positions.sort_unstable();
        positions.dedup();
        let mut kept = BudgetVec::new(budget);
        kept.try_reserve_exact(self.items.len())?;
        for (idx, item) in self.items.iter().enumerate() {
            budget.poll()?;
            let removed = positions.binary_search(&idx).is_ok();
            if let Value::Dict(_) = item {
                let entry = copy_for_edit(item, budget)?;
                let reference = transaction.allocate()?;
                transaction.write(reference.number(), &entry, budget)?;
                if !removed {
                    kept.try_push(Value::Reference(reference))?;
                }
            } else if !removed {
                kept.try_push(copy_for_edit(item, budget)?)?;
            }
        }
        self.write_items(transaction, Array::from(kept.into_vec()), budget)
    }
}

/// Adds `spec` to page `(number, generation)`, returning its `(number,
/// generation)` or `None` when the page is gone.
pub fn annotation_add(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    spec: &AnnotationSpec<'_>,
    face: bool,
) -> Result<Option<(u32, u16)>> {
    with_page_edit(
        transaction,
        draft_pages,
        budget,
        number,
        generation,
        |annots, transaction, _| annots.add(transaction, spec, face),
    )
}

/// Applies `patch` to handle `(index, id)` on page `(number, generation)`.
#[expect(
    clippy::too_many_arguments,
    reason = "page identity, annotation handle identity, patch payload, and embedded-face flag"
)]
pub fn annotation_edit(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    patch: &AnnotationPatch,
    face: bool,
) -> Result<Option<(u32, u16)>> {
    with_page_edit(
        transaction,
        draft_pages,
        budget,
        number,
        generation,
        |annots, transaction, _| annots.edit(transaction, index, id, patch, face),
    )
}

/// Draws the free-text annotation `(index, id)` in embedded OpenType `face`.
#[expect(
    clippy::too_many_arguments,
    reason = "page identity, annotation handle identity, embedded face, and optional font mapping"
)]
pub fn annotation_draw_face(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    face: &Face,
    font: Option<(u32, &HashMap<u32, String>)>,
) -> Result<Option<Vec<(u32, String)>>> {
    with_page_edit(
        transaction,
        draft_pages,
        budget,
        number,
        generation,
        |annots, transaction, _| annots.draw_face(transaction, index, id, face, font),
    )
}

/// Removes `handles` (and any popups they name on the page) from `/Annots` of
/// page `(number, generation)`. Returns `false` when the page is gone.
pub fn annotation_remove(
    transaction: &mut Transaction,
    draft_pages: &Memo<PageTree, PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    handles: &[(u32, (u32, u16))],
) -> Result<bool> {
    with_page_edit(
        transaction,
        draft_pages,
        budget,
        number,
        generation,
        |annots, transaction, _| annots.remove(transaction, handles),
    )
    .map(|page| page.is_some())
}
