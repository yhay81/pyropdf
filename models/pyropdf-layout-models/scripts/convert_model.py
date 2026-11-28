# ruff: noqa: INP001, S603 - standalone reproducible model conversion script.
"""Convert PaddlePaddle/PP-DocLayout_plus-L_onnx into the quantized RTen layout artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

SOURCE_REPO = "PaddlePaddle/PP-DocLayout_plus-L_onnx"
SOURCE_COMMIT = "feb74619326f634e0e883218598096a3733ad9f7"
ONNX_URL = f"https://huggingface.co/{SOURCE_REPO}/resolve/{SOURCE_COMMIT}/inference.onnx"
EXPECTED_FP32_ONNX_SHA256 = "77afb2caa74dd13240d087d2eced91d7fcd2caebd16006a0a66162fc8707ff0e"
EXPECTED_RTEN_NAME = "PP-DocLayout_plus-L_int8.rten"

LABELS: list[str] = [
    "paragraph_title",
    "image",
    "text",
    "number",
    "abstract",
    "content",
    "figure_title",
    "formula",
    "table",
    "reference",
    "doc_title",
    "footnote",
    "header",
    "algorithm",
    "footer",
    "seal",
    "chart",
    "formula_number",
    "aside_text",
    "reference_content",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download_onnx(dest: Path) -> None:
    if dest.exists():
        return
    if not ONNX_URL.startswith("https://huggingface.co/"):
        msg = f"unexpected model download URL: {ONNX_URL}"
        raise ValueError(msg)
    req = urllib.request.Request(ONNX_URL)
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - pinned https://huggingface.co URL validated above.
        dest.write_bytes(resp.read())


def _verify_sums(out_dir: Path) -> None:
    sums_path = out_dir / "SHA256SUMS"
    for raw_line in sums_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        expected, name = line.split(None, 1)
        target = out_dir / name
        if not target.is_file():
            msg = f"missing expected artifact: {target}"
            raise FileNotFoundError(msg)
        actual = _sha256(target)
        if actual != expected:
            msg = f"SHA-256 mismatch for {name}: expected {expected}, got {actual}"
            raise ValueError(msg)


def main() -> None:
    """Download, dynamically INT8-quantize, convert to RTen, and emit or verify checksums."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify the generated model against existing SHA256SUMS instead of overwriting metadata.",
    )
    args = parser.parse_args()

    from onnxruntime.quantization import QuantType, quantize_dynamic  # noqa: PLC0415

    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    fp32_onnx = args.work_dir / "inference.onnx"
    _download_onnx(fp32_onnx)
    fp32_sha = _sha256(fp32_onnx)
    if fp32_sha != EXPECTED_FP32_ONNX_SHA256:
        msg = f"upstream FP32 ONNX SHA-256 mismatch: expected {EXPECTED_FP32_ONNX_SHA256}, got {fp32_sha}"
        raise ValueError(msg)

    int8_onnx = args.work_dir / "inference_int8.onnx"
    quantize_dynamic(
        model_input=str(fp32_onnx),
        model_output=str(int8_onnx),
        op_types_to_quantize=["MatMul", "Conv"],
        weight_type=QuantType.QUInt8,
    )

    rten_convert = Path(sys.executable).with_name("rten-convert")
    rten_convert_bin = str(rten_convert) if rten_convert.is_file() else shutil.which("rten-convert")
    if rten_convert_bin is None:
        msg = "rten-convert executable not found in current Python environment"
        raise FileNotFoundError(msg)

    rten_out = args.out_dir / EXPECTED_RTEN_NAME
    subprocess.run(
        [rten_convert_bin, str(int8_onnx), str(rten_out)],
        check=True,
    )

    if args.verify:
        _verify_sums(args.out_dir)
        sys.stdout.write(f"Verified {EXPECTED_RTEN_NAME} against SHA256SUMS\n")
        return

    rten_sha = _sha256(rten_out)
    meta = {
        "name": "PP-DocLayout_plus-L_int8",
        "source_repo": SOURCE_REPO,
        "source_commit": SOURCE_COMMIT,
        "license": "Apache-2.0",
        "quantization": "dynamic_int8_matmul_conv_uint8",
        "rten_convert_version": "0.22.0",
        "input_size": 800,
        "normalize": {"scale": 1.0 / 255.0, "mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]},
        "labels": LABELS,
        "sha256": rten_sha,
        "size_bytes": rten_out.stat().st_size,
    }
    meta_out = args.out_dir / "model.json"
    meta_out.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    meta_sha = _sha256(meta_out)
    (args.out_dir / "SHA256SUMS").write_text(
        f"{rten_sha}  {rten_out.name}\n{meta_sha}  {meta_out.name}\n",
        encoding="utf-8",
    )
    sys.stdout.write(f"Wrote {rten_out.name} ({rten_out.stat().st_size:,} B, sha256={rten_sha[:16]})\n")


if __name__ == "__main__":
    main()
