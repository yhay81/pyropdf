//! Document layout detection through the pure-Rust RTen runtime.
//!
//! Model data is not embedded. The Python layer locates the optional
//! `pyropdf-layout-models` package (or an explicit `.rten` file) and loads an
//! [`Engine`] over `pyropdf-engine`'s existing `rten_format` runtime.

use std::fmt;
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, LimitCode};
use rten::{Model, NodeId, RunOptions, ThreadPool, ValueOrView};
use rten_tensor::{AsView, Layout as _, NdTensor, NdTensorView};

/// The most inference threads of one layout engine.
pub const MAX_THREADS: usize = 16;
/// The default square input resolution of PP-DocLayout models.
pub const DEFAULT_INPUT_SIZE: usize = 800;
/// The largest supported square input resolution, keeping the NCHW float32
/// tensor bounded independently of untrusted page rasters.
pub const MAX_INPUT_SIZE: usize = 2048;
/// The bytes that one read of a model file adds at most.
const READ_CHUNK: usize = 64 * 1024;
/// How often preprocessing polls cancellation and deadlines (in output rows).
const POLL_ROW_STRIDE: usize = 64;

/// One detected layout region in raster pixel coordinates:
/// `(class_id, confidence, x0, y0, x1, y1, reading_order)`.
pub type LayoutTuple = (i32, f32, f32, f32, f32, f32, Option<i32>);

/// Why loading a layout model or detecting regions failed.
#[derive(Debug)]
pub enum Error {
    /// The model file does not exist.
    NotFound(PathBuf),
    /// The input or detections passed a limit of the budget.
    Limit(LimitExceeded),
    /// Loading or inference failed, or an option is out of range.
    Failed(String),
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotFound(path) => write!(formatter, "no such file: {}", path.display()),
            Self::Limit(refusal) => refusal.fmt(formatter),
            Self::Failed(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for Error {}

impl From<LimitExceeded> for Error {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Limit(refusal)
    }
}

impl From<String> for Error {
    fn from(message: String) -> Self {
        Self::Failed(message)
    }
}

/// How the pixels of a raster are laid out: row by row from the top, gray or
/// RGB, each with or without straight alpha, 8 bits per channel.
#[derive(Clone, Copy, Debug)]
pub struct RasterLayout {
    /// The width in pixels.
    pub width: usize,
    /// The height in pixels.
    pub height: usize,
    /// Whether each pixel holds one gray byte instead of three color bytes.
    pub gray: bool,
    /// Whether each pixel ends with an alpha byte.
    pub alpha: bool,
}

impl RasterLayout {
    const fn channels(self) -> usize {
        (if self.gray { 1 } else { 3 }) + if self.alpha { 1 } else { 0 }
    }
}

/// A loaded RTen document layout model, which runs inference on its own thread
/// pool. Threads may share one engine.
pub struct Engine {
    model: Model,
    thread_pool: Arc<ThreadPool>,
    output_ids: [NodeId; 2],
    output_count: usize,
    input_size: usize,
}

impl fmt::Debug for Engine {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Engine")
            .field("input_size", &self.input_size)
            .field("output_count", &self.output_count)
            .finish_non_exhaustive()
    }
}

