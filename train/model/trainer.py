"""train the RoPE char tagger in torch, export to onnx; local-only, never in the serving image.
fixed-length graph self-masks from ids==PAD; the onnx input is one `ids` padded to max_len."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import numpy as np
import torch
from bifrost.arms.segmenter import (
    LABELS,
    META_NAME,
    ONNX_NAME,
    PAD_ID,
    bio_constraints,
    bio_ids,
    build_tags,
    build_vocab,
    decode_spans,
    encode_chars,
    viterbi,
)
from eval.run import _prf, read_jsonl
from torch import nn
from torch.export import Dim
from torch.nn import functional as F


def _rope_tables(t: int, hd: int, theta: float, device, dtype):
    inv = 1.0 / (theta ** (torch.arange(0, hd, 2, device=device, dtype=torch.float32) / hd))
    freqs = torch.outer(torch.arange(t, device=device, dtype=torch.float32), inv)
    emb = torch.cat((freqs, freqs), dim=-1)  # (t, hd)
    return emb.cos().to(dtype)[None, None], emb.sin().to(dtype)[None, None]


def _rotate_half(x):
    h = x.shape[-1] // 2
    return torch.cat((-x[..., h:], x[..., :h]), dim=-1)


def _rope(x, cos, sin):
    return x * cos + _rotate_half(x) * sin


class _Attn(nn.Module):
    def __init__(self, dim: int, heads: int):
        super().__init__()
        self.h, self.hd = heads, dim // heads
        self.qkv = nn.Linear(dim, 3 * dim)
        self.proj = nn.Linear(dim, dim)

    def forward(self, x, cos, sin, bias):
        b, t, _ = x.shape
        qkv = self.qkv(x).view(b, t, 3, self.h, self.hd).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]  # (b, h, t, hd)
        q, k = _rope(q, cos, sin), _rope(k, cos, sin)
        att = q @ k.transpose(-2, -1) / (self.hd**0.5)
        att = (att + bias).softmax(dim=-1)
        out = (att @ v).transpose(1, 2).reshape(b, t, -1)
        return self.proj(out)


class _Block(nn.Module):
    def __init__(self, dim: int, heads: int, mult: int, p: float):
        super().__init__()
        self.ln1, self.ln2 = nn.LayerNorm(dim), nn.LayerNorm(dim)
        self.attn = _Attn(dim, heads)
        self.fc1, self.fc2 = nn.Linear(dim, mult * dim), nn.Linear(mult * dim, dim)
        self.drop = nn.Dropout(p)

    def forward(self, x, cos, sin, bias):
        x = x + self.drop(self.attn(self.ln1(x), cos, sin, bias))
        return x + self.drop(self.fc2(F.gelu(self.fc1(self.ln2(x)))))


class SegModel(nn.Module):
    def __init__(
        self, vocab_size, n_tags, dim=192, layers=3, heads=6, mult=4, p=0.1, theta=10000.0
    ):
        super().__init__()
        self.emb = nn.Embedding(vocab_size, dim, padding_idx=PAD_ID)
        self.blocks = nn.ModuleList([_Block(dim, heads, mult, p) for _ in range(layers)])
        self.lnf = nn.LayerNorm(dim)
        self.head = nn.Linear(dim, n_tags)
        self.hd, self.theta = dim // heads, theta
        # crf: emissions stay in forward()/onnx; transitions train jointly, decode via numpy viterbi
        self.start_transitions = nn.Parameter(torch.zeros(n_tags))
        self.transitions = nn.Parameter(torch.zeros(n_tags, n_tags))
        self.end_transitions = nn.Parameter(torch.zeros(n_tags))
        s_ok, t_ok = bio_constraints(n_tags)  # cached; copy so buffers don't alias the singleton
        self.register_buffer("_start_ok", torch.from_numpy(s_ok.copy()))
        self.register_buffer("_trans_ok", torch.from_numpy(t_ok.copy()))

    def forward(self, ids):
        mask = ids != PAD_ID  # (b, t)
        x = self.emb(ids)
        b, t, _ = x.shape
        cos, sin = _rope_tables(t, self.hd, self.theta, x.device, x.dtype)
        bias = torch.zeros(b, 1, 1, t, dtype=x.dtype, device=x.device)
        bias = bias.masked_fill(~mask[:, None, None, :], float("-inf"))  # ignore pad keys
        for blk in self.blocks:
            x = blk(x, cos, sin, bias)
        return self.head(self.lnf(x))

    def _masked(self):
        # -1e4 (not -inf) keeps autograd nan-free; illegal paths still vanish in the sum
        start = self.start_transitions.masked_fill(~self._start_ok, -1e4)
        trans = self.transitions.masked_fill(~self._trans_ok, -1e4)
        return start, trans, self.end_transitions

    def _path_score(self, emissions, tags, mask, start, trans, end):
        b, t, _ = emissions.shape
        m = mask.to(emissions.dtype)
        score = start[tags[:, 0]] + emissions[:, 0].gather(1, tags[:, :1]).squeeze(1)
        for i in range(1, t):
            emit = emissions[:, i].gather(1, tags[:, i : i + 1]).squeeze(1)
            score = score + (trans[tags[:, i - 1], tags[:, i]] + emit) * m[:, i]
        last_tag = tags.gather(1, (mask.sum(1) - 1).unsqueeze(1)).squeeze(1)
        return score + end[last_tag]

    def _logz(self, emissions, mask, start, trans, end):
        _, t, _ = emissions.shape
        alpha = start.unsqueeze(0) + emissions[:, 0]
        for i in range(1, t):
            nxt = torch.logsumexp(
                alpha.unsqueeze(2) + trans.unsqueeze(0) + emissions[:, i].unsqueeze(1), dim=1
            )
            alpha = torch.where(mask[:, i].unsqueeze(1), nxt, alpha)  # freeze past last real tok
        return torch.logsumexp(alpha + end.unsqueeze(0), dim=1)

    def nll(self, emissions, tags, mask):
        start, trans, end = self._masked()
        expected = self._path_score(emissions, tags, mask, start, trans, end)
        return (self._logz(emissions, mask, start, trans, end) - expected).mean()


def _examples(records, vocab, max_len):
    out = []
    for r in records:
        surface = r.get("normalized", r["raw"])[:max_len]
        if surface:
            out.append((encode_chars(surface, vocab), bio_ids(len(surface), r["spans"])))
    return out


def _collate(batch, device):
    m = max(len(ids) for ids, _ in batch)
    bi = torch.zeros(len(batch), m, dtype=torch.long)
    bt = torch.zeros(len(batch), m, dtype=torch.long)  # pad tag = O; mask excludes it from the crf
    mask = torch.zeros(len(batch), m, dtype=torch.bool)
    for i, (ids, tags) in enumerate(batch):
        bi[i, : len(ids)] = torch.tensor(ids)
        bt[i, : len(tags)] = torch.tensor(tags)
        mask[i, : len(ids)] = True
    return bi.to(device), bt.to(device), mask.to(device)


@torch.no_grad()
def _val_f1(model, examples, device, batch) -> float:
    model.eval()
    start = model.start_transitions.cpu().numpy()
    trans = model.transitions.cpu().numpy()
    end = model.end_transitions.cpu().numpy()
    tp = pred = expected = 0
    for i in range(0, len(examples), batch):
        chunk = examples[i : i + batch]
        bi, _, _ = _collate(chunk, device)
        emissions = model(bi).cpu().numpy()
        for (ids, tags), emit in zip(chunk, emissions, strict=True):  # slice back to real length
            p = set(decode_spans(viterbi(emit[: len(ids)], start, trans, end)))
            g = set(decode_spans(tags))
            tp += len(p & g)
            pred += len(p)
            expected += len(g)
    return _prf(tp, pred, expected)[2]


def _strip_docstrings(path: Path) -> None:
    """the dynamo exporter bakes local source paths into the node metadata; the artifact ships."""
    import onnx

    m = onnx.load(str(path))
    m.doc_string = ""
    for node in m.graph.node:
        node.doc_string = ""
        del node.metadata_props[:]  # holds the exporter stack traces
    onnx.save(m, str(path))


def _export(model, vocab, n_tags, max_len, cfg, out_dir):
    model.eval()
    model.cpu()  # export + sample on cpu; artifact is cpu-only (CPUExecutionProvider)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    sample = torch.ones(1, max_len, dtype=torch.long)  # non-pad: clean trace of the masked path
    torch.onnx.export(
        model,
        (sample,),
        str(d / ONNX_NAME),
        input_names=["ids"],
        output_names=["logits"],
        dynamo=True,
        # dynamic time axis: serve at actual len, not padded max_len
        dynamic_shapes={"ids": {1: Dim("seq", min=1, max=max_len)}},
        external_data=False,
    )
    _strip_docstrings(d / ONNX_NAME)
    torch.save(model.state_dict(), d / "segmenter.pt")  # local re-export source, skips retrain
    meta = {
        "labels": list(LABELS),
        "vocab": vocab,
        "max_len": max_len,
        "config": cfg,
        "transitions": {
            "start": model.start_transitions.detach().cpu().tolist(),
            "trans": model.transitions.detach().cpu().tolist(),
            "end": model.end_transitions.detach().cpu().tolist(),
        },
    }
    (d / META_NAME).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print(f"[+] exported {d / ONNX_NAME} + meta")
    print(f"[i] vocab {len(vocab)} | {n_tags} tags | max_len {max_len}")


def train(train_path, out_dir, *, dim, layers, heads, epochs, batch, lr, max_len, val_frac, seed):
    if epochs < 1:
        raise ValueError("[!] epochs must be >= 1")
    random.seed(seed)
    torch.manual_seed(seed)
    np.random.seed(seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    print(f"[-] loading corpus {train_path}")
    records = read_jsonl(train_path)
    random.shuffle(records)
    n_val = max(1, int(len(records) * val_frac))
    val, tr = (
        records[:n_val],
        records[n_val:],
    )  # val carved from train.jsonl; hard.jsonl is the gate

    vocab = build_vocab(tr, max_len)
    n_tags = len(build_tags())
    print(f"[i] {len(tr)} train / {len(val)} val | vocab {len(vocab) + 2} syms | {n_tags} tags")

    tr_ex, va_ex = _examples(tr, vocab, max_len), _examples(val, vocab, max_len)
    model = SegModel(len(vocab) + 2, n_tags, dim=dim, layers=layers, heads=heads).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    steps = max(1, math.ceil(len(tr_ex) / batch)) * epochs
    warm = max(1, steps // 20)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt,
        lambda s: (
            s / warm
            if s < warm
            else 0.5 * (1 + math.cos(math.pi * (s - warm) / max(1, steps - warm)))
        ),
    )

    cfg = {"dim": dim, "layers": layers, "heads": heads}
    best_f1 = -1.0
    for ep in range(epochs):
        model.train()
        random.shuffle(tr_ex)
        total = 0.0
        nb = 0
        for i in range(0, len(tr_ex), batch):
            bi, bt, mask = _collate(tr_ex[i : i + batch], device)
            loss = model.nll(model(bi), bt, mask)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
            total += loss.item()
            nb += 1
        f1 = _val_f1(model, va_ex, device, batch)
        print(f"[i] epoch {ep + 1}/{epochs}  loss={total / max(1, nb):.4f}  val seg-F1={f1:.4f}")
        if f1 > best_f1:  # checkpoint to disk on improvement: crash-safe, early-stoppable
            best_f1 = f1
            state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            best = SegModel(len(vocab) + 2, n_tags, dim=dim, layers=layers, heads=heads)
            best.load_state_dict(state)
            _export(best, vocab, n_tags, max_len, cfg, out_dir)
    print(f"[+] best val seg-F1 {best_f1:.4f}")
