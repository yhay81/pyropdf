//! Standalone RTen 0.28.0 (`rten_format` only) evaluator for Apache-2.0 PP-DocLayout models.
//!
//! Usage:
//!   cargo run --release --locked --manifest-path evidence/layout-model/Cargo.toml -- \
//!     MODEL.rten ARCH(s|plus_l|v3) THREADS OUT.json PAGE1.png [PAGE2.png ...]
//!
//! Mirrors `crates/pyropdf-engine/Cargo.toml` (`rten` with `default-features = false,
//! features = ["rten_format"]`, zero extra crates or operators).

use std::fmt::Write as _;
use std::fs::File;
use std::io::BufReader;
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use rten::{Model, NodeId, RunOptions, ThreadPool, ValueOrView};
use rten_tensor::{AsView, Layout, NdTensor};

fn load_rgb_png(path: &Path) -> Result<(usize, usize, Vec<u8>), String> {
    let file = File::open(path).map_err(|e| format!("open {}: {e}", path.display()))?;
    let decoder = png::Decoder::new(BufReader::new(file));
    let mut reader = decoder
        .read_info()
        .map_err(|e| format!("read png info {}: {e}", path.display()))?;
    let buf_len = reader
        .output_buffer_size()
        .ok_or_else(|| format!("overflow computing png buffer size for {}", path.display()))?;
    let mut buf = Vec::new();
    buf.try_reserve_exact(buf_len)
        .map_err(|e| format!("allocate png buffer: {e}"))?;
    buf.resize(buf_len, 0);
    let info = reader
        .next_frame(&mut buf)
        .map_err(|e| format!("decode png {}: {e}", path.display()))?;
    let bytes = &buf[..info.buffer_size()];
    let (w, h) = (info.width as usize, info.height as usize);
    let rgb_len = w
        .checked_mul(h)
        .and_then(|px| px.checked_mul(3))
        .ok_or_else(|| format!("image dimensions overflow: {w}x{h}"))?;
    let mut rgb = Vec::new();
    rgb.try_reserve_exact(rgb_len)
        .map_err(|e| format!("allocate rgb buffer: {e}"))?;
    match info.color_type {
        png::ColorType::Rgb => rgb.extend_from_slice(bytes),
        png::ColorType::Rgba => {
            for px in bytes.as_chunks::<4>().0 {
                rgb.extend_from_slice(&px[..3]);
            }
        }
        png::ColorType::Grayscale => {
            for &g in bytes {
                rgb.extend_from_slice(&[g, g, g]);
            }
        }
        other => return Err(format!("unsupported png color type: {other:?}")),
    }
    Ok((w, h, rgb))
}

/// Bilinear resize RGB -> NCHW float32 tensor with optional ImageNet normalization.
fn preprocess(
    rgb: &[u8],
    orig_w: usize,
    orig_h: usize,
    target: usize,
    imagenet: bool,
) -> Result<NdTensor<f32, 4>, String> {
    let mean = if imagenet {
        [0.485_f32, 0.456, 0.406]
    } else {
        [0.0; 3]
    };
    let std = if imagenet {
        [0.229_f32, 0.224, 0.225]
    } else {
        [1.0; 3]
    };
    let plane = target * target;
    let total = 3 * plane;
    let mut data = Vec::new();
    data.try_reserve_exact(total)
        .map_err(|e| format!("allocate input tensor: {e}"))?;
    data.resize(total, 0.0_f32);
    let sx = orig_w as f32 / target as f32;
    let sy = orig_h as f32 / target as f32;
    let max_x = (orig_w - 1) as f32;
    let max_y = (orig_h - 1) as f32;
    for y in 0..target {
        let fy = ((y as f32 + 0.5) * sy - 0.5).clamp(0.0, max_y);
        let y0 = fy.floor() as usize;
        let y1 = (y0 + 1).min(orig_h - 1);
        let wy = fy - y0 as f32;
        for x in 0..target {
            let fx = ((x as f32 + 0.5) * sx - 0.5).clamp(0.0, max_x);
            let x0 = fx.floor() as usize;
            let x1 = (x0 + 1).min(orig_w - 1);
            let wx = fx - x0 as f32;
            let idx = y * target + x;
            let p00 = (y0 * orig_w + x0) * 3;
            let p01 = (y0 * orig_w + x1) * 3;
            let p10 = (y1 * orig_w + x0) * 3;
            let p11 = (y1 * orig_w + x1) * 3;
            for c in 0..3 {
                let top = f32::from(rgb[p00 + c]) * (1.0 - wx) + f32::from(rgb[p01 + c]) * wx;
                let bot = f32::from(rgb[p10 + c]) * (1.0 - wx) + f32::from(rgb[p11 + c]) * wx;
                let val = (top * (1.0 - wy) + bot * wy) / 255.0;
                data[c * plane + idx] = (val - mean[c]) / std[c];
            }
        }
    }
    Ok(NdTensor::from_data([1, 3, target, target], data))
}

fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c => out.push(c),
        }
    }
    out
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 6 {
        return Err(
            "usage: pyropdf-layout-model-spike MODEL.rten ARCH(s|plus_l|v3) THREADS OUT.json PAGE.png..."
                .to_owned(),
        );
    }
    let model_path = &args[1];
    let arch = &args[2];
    let threads: usize = args[3]
        .parse()
        .map_err(|e| format!("invalid thread count {:?}: {e}", args[3]))?;
    let out_json = &args[4];
    let pages = &args[5..];

    let (target, imagenet, has_im_shape) = match arch.as_str() {
        "s" => (480usize, true, false),
        "plus_l" | "v3" => (800usize, false, true),
        other => return Err(format!("unknown arch: {other}")),
    };

    let t_load = Instant::now();
    let model = Model::load_file(model_path).map_err(|e| format!("load {model_path}: {e}"))?;
    let load_ms = t_load.elapsed().as_secs_f64() * 1e3;

    let pool = Arc::new(ThreadPool::with_num_threads(threads));
    let out_ids: Vec<NodeId> = model.output_ids().to_vec();

    let mut page_entries = Vec::new();
    for page_path in pages {
        let p = Path::new(page_path);
        let stem = p
            .file_stem()
            .ok_or_else(|| format!("missing file stem for {page_path}"))?
            .to_string_lossy()
            .into_owned();
        let (orig_w, orig_h, rgb) = load_rgb_png(p)?;

        let t_pre = Instant::now();
        let image = preprocess(&rgb, orig_w, orig_h, target, imagenet)?;
        let pre_ms = t_pre.elapsed().as_secs_f64() * 1e3;

        let scale = NdTensor::<f32, 2>::from_data(
            [1, 2],
            vec![target as f32 / orig_h as f32, target as f32 / orig_w as f32],
        );
        let im_shape = NdTensor::<f32, 2>::from_data([1, 2], vec![target as f32, target as f32]);

        let mut inputs: Vec<(NodeId, ValueOrView<'_>)> = Vec::new();
        for &id in model.input_ids() {
            let name = model.node_info(id).and_then(|n| n.name()).unwrap_or("");
            match name {
                "image" => inputs.push((id, image.view().into())),
                "scale_factor" => inputs.push((id, scale.view().into())),
                "im_shape" if has_im_shape => inputs.push((id, im_shape.view().into())),
                _ => {}
            }
        }
        let opts = RunOptions::default().with_thread_pool(Some(Arc::clone(&pool)));
        let t_inf = Instant::now();
        let outs = model
            .run(inputs, &out_ids, Some(opts))
            .map_err(|e| format!("infer {stem}: {e}"))?;
        let inf_ms = t_inf.elapsed().as_secs_f64() * 1e3;

        let det_tensor = outs[0]
            .as_tensor_view::<f32>()
            .ok_or_else(|| format!("output 0 is not f32 for {stem}"))?;
        let rows = det_tensor.shape()[0];
        let cols = det_tensor.shape()[1];
        let valid_n = if let Some(count_val) = outs.get(1)
            && let Some(n_i32) = count_val.as_tensor_view::<i32>()
        {
            (*n_i32.iter().next().unwrap_or(&(rows as i32)) as usize).min(rows)
        } else {
            rows
        };
        let flat: Vec<f32> = det_tensor.iter().copied().collect();
        let mut dets_json = Vec::new();
        for r in 0..valid_n {
            let row = &flat[r * cols..(r + 1) * cols];
            let cls = row[0] as i32;
            let score = row[1];
            if score < 0.3 {
                continue;
            }
            let mut item = String::new();
            let _ = write!(
                item,
                "{{\"cls\": {cls}, \"score\": {score:.4}, \"bbox_px\": [{:.2}, {:.2}, {:.2}, {:.2}]",
                row[2], row[3], row[4], row[5]
            );
            if cols >= 7 {
                let _ = write!(item, ", \"order\": {:.1}", row[6]);
            }
            item.push('}');
            dets_json.push(item);
        }
        page_entries.push(format!(
            "    \"{}\": {{\"img_w\": {}, \"img_h\": {}, \"pre_ms\": {:.2}, \"infer_ms\": {:.2}, \"detections\": [{}]}}",
            json_escape(&stem),
            orig_w,
            orig_h,
            pre_ms,
            inf_ms,
            dets_json.join(", ")
        ));
    }

    let json = format!(
        "{{\n  \"model\": \"{}\",\n  \"arch\": \"{}\",\n  \"threads\": {},\n  \"load_ms\": {:.2},\n  \"pages\": {{\n{}\n  }}\n}}\n",
        json_escape(model_path),
        json_escape(arch),
        threads,
        load_ms,
        page_entries.join(",\n")
    );
    std::fs::write(out_json, json).map_err(|e| format!("write {out_json}: {e}"))?;
    eprintln!(
        "DONE {arch} ({threads}t) load={load_ms:.1}ms pages={}",
        pages.len()
    );
    Ok(())
}

fn main() {
    if let Err(err) = run() {
        eprintln!("error: {err}");
        std::process::exit(1);
    }
}