/// Reads one `.rten` model file, charging its bytes to `layout_model_size`.
///
/// A file larger than the limit is refused via metadata before reading any
/// bytes, and the read stops one byte past the limit in case the file grows.
fn read_model_file(path: &Path, budget: &Budget) -> Result<Vec<u8>, Error> {
    let io_error = |error: std::io::Error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            Error::NotFound(path.to_path_buf())
        } else {
            Error::Failed(format!(
                "failed to read layout model {}: {error}",
                path.display()
            ))
        }
    };
    let file = File::open(path).map_err(io_error)?;
    let size = file.metadata().map_err(io_error)?.len();
    let remaining = budget.remaining(LimitCode::LayoutModelSize);
    if remaining.is_some_and(|remaining| size > remaining) {
        budget
            .charge(LimitCode::LayoutModelSize, size)
            .map_err(Error::Limit)?;
    }
    let read_limit = remaining.map_or(u64::MAX, |remaining| remaining.saturating_add(1));
    let mut data = Vec::new();
    #[expect(
        clippy::disallowed_methods,
        reason = "the file's size, capped one byte past the rest of layout_model_size"
    )]
    data.try_reserve_exact(usize::try_from(size.min(read_limit)).unwrap_or(0))
        .map_err(|error| format!("failed to allocate layout model buffer: {error}"))?;
    let mut reader = file.take(read_limit);
    #[expect(clippy::disallowed_methods, reason = "one buffer of READ_CHUNK bytes")]
    let mut chunk = vec![0; READ_CHUNK];
    loop {
        let read = match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(read) => read,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(io_error(error)),
        };
        #[expect(
            clippy::disallowed_methods,
            reason = "the read stops one byte past the rest of layout_model_size, which is charged below"
        )]
        data.try_reserve(read)
            .map_err(|error| format!("failed to grow layout model buffer: {error}"))?;
        #[expect(
            clippy::disallowed_methods,
            reason = "the read stops one byte past the rest of layout_model_size, which is charged below"
        )]
        data.extend_from_slice(&chunk[..read]);
    }
    budget
        .charge(LimitCode::LayoutModelSize, data.len() as u64)
        .map_err(Error::Limit)?;
    Ok(data)
}

#[expect(
    clippy::disallowed_methods,
    reason = "fallible, and bounded by layout_detection_count"
)]
fn try_push_detection(values: &mut Vec<LayoutTuple>, value: LayoutTuple) -> Result<(), Error> {
    if values.len() == values.capacity() {
        values
            .try_reserve(1)
            .map_err(|error| format!("failed to grow layout detections: {error}"))?;
    }
    values.push(value);
    Ok(())
}

/// Samples one RGB channel (0..=2) at `(x, y)` in `pixels`, expanding gray and
/// compositing straight alpha over white.
#[inline]
fn sample_channel(pixels: &[u8], layout: RasterLayout, x: usize, y: usize, channel: usize) -> f32 {
    let channels = layout.channels();
    let offset = (y * layout.width + x) * channels;
    let raw = if layout.gray {
        pixels[offset]
    } else {
        pixels[offset + channel]
    };
    if !layout.alpha {
        return f32::from(raw);
    }
    let alpha = pixels[offset + channels - 1];
    if alpha == u8::MAX {
        return f32::from(raw);
    }
    let alpha_f = f32::from(alpha) / 255.0;
    f32::from(raw) * alpha_f + 255.0 * (1.0 - alpha_f)
}

/// Bilinearly resizes a validated raster into a fixed `[1, 3, target, target]`
/// RGB `0..1` NCHW float32 tensor.
fn preprocess_input(
    pixels: &[u8],
    layout: RasterLayout,
    target: usize,
    budget: &Budget,
) -> Result<NdTensor<f32, 4>, Error> {
    let plane = target
        .checked_mul(target)
        .ok_or_else(|| "layout input tensor dimensions overflowed".to_owned())?;
    let total = 3usize
        .checked_mul(plane)
        .ok_or_else(|| "layout input tensor dimensions overflowed".to_owned())?;
    let mut data = Vec::new();
    #[expect(
        clippy::disallowed_methods,
        reason = "fallible allocation of a fixed 1x3xtargetxtarget tensor bounded by MAX_INPUT_SIZE"
    )]
    {
        data.try_reserve_exact(total)
            .map_err(|error| format!("failed to allocate layout input tensor: {error}"))?;
        data.resize(total, 0.0_f32);
    }

    let orig_w = layout.width;
    let orig_h = layout.height;
    let sx = orig_w as f32 / target as f32;
    let sy = orig_h as f32 / target as f32;
    let max_x = (orig_w - 1) as f32;
    let max_y = (orig_h - 1) as f32;

    for y in 0..target {
        if y % POLL_ROW_STRIDE == 0 {
            budget.poll()?;
        }
        let fy = ((y as f32 + 0.5) * sy - 0.5).clamp(0.0, max_y);
        let y0 = fy.floor() as usize;
        let y1 = (y0 + 1).min(orig_h - 1);
        let wy = fy - y0 as f32;
        let inv_wy = 1.0 - wy;
        for x in 0..target {
            let fx = ((x as f32 + 0.5) * sx - 0.5).clamp(0.0, max_x);
            let x0 = fx.floor() as usize;
            let x1 = (x0 + 1).min(orig_w - 1);
            let wx = fx - x0 as f32;
            let inv_wx = 1.0 - wx;
            let idx = y * target + x;
            for c in 0..3 {
                let p00 = sample_channel(pixels, layout, x0, y0, c);
                let p01 = sample_channel(pixels, layout, x1, y0, c);
                let p10 = sample_channel(pixels, layout, x0, y1, c);
                let p11 = sample_channel(pixels, layout, x1, y1, c);
                let top = p00 * inv_wx + p01 * wx;
                let bot = p10 * inv_wx + p11 * wx;
                data[c * plane + idx] = (top * inv_wy + bot * wy) / 255.0;
            }
        }
    }
    Ok(NdTensor::from_data([1, 3, target, target], data))
}

