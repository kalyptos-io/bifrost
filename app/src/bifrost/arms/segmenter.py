"""char-level NER segmenter - the single source of truth for the label scheme + encoding + decode,
shared by serving (app) and training (train imports it back, like normalize.py). torch-free:
segment(raw) -> [(label, start, end)]; onnxruntime loads the trainer artifact (.onnx + meta.json)
as a file, never a code import."""

from __future__ import annotations

import json
import os
import threading
from functools import cache
from pathlib import Path

import numpy as np

# synthetic span labels from train/gen; order fixes the BIO tag ids
LABELS = (
    "street",
    "house_number",
    "house_letter",
    "floor",
    "door",
    "sub_locality",
    "postcode",
    "city",
    "junk",
)
PAD_ID, UNK_ID = 0, 1  # reserved char ids; real chars start at 2

ONNX_NAME, META_NAME = "segmenter.onnx", "segmenter.meta.json"


def build_tags(labels: tuple[str, ...] = LABELS) -> list[str]:
    # O plus B-/I- per label; B = odd id (1+2i), I = even id (2+2i)
    tags = ["O"]
    for label in labels:
        tags += [f"B-{label}", f"I-{label}"]
    return tags


def encode_chars(raw: str, vocab: dict[str, int]) -> list[int]:
    return [vocab.get(ch, UNK_ID) for ch in raw]


def build_vocab(records: list[dict], max_len: int) -> dict[str, int]:
    # deterministic: sorted observed chars, ids from 2 (0=PAD, 1=UNK reserved)
    chars = sorted({ch for r in records for ch in r.get("normalized", r["raw"])[:max_len]})
    return {ch: i + 2 for i, ch in enumerate(chars)}


def bio_ids(n: int, spans: list[dict], labels: tuple[str, ...] = LABELS) -> list[int]:
    # paint per-char BIO tag ids; chars outside any span stay O (delimiters included)
    idx = {label: i for i, label in enumerate(labels)}
    tags = [0] * n
    for s in spans:
        b, e = s["start"], min(s["end"], n)  # clamp to window; partial-visible spans still labeled
        if 0 <= b < e:
            i = idx[s["label"]]
            tags[b] = 1 + 2 * i
            for j in range(b + 1, e):
                tags[j] = 2 + 2 * i
    return tags


