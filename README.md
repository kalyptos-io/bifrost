# Bifrost

Danish address resolution engine building on a novel approach to structured search to achieve frontier accuracy, performance and scalability. It reads noisy or incomplete Danish input and returns the official
address. One engine does autocomplete and normalization together. API reference:
<https://bifrost.kalyptos.io/docs>.

## Results

### Performance

The 2026-08-27 run used two API workers pinned to two physical i5-13400F cores, a separate database,
no response cache, and 2,500 synthetic queries.

| clients | successful req/s | p50 latency | p99 latency | shed |
| ------: | ---------------: | ----------: | ----------: | ---: |
|       1 |             66.2 |      9.5 ms |     97.2 ms | 0.0% |
|       4 |            142.0 |     18.8 ms |    149.3 ms | 0.0% |
|      16 |            147.7 |     78.6 ms |    441.7 ms | 0.0% |
|     160 |            147.8 |    899.3 ms |      3.0 s | 1.8% |

Two workers reach their capacity near 148 successful requests/s. At 160 clients, admitted requests
queue and excess work is deliberately shed with `Retry-After`; there were no unexpected errors.

### Accuracy

We ran the same deterministic sample once against
[DAWA](https://dawadocs.dataforsyningen.dk/dok/guide/datavask). DAWA rejected 74 inputs longer than
100 characters, so both systems were compared on the remaining 2,426 inputs: 2,023 with a target
and 403 without one.

| system  | recall@1 | recall@5 | confident false match |
| ------- | -------: | -------: | --------------------: |
| Bifrost |    48.5% |    54.5% |                  0.5% |
| DAWA    |    30.4% |    38.8% |                  0.0% |

Exact recall means the generated target DAR id is first or in the first five results. The permanent
result files and provenance manifest contain only Bifrost results and are in
`docs/benchmarks/2026-08-27/`.

## Local development

Each part describes itself: [app](app/), [sync](sync/), [train](train/), [ui](ui/), [docs](docs/).

```sh
uv sync                       # resolve the workspace into .venv (python 3.13)
uv run ruff check .           # lint
uv run ruff format .          # format
uv run pytest                 # tests

docker compose up --build     # app + postgres + dragonfly + ui + docs, read-only rootless
curl localhost:8000/health
```

Compose serves the ui on :5173, the docs on :4321 and the api on :8000. Fill the data once
(Datafordeler OAuth credentials required); the first run makes a baseline, then daily deltas follow.

```sh
docker compose --profile sync run sync sync   # no --rm: a failed run keeps its logs
```

The always-on loop is `docker compose --profile sync up sync`. See [sync](sync/) for how a
generation reaches the app.

---

Danish register data from Datafordeleren under CC BY 4.0. Full attributions in [NOTICE](NOTICE).