impl Engine {
    /// Loads a `.rten` document layout model at `model_path` with `threads`
    /// inference threads and square input resolution `input_size`.
    ///
    /// The file size is checked and charged against `layout_model_size`.
    pub fn load(
        model_path: &Path,
        threads: usize,
        input_size: usize,
        budget: &Budget,
    ) -> Result<Self, Error> {
        if !(1..=MAX_THREADS).contains(&threads) {
            return Err(Error::Failed(format!(
                "threads must be from 1 through {MAX_THREADS}"
            )));
        }
        if !(1..=MAX_INPUT_SIZE).contains(&input_size) {
            return Err(Error::Failed(format!(
                "input_size must be from 1 through {MAX_INPUT_SIZE}"
            )));
        }
        let model_data = read_model_file(model_path, budget)?;
        let model = Model::load(model_data).map_err(|error| {
            format!(
                "failed to load layout model {}: {error}",
                model_path.display()
            )
        })?;
        let raw_outputs = model.output_ids();
        let Some(&first_output) = raw_outputs.first() else {
            return Err(Error::Failed(format!(
                "layout model {} defines no outputs",
                model_path.display()
            )));
        };
        let (output_ids, output_count) = match raw_outputs.get(1) {
            Some(&second_output) => ([first_output, second_output], 2),
            None => ([first_output, first_output], 1),
        };
        Ok(Self {
            model,
            thread_pool: Arc::new(ThreadPool::with_num_threads(threads)),
            output_ids,
            output_count,
            input_size,
        })
    }

    /// Returns the square input resolution of this engine.
    #[must_use]
    pub const fn input_size(&self) -> usize {
        self.input_size
    }

    /// Detects document layout regions in an opaque or transparent RGBA8 raster,
    /// returning boxes in raster pixel coordinates.
    pub fn detect_rgba(
        &self,
        rgba: &[u8],
        width: usize,
        height: usize,
        min_confidence: f32,
        budget: &Budget,
    ) -> Result<Vec<LayoutTuple>, Error> {
        let layout = RasterLayout {
            width,
            height,
            gray: false,
            alpha: true,
        };
        self.detect(rgba, layout, min_confidence, budget)
    }

