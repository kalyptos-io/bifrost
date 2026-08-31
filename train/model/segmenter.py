"""segmenter CLI: train + export (torch, via trainer) and bench (torch-free, via bifrost).
the label scheme + encoding + decode + inference live in bifrost.arms.segmenter (serving ==
training parity); this module is just the local-only command surface."""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from bifrost.arms.segmenter import META_NAME, ONNX_NAME, load, segment

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data" / "artifacts"
_HARD = Path(__file__).resolve().parent.parent / "data" / "synth" / "hard.jsonl"


def bench(synth_path: str | Path, artifact_dir: str | Path = _DEFAULT_DIR) -> None:
    from eval.run import read_jsonl

    recs = read_jsonl(synth_path)
    if not recs:
        print(f"[!] no records in {synth_path}")
        return
    load(artifact_dir)
    for r in recs[:20]:  # warmup
        segment(r.get("normalized", r["raw"]))
    ms = []
    for r in recs:
        t0 = time.perf_counter()
        segment(r.get("normalized", r["raw"]))
        ms.append((time.perf_counter() - t0) * 1e3)
    ms.sort()

    def pct(p: float) -> float:
        return ms[min(len(ms) - 1, int(p * len(ms)))]

    print(
        f"[i] segment() over {len(ms)} queries (ms): "
        f"p50={pct(0.5):.3f} p95={pct(0.95):.3f} p99={pct(0.99):.3f} max={ms[-1]:.3f}"
    )


def quantize(in_dir: str | Path, out_dir: str | Path) -> None:
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from onnxruntime.quantization.shape_inference import quant_pre_process

    src, out = Path(in_dir) / ONNX_NAME, Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pre, dst = out / "pre.onnx", out / ONNX_NAME
    # skip symbolic shape infer: asserts on rope's dynamic-axis Range; onnx infer + opt still run
    quant_pre_process(str(src), str(pre), skip_symbolic_shape=True)
    quantize_dynamic(str(pre), str(dst), weight_type=QuantType.QInt8)
    pre.unlink()
    meta = Path(in_dir) / META_NAME
    if meta.resolve() != (out / META_NAME).resolve():
        shutil.copyfile(meta, out / META_NAME)  # app needs both files in one dir
    print(f"[+] quantized {src} -> {dst} (int8 dynamic)")


def main() -> None:
    p = argparse.ArgumentParser(description="char-level NER segmenter (train / bench)")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train", help="train + export the onnx artifact")
    t.add_argument("--train", required=True, help="synth train.jsonl")
    t.add_argument("--out", default=str(_DEFAULT_DIR))
    t.add_argument("--epochs", type=int, default=12)
    t.add_argument("--dim", type=int, default=192)
    t.add_argument("--layers", type=int, default=3)
    t.add_argument("--heads", type=int, default=6)
    t.add_argument("--batch", type=int, default=256)
    t.add_argument("--lr", type=float, default=3e-4)
    t.add_argument("--max-len", dest="max_len", type=int, default=256)
    t.add_argument("--val-frac", dest="val_frac", type=float, default=0.05)
    t.add_argument("--seed", type=int, default=1)
    b = sub.add_parser("bench", help="latency of segment() over a corpus")
    b.add_argument("--synth", default=str(_HARD))
    b.add_argument("--dir", default=str(_DEFAULT_DIR), help="artifact dir to time")
    q = sub.add_parser("quantize", help="int8-dynamic quantize an exported onnx")
    q.add_argument("--in", dest="in_dir", default=str(_DEFAULT_DIR))
    q.add_argument("--out", dest="out_dir", default=str(_DEFAULT_DIR))
    a = p.parse_args()

    if a.cmd == "train":
        from .trainer import train

        train(
            a.train,
            a.out,
            dim=a.dim,
            layers=a.layers,
            heads=a.heads,
            epochs=a.epochs,
            batch=a.batch,
            lr=a.lr,
            max_len=a.max_len,
            val_frac=a.val_frac,
            seed=a.seed,
        )
    elif a.cmd == "quantize":
        quantize(a.in_dir, a.out_dir)
    else:
        bench(a.synth, a.dir)


if __name__ == "__main__":
    main()
