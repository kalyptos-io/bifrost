# train

Local only. It makes the synthetic corpus and the char-level NER segmenter that `app` loads. The
code is never on the request path; only the exported `.onnx` artifact ships.

## Getting the baseline

`train/data/baseline_addresses.jsonl` is not in git (~2 GB). It is produced by the `sync` member
from live Datafordeler data, so a fresh checkout has to build it once:

```sh
bifrost-sync baseline --all     # full fetch of all 55 entities into dlt staging schema
bifrost-sync export             # stream corpus jsonl to train/data/baseline_addresses.jsonl
```

See `sync/README.md` for the Datafordeler OAuth credentials and the work-dir. What it costs, from
the live deployment:

| | |
|---|---|
| work-dir on disk | ~3 GB settled; the per-entity zips stay for resume. The chart asks for 40 Gi because dlt's normalize spool, not the zips, sets the peak |
| `datafordeler` staging schema | 7.3 GB in Postgres |
| each derived serving generation | 5.4-5.9 GB, and a few are kept at once |
| Postgres in total | ~22 GB with staging plus three generations |
| the corpus jsonl | ~1.9 GB, 4.16 M addresses |
| time | the one measured full baseline took ~32 min to stage and ~24 min to snapshot on a dev machine; the download on top of that depends on your link to Datafordeler |

So it is an afternoon on a machine with ~40 GB to spare, not a weekend. `export` needs matrikel
present in staging - it builds a throwaway schema to do the join - so run it after a baseline or a
tick has finished, not during one.

The export is a snapshot of DAR on the day it runs, so a later export gives different rows and will
not reproduce a recorded eval number exactly.

## Synthetic data

`train/gen` makes deterministic training and held-out corpora from a local DAR export. It varies
address shape, missing fields, typed prefixes, formatting, encoding, OCR-like errors, typos, junk,
field order, unit notation, and invalid queries. Training and held-out address ids are disjoint.

```sh
cd train
uv run python -m gen.generate --in data/baseline_addresses.jsonl \
  --out data/synth/train.jsonl --hard-out data/synth/hard.jsonl \
  --n 1000000 --hard-frac 0.15 --seed 1
uv run python -m gen.validate --train data/synth/train.jsonl --held data/synth/hard.jsonl
```

## The model

The segmenter is a lean little character tagger. Every character of the normalized query gets one BIO tag over
the nine span labels (street, house_number, house_letter, floor, door, sub_locality, postcode, city,
junk), so 19 tags in all. Characters between spans stay `O`.

- Vocabulary: the characters observed in the corpus, sorted, ids from 2 (0 is PAD, 1 is UNK).
  Around 56 entries. Anything unseen at serve time becomes UNK rather than an error. Input is cut at
  256 characters.
- Encoder: a small transformer - 3 layers, width 192, 6 heads, rotary position embeddings. About
  1.4 MB on disk after quantizing. It is small on purpose: the whole thing runs per request on a CPU
  core beside the belief engine.
- CRF head: a linear-chain CRF scores whole tag paths instead of each character alone, and
  Viterbi picks the best path. BIO-illegal moves are forced out, so a span can never open on `I-` or
  change label without a new `B-`. This is what keeps a long street name from breaking into pieces
  when one character is noisy.
- Training: AdamW at 3e-4, batch 256, 12 epochs, 5% held out for validation, loss is the CRF
  negative log-likelihood. The best validation epoch is the one exported.
- Export: ONNX with a dynamic time axis, so serving runs at the real query length instead of
  padding to 256, then int8-dynamic quantization. CPU only, loaded by onnxruntime. The two together
  are worth roughly 13x on `segment()` latency at no measurable cost in seg-F1.

The label scheme, the character encoding and the span decode live in `app`
(`app/.../arms/segmenter.py`), and `train` imports them back. Same rule for the normalizer: one
implementation, never a copy. Divergence between serving and generation trains a phantom.

## Training and evaluation

```sh
uv run python -m train.model.segmenter train --train train/data/synth/train.jsonl \
  --out train/data/artifacts
uv run python -m train.model.segmenter quantize --in train/data/artifacts --out train/data/artifacts

cd train
uv run python -m eval.bench --synth data/synth/hard.jsonl --limit 2500 \
  --sample-seed 1 --cache-state disabled --workers 2 \
  --out ../docs/benchmarks/<date>
```

Training also writes `segmenter.pt` next to the artifact. It is not in git, but keeping it locally
lets you re-export without retraining.