    /// Detects document layout regions in a raster described by `layout`,
    /// returning boxes clamped to `[0, width] x [0, height]` in raster pixels.
    ///
    /// Transparent pixels are composited over white. Enforces `layout_raster_size`
    /// (512 MiB RGBA), `layout_detection_count` (1,024 retained regions), and
    /// document cancellation/deadline polling before preprocessing and before
    /// inference.
    pub fn detect(
        &self,
        pixels: &[u8],
        layout: RasterLayout,
        min_confidence: f32,
        budget: &Budget,
    ) -> Result<Vec<LayoutTuple>, Error> {
        if !min_confidence.is_finite() || !(0.0..=1.0).contains(&min_confidence) {
            return Err(Error::Failed(
                "min_confidence must be a finite number from 0 through 1".to_owned(),
            ));
        }
        budget.poll()?;
        let RasterLayout { width, height, .. } = layout;
        let expected_len = width
            .checked_mul(height)
            .and_then(|value| value.checked_mul(layout.channels()))
            .ok_or_else(|| "layout raster dimensions overflow".to_owned())?;
        if pixels.len() != expected_len {
            return Err(Error::Failed(format!(
                "layout raster has {} bytes, expected {expected_len}",
                pixels.len()
            )));
        }
        if width == 0 || height == 0 {
            return Ok(Vec::new());
        }
        let rgba_size = width
            .checked_mul(height)
            .and_then(|size| size.checked_mul(4))
            .ok_or_else(|| LimitExceeded::new(LimitCode::LayoutRasterSize))?;
        budget.check(LimitCode::LayoutRasterSize, rgba_size as u64)?;

        let target = self.input_size;
        let image = preprocess_input(pixels, layout, target, budget)?;
        let scale = NdTensor::<f32, 2>::from_data(
            [1, 2],
            Vec::from([target as f32 / height as f32, target as f32 / width as f32]),
        );
        let im_shape =
            NdTensor::<f32, 2>::from_data([1, 2], Vec::from([target as f32, target as f32]));

        let mut inputs: Vec<(NodeId, ValueOrView<'_>)> = Vec::new();
        let mut has_image = false;
        let mut has_scale = false;
        let mut has_im_shape = false;
        for &id in self.model.input_ids() {
            let name = self
                .model
                .node_info(id)
                .and_then(|n| n.name())
                .unwrap_or("");
            #[expect(
                clippy::disallowed_methods,
                reason = "at most three fixed model input tensors (image, scale_factor, im_shape)"
            )]
            match name {
                "image" if !has_image => {
                    has_image = true;
                    inputs.push((id, image.view().into()));
                }
                "scale_factor" if !has_scale => {
                    has_scale = true;
                    inputs.push((id, scale.view().into()));
                }
                "im_shape" if !has_im_shape => {
                    has_im_shape = true;
                    inputs.push((id, im_shape.view().into()));
                }
                _ => {}
            }
        }

        budget.poll()?;
        let opts = RunOptions::default().with_thread_pool(Some(Arc::clone(&self.thread_pool)));
        let outs = self
            .model
            .run(inputs, &self.output_ids[..self.output_count], Some(opts))
            .map_err(|error| format!("layout inference failed: {error}"))?;
        budget.poll()?;

        let dets_view = outs
            .first()
            .and_then(|value| value.as_tensor_view::<f32>())
            .ok_or_else(|| "layout model output 0 must be an f32 tensor".to_owned())?;
        let shape = dets_view.shape();
        if shape.len() != 2 || shape[1] < 6 {
            return Err(Error::Failed(format!(
                "layout model output 0 must have shape [N, >=6], got {shape:?}"
            )));
        }
        let dets: NdTensorView<'_, f32, 2> = dets_view.nd_view();
        let rows = shape[0];
        let cols = shape[1];
        let valid_n = outs
            .get(1)
            .and_then(|value| value.as_tensor_view::<i32>())
            .and_then(|tensor| tensor.iter().next().copied())
            .map_or(rows, |count| usize::try_from(count).unwrap_or(0).min(rows));