def decode_spans(
    tag_ids: list[int], labels: tuple[str, ...] = LABELS
) -> list[tuple[str, int, int]]:
    # greedy BIO -> spans with repair: extend only on I matching the open label, else open new
    out: list[tuple[str, int, int]] = []
    cur: str | None = None
    start = 0
    for i, t in enumerate(tag_ids):
        if t == 0:
            if cur is not None:
                out.append((cur, start, i))
                cur = None
            continue
        label = labels[(t - 1) // 2]
        if t % 2 == 0 and cur == label:  # I- continuing the open span
            continue
        if cur is not None:
            out.append((cur, start, i))
        cur, start = label, i
    if cur is not None:
        out.append((cur, start, len(tag_ids)))
    return out


# ---- linear-chain CRF decode (numpy, torch-free) ----


@cache
def bio_constraints(n_tags: int) -> tuple[np.ndarray, np.ndarray]:
    # i-x tags are even ids >=2; reachable only from their own b-x (id-1) or themselves
    start_ok = np.ones(n_tags, dtype=bool)
    trans_ok = np.ones((n_tags, n_tags), dtype=bool)
    for j in range(2, n_tags, 2):
        start_ok[j] = False  # cannot open a sequence on I-x
        trans_ok[:, j] = False
        trans_ok[j - 1, j] = True  # B-x -> I-x
        trans_ok[j, j] = True  # I-x -> I-x
    return start_ok, trans_ok


def _mask_transitions(start, trans, end, n_tags: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # force bio-illegal starts/transitions to -inf; constant per model, so hoistable out of decode
    s_ok, t_ok = bio_constraints(n_tags)
    return (
        np.where(s_ok, np.asarray(start, dtype=np.float64), -np.inf),
        np.where(t_ok, np.asarray(trans, dtype=np.float64), -np.inf),
        np.asarray(end, dtype=np.float64),
    )


def viterbi(emissions, start, trans, end) -> list[int]:
    # highest-score BIO-legal tag path; illegal transitions forced out with -inf
    emissions = np.asarray(emissions, dtype=np.float64)
    t, k = emissions.shape
    if t == 0:
        return []
    return _viterbi(emissions, *_mask_transitions(start, trans, end, k))


def _viterbi(
    emissions: np.ndarray, start: np.ndarray, trans: np.ndarray, end: np.ndarray
) -> list[int]:
    # dp core over pre-masked float64 matrices; serving masks once at load, viterbi masks per call
    t, k = emissions.shape
    score = start + emissions[0]
    back = np.empty((t, k), dtype=np.int64)
    for i in range(1, t):
        m = score[:, None] + trans  # (from, to)
        back[i] = m.argmax(0)
        score = m.max(0) + emissions[i]
    last = int((score + end).argmax())
    path = [last]
    for i in range(t - 1, 0, -1):
        last = int(back[i, last])
        path.append(last)
    path.reverse()
    return path


# ---- onnxruntime inference (lazy singleton) ----

_state: dict = {"sess": None, "meta": None, "decode": None}
_load_lock = threading.Lock()


def _resolve_dir(artifact_dir: str | Path | None) -> Path:
    if artifact_dir is not None:
        return Path(artifact_dir)
    env = os.environ.get("BIFROST_SEGMENTER")
    if env is None:
        raise FileNotFoundError("[!] no segmenter artifact dir: pass one or set $BIFROST_SEGMENTER")
    return Path(env)


def load(artifact_dir: str | Path | None = None) -> None:
    import onnxruntime as ort

    d = _resolve_dir(artifact_dir)
    onnx_path, meta_path = d / ONNX_NAME, d / META_NAME
    if not (onnx_path.exists() and meta_path.exists()):
        raise FileNotFoundError(f"[!] no segmenter artifact in {d}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    so = ort.SessionOptions()
    so.intra_op_num_threads = 1  # single-query latency, not throughput
    sess = ort.InferenceSession(str(onnx_path), sess_options=so, providers=["CPUExecutionProvider"])
    tr = meta["transitions"]  # shipped artifact is always CRF; mask once at load, reuse per query
    _state["sess"], _state["meta"] = sess, meta
    _state["decode"] = _mask_transitions(tr["start"], tr["trans"], tr["end"], len(tr["start"]))


# pad widths: bounded set keeps ort shape caches warm; 16-floor avoids dynamo 0/1-dim specialization
_BUCKETS = (16, 32, 48, 64, 96, 128, 192, 256)


def _bucket(n_ids: int, max_len: int) -> int:
    for b in _BUCKETS:
        if n_ids <= b <= max_len:
            return b
    return max_len


def segment(raw: str) -> list[tuple[str, int, int]]:
    if _state["sess"] is None:
        with _load_lock:
            if _state["sess"] is None:
                load()
    meta = _state["meta"]
    n = meta["max_len"]
    chars = raw[:n]
    if not chars:
        return []
    ids = encode_chars(chars, meta["vocab"])
    width = _bucket(len(ids), n)  # dynamic graph: pad to a bucket, not always max_len
    padded = ids + [PAD_ID] * (width - len(ids))
    logits = _state["sess"].run(None, {"ids": np.asarray([padded], dtype=np.int64)})[0]
    emissions = np.asarray(logits[0][: len(ids)], dtype=np.float64)
    tag_ids = _viterbi(emissions, *_state["decode"])
    return [(label, int(s), int(e)) for label, s, e in decode_spans(tag_ids, tuple(meta["labels"]))]