        let max_w = width as f32;
        let max_h = height as f32;
        let mut kept = Vec::new();
        for row in 0..valid_n {
            let raw_cls = dets[[row, 0]];
            let score = dets[[row, 1]];
            if !score.is_finite() || score < min_confidence {
                continue;
            }
            if !raw_cls.is_finite() {
                continue;
            }
            let raw_x0 = dets[[row, 2]];
            let raw_y0 = dets[[row, 3]];
            let raw_x1 = dets[[row, 4]];
            let raw_y1 = dets[[row, 5]];
            if !raw_x0.is_finite()
                || !raw_y0.is_finite()
                || !raw_x1.is_finite()
                || !raw_y1.is_finite()
            {
                continue;
            }
            let x0 = raw_x0.clamp(0.0, max_w);
            let y0 = raw_y0.clamp(0.0, max_h);
            let x1 = raw_x1.clamp(0.0, max_w);
            let y1 = raw_y1.clamp(0.0, max_h);
            if x1 <= x0 || y1 <= y0 {
                continue;
            }
            budget.charge(LimitCode::LayoutDetectionCount, 1)?;
            let reading_order = if cols >= 7 {
                let order = dets[[row, 6]];
                if order.is_finite() && order >= 0.0 && order <= i32::MAX as f32 {
                    Some(order.round() as i32)
                } else {
                    None
                }
            } else {
                None
            };
            let class_id = raw_cls.round().clamp(i32::MIN as f32, i32::MAX as f32) as i32;
            try_push_detection(
                &mut kept,
                (
                    class_id,
                    score.clamp(0.0, 1.0),
                    x0,
                    y0,
                    x1,
                    y1,
                    reading_order,
                ),
            )?;
        }
        Ok(kept)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyropdf_store::budget::CancellationToken;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    /// Minimal zero-dependency back-to-front FlatBuffers writer for building
    /// synthetic `.rten` v1 test models matching `rten-model-file/src/schema.fbs`.
    struct FbBuilder {
        /// Bytes stored in reverse order; final buffer is `buf.reverse()`.
        buf: Vec<u8>,
    }

    impl FbBuilder {
        fn new() -> Self {
            Self { buf: Vec::new() }
        }

        fn pad_for(&mut self, payload_len: usize, alignment: usize) {
            let rem = (self.buf.len() + payload_len) % alignment;
            if rem != 0 {
                self.buf.resize(self.buf.len() + (alignment - rem), 0);
            }
        }

        fn push_rev(&mut self, bytes: &[u8]) {
            for &b in bytes.iter().rev() {
                self.buf.push(b);
            }
        }

        fn write_u8(&mut self, v: u8) -> usize {
            self.buf.push(v);
            self.buf.len()
        }

        fn write_i32(&mut self, v: i32) -> usize {
            self.pad_for(4, 4);
            self.push_rev(&v.to_le_bytes());
            self.buf.len()
        }

        fn write_string(&mut self, s: &str) -> usize {
            let total = 4 + s.len() + 1;
            self.pad_for(total, 4);
            self.buf.push(0);
            self.push_rev(s.as_bytes());
            self.push_rev(&(s.len() as u32).to_le_bytes());
            self.buf.len()
        }

        fn write_vec_u32(&mut self, items: &[u32]) -> usize {
            self.pad_for(4 * (1 + items.len()), 4);
            for &v in items.iter().rev() {
                self.push_rev(&v.to_le_bytes());
            }
            self.push_rev(&(items.len() as u32).to_le_bytes());
            self.buf.len()
        }

        fn write_vec_i32(&mut self, items: &[i32]) -> usize {
            self.pad_for(4 * (1 + items.len()), 4);
            for &v in items.iter().rev() {
                self.push_rev(&v.to_le_bytes());
            }
            self.push_rev(&(items.len() as u32).to_le_bytes());
            self.buf.len()
        }

        fn write_vec_f32(&mut self, items: &[f32]) -> usize {
            self.pad_for(4 * (1 + items.len()), 4);
            for &v in items.iter().rev() {
                self.push_rev(&v.to_le_bytes());
            }
            self.push_rev(&(items.len() as u32).to_le_bytes());
            self.buf.len()
        }

        fn write_vec_offsets(&mut self, targets: &[usize]) -> usize {
            self.pad_for(4 * (1 + targets.len()), 4);
            for &target in targets.iter().rev() {
                let slot_pos = self.buf.len() + 4;
                let rel = (slot_pos - target) as u32;
                self.push_rev(&rel.to_le_bytes());
            }
            self.push_rev(&(targets.len() as u32).to_le_bytes());
            self.buf.len()
        }

        /// Writes a table whose fields are specified as `(field_id, FieldVal)`.
        fn write_table(&mut self, num_fields: usize, fields: &[(usize, FieldVal)]) -> usize {
            let table_bottom = self.buf.len();
            let mut field_positions = vec![0usize; num_fields];
            for &(field_id, val) in fields.iter().rev() {
                let field_pos = match val {
                    FieldVal::U8(v) => self.write_u8(v),
                    FieldVal::I32(v) => self.write_i32(v),
                    FieldVal::Offset(target) => {
                        self.pad_for(4, 4);
                        let slot_pos = self.buf.len() + 4;
                        let rel = (slot_pos - target) as u32;
                        self.push_rev(&rel.to_le_bytes());
                        self.buf.len()
                    }
                };
                field_positions[field_id] = field_pos;
            }
            self.pad_for(4, 4);
            let soffset_idx = self.buf.len();
            self.buf.extend_from_slice(&[0u8; 4]);
            let table_pos = self.buf.len();
            let body_size = (table_pos - table_bottom) as u16;

            let vtable_size = (4 + 2 * num_fields) as u16;
            self.pad_for(usize::from(vtable_size), 2);
            for &field_pos in field_positions.iter().rev() {
                let rel = if field_pos == 0 {
                    0u16
                } else {
                    (table_pos - field_pos) as u16
                };
                self.push_rev(&rel.to_le_bytes());
            }
            self.push_rev(&body_size.to_le_bytes());
            self.push_rev(&vtable_size.to_le_bytes());
            let vtable_pos = self.buf.len();

            let soffset = (vtable_pos - table_pos) as i32;
            let bytes = soffset.to_le_bytes();
            self.buf[soffset_idx] = bytes[3];
            self.buf[soffset_idx + 1] = bytes[2];
            self.buf[soffset_idx + 2] = bytes[1];
            self.buf[soffset_idx + 3] = bytes[0];
            table_pos
        }

        fn finish(mut self, root_pos: usize) -> Vec<u8> {
            self.pad_for(8, 8);
            self.push_rev(b"RTEN");
            let slot_pos = self.buf.len() + 4;
            let root_uoff = (slot_pos - root_pos) as u32;
            self.push_rev(&root_uoff.to_le_bytes());
            self.buf.reverse();
            self.buf
        }
    }

    #[derive(Clone, Copy)]
    enum FieldVal {
        U8(u8),
        I32(i32),
        Offset(usize),
    }

    /// Builds a valid `.rten` v1 model with inputs `"im_shape"`, `"image"`, `"scale_factor"`
    /// and constant outputs: `dets` (`[rows, cols]` f32) and optional `count` (`[1]` i32).
    fn build_synthetic_rten(rows: usize, cols: usize, dets: &[f32], count: Option<i32>) -> Vec<u8> {
        assert_eq!(dets.len(), rows * cols);
        let mut fb = FbBuilder::new();
        let mut node_offsets = Vec::new();

        // Nodes 0, 1, 2: input ValueNodes ("im_shape", "image", "scale_factor").
        // NodeKind: 1=OperatorNode, 2=ConstantNode, 3=ValueNode.
        for name in ["im_shape", "image", "scale_factor"] {
            let name_off = fb.write_string(name);
            let value_node = fb.write_table(2, &[]);
            let node = fb.write_table(
                3,
                &[
                    (0, FieldVal::Offset(name_off)),
                    (1, FieldVal::U8(3)),
                    (2, FieldVal::Offset(value_node)),
                ],
            );
            node_offsets.push(node);
        }

        // Node 3: ConstantNode for detections [rows, cols] f32.
        // ConstantData union: 1=FloatData, 2=Int32Data.
        let det_name = fb.write_string("dets");
        let det_shape = fb.write_vec_u32(&[rows as u32, cols as u32]);
        let det_vec = fb.write_vec_f32(dets);
        let float_data = fb.write_table(1, &[(0, FieldVal::Offset(det_vec))]);
        let det_const = fb.write_table(
            5,
            &[
                (0, FieldVal::Offset(det_shape)),
                (1, FieldVal::U8(1)),
                (2, FieldVal::Offset(float_data)),
            ],
        );
        let det_node = fb.write_table(
            3,
            &[
                (0, FieldVal::Offset(det_name)),
                (1, FieldVal::U8(2)),
                (2, FieldVal::Offset(det_const)),
            ],
        );
        node_offsets.push(det_node);

        let mut output_ids = vec![3u32];
        if let Some(valid_n) = count {
            let cnt_name = fb.write_string("bbox_num");
            let cnt_shape = fb.write_vec_u32(&[1]);
            let cnt_vec = fb.write_vec_i32(&[valid_n]);
            let int_data = fb.write_table(1, &[(0, FieldVal::Offset(cnt_vec))]);
            let cnt_const = fb.write_table(
                5,
                &[
                    (0, FieldVal::Offset(cnt_shape)),
                    (1, FieldVal::U8(2)),
                    (2, FieldVal::Offset(int_data)),
                ],
            );
            let cnt_node = fb.write_table(
                3,
                &[
                    (0, FieldVal::Offset(cnt_name)),
                    (1, FieldVal::U8(2)),
                    (2, FieldVal::Offset(cnt_const)),
                ],
            );
            node_offsets.push(cnt_node);
            output_ids.push(4);
        }

        let nodes_vec = fb.write_vec_offsets(&node_offsets);
        let inputs_vec = fb.write_vec_u32(&[0, 1, 2]);
        let outputs_vec = fb.write_vec_u32(&output_ids);
        let graph = fb.write_table(
            4,
            &[
                (0, FieldVal::Offset(nodes_vec)),
                (1, FieldVal::Offset(inputs_vec)),
                (2, FieldVal::Offset(outputs_vec)),
            ],
        );
        let model = fb.write_table(3, &[(0, FieldVal::I32(1)), (1, FieldVal::Offset(graph))]);
        fb.finish(model)
    }

    fn write_temp_model(name: &str, bytes: &[u8]) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "pyropdf-layout-test-{name}-{}-{nanos}.rten",
            std::process::id()
        ));
        std::fs::write(&path, bytes).unwrap();
        path
    }

    fn limit_code(error: Error) -> LimitCode {
        match error {
            Error::Limit(refusal) => refusal.code(),
            other => panic!("expected Limit error, got {other}"),
        }
    }

    #[test]
    fn loads_synthetic_rten_and_detects_6_and_7_column_outputs() {
        // 7-column output (PP-DocLayoutV3 style) with valid_n = 2 capping row 2,
        // box clamping on row 0, and confidence filtering on row 1.
        let dets = [
            // row 0: class 10 (doc_title), conf 0.92, box extends past edges, reading_order 1
            10.0, 0.92, -5.0, 10.0, 120.0, 90.0, 1.0, // row 1: below min_confidence
            2.0, 0.30, 10.0, 10.0, 50.0, 50.0, 2.0,
            // row 2: high conf, but excluded by valid_n = 2
            8.0, 0.99, 5.0, 5.0, 60.0, 60.0, 0.0,
        ];
        let model_bytes = build_synthetic_rten(3, 7, &dets, Some(2));
        let path = write_temp_model("v3", &model_bytes);
        let budget = Budget::default();
        let engine = Engine::load(&path, 1, 32, &budget).unwrap();
        let _ = std::fs::remove_file(&path);

        let rgba = vec![200u8; 100 * 80 * 4];
        let regions = engine.detect_rgba(&rgba, 100, 80, 0.5, &budget).unwrap();
        assert_eq!(regions, vec![(10, 0.92, 0.0, 10.0, 100.0, 80.0, Some(1))]);

        // 6-column output (PP-DocLayout_plus-L style): reading_order is None.
        let dets6 = [0.0, 0.85, 10.0, 20.0, 40.0, 50.0];
        let path6 = write_temp_model("plus-l", &build_synthetic_rten(1, 6, &dets6, None));
        let engine6 = Engine::load(&path6, 2, 32, &budget).unwrap();
        let _ = std::fs::remove_file(&path6);
        let gray = vec![128u8; 64 * 64];
        let regions6 = engine6
            .detect(
                &gray,
                RasterLayout {
                    width: 64,
                    height: 64,
                    gray: true,
                    alpha: false,
                },
                0.5,
                &budget,
            )
            .unwrap();
        assert_eq!(regions6, vec![(0, 0.85, 10.0, 20.0, 40.0, 50.0, None)]);
    }

    #[test]
    fn enforces_max_model_size_before_reading() {
        let path = write_temp_model("oversized", &[0u8; 128]);
        let budget = Budget::default().with_limit(LimitCode::LayoutModelSize, Some(64));
        let err = Engine::load(&path, 1, 32, &budget).unwrap_err();
        let _ = std::fs::remove_file(&path);
        assert_eq!(limit_code(err), LimitCode::LayoutModelSize);
    }

    #[test]
    fn enforces_raster_size_detection_count_cancellation_and_deadline() {
        // Build a model emitting 1,025 valid detections (> 1,024 default cap).
        let mut dets = Vec::with_capacity(1_025 * 6);
        for i in 0..1_025 {
            dets.extend_from_slice(&[2.0, 0.9, 1.0, 1.0, 10.0 + (i % 10) as f32, 10.0]);
        }
        let path = write_temp_model("many", &build_synthetic_rten(1_025, 6, &dets, None));
        let engine = Engine::load(&path, 1, 16, &Budget::default()).unwrap();
        let _ = std::fs::remove_file(&path);

        let rgba = vec![255u8; 32 * 32 * 4];
        let err = engine
            .detect_rgba(&rgba, 32, 32, 0.5, &Budget::default())
            .unwrap_err();
        assert_eq!(limit_code(err), LimitCode::LayoutDetectionCount);

        // Raster size limit.
        let small_raster_budget =
            Budget::default().with_limit(LimitCode::LayoutRasterSize, Some(100));
        let err = engine
            .detect_rgba(&rgba, 32, 32, 0.5, &small_raster_budget)
            .unwrap_err();
        assert_eq!(limit_code(err), LimitCode::LayoutRasterSize);

        // Cancellation token.
        let token = CancellationToken::new();
        token.cancel();
        let cancel_budget = Budget::default().with_cancellation(token);
        let err = engine
            .detect_rgba(&rgba, 32, 32, 0.5, &cancel_budget)
            .unwrap_err();
        assert_eq!(limit_code(err), LimitCode::Cancelled);

        // Expired deadline.
        let deadline_budget = Budget::default().with_timeout(Duration::ZERO);
        let err = engine
            .detect_rgba(&rgba, 32, 32, 0.5, &deadline_budget)
            .unwrap_err();
        assert_eq!(limit_code(err), LimitCode::Deadline);
    }

    #[test]
    fn preprocess_composites_alpha_over_white_and_expands_gray() {
        let budget = Budget::default();
        // 1x1 fully transparent black pixel -> composited over white = 1.0 in all RGB channels.
        let transparent = [0u8, 0, 0, 0];
        let tensor = preprocess_input(
            &transparent,
            RasterLayout {
                width: 1,
                height: 1,
                gray: false,
                alpha: true,
            },
            4,
            &budget,
        )
        .unwrap();
        assert_eq!(tensor.shape(), [1, 3, 4, 4]);
        assert!(tensor.iter().all(|&v| (v - 1.0).abs() < 1e-6));

        // 1x1 50% gray with 50% alpha -> (0*0.5 + 255*0.5)/255 = 0.5.
        let semi = [0u8, 128];
        let gray_tensor = preprocess_input(
            &semi,
            RasterLayout {
                width: 1,
                height: 1,
                gray: true,
                alpha: true,
            },
            2,
            &budget,
        )
        .unwrap();
        let expected = 1.0 - (128.0 / 255.0);
        assert!(gray_tensor.iter().all(|&v| (v - expected).abs() < 1e-4));
    }

    #[test]
    fn validates_threads_input_size_and_confidence() {
        let path = Path::new("/nonexistent/model.rten");
        assert!(matches!(
            Engine::load(path, 0, 800, &Budget::default()),
            Err(Error::Failed(_))
        ));
        assert!(matches!(
            Engine::load(path, 17, 800, &Budget::default()),
            Err(Error::Failed(_))
        ));
        assert!(matches!(
            Engine::load(path, 1, 0, &Budget::default()),
            Err(Error::Failed(_))
        ));
        assert!(matches!(
            Engine::load(path, 1, 4096, &Budget::default()),
            Err(Error::Failed(_))
        ));
        assert!(matches!(
            Engine::load(path, 1, 800, &Budget::default()),
            Err(Error::NotFound(_))
        ));
    }
}
