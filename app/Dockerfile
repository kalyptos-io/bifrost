# syntax=docker/dockerfile:1
# build context is the repo root (workspace lives there); see compose.yaml.

FROM python:3.13.15-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /repo

# deps layer: manifests + lock only, so it caches independently of source.
# every member pyproject is needed for workspace resolution; --package limits install to bifrost.
COPY pyproject.toml uv.lock ./
COPY app/pyproject.toml app/pyproject.toml
COPY train/pyproject.toml train/pyproject.toml
COPY sync/pyproject.toml sync/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev --package bifrost

COPY app/ app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package bifrost

# distroless: non-root (uid 65532), no shell, no package manager. venv's own interpreter path
# won't exist here, so run via the image's python3.13 + PYTHONPATH.
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:f3d5ddc6c64a019fe520e7f005f2880be21e6afc461b10a3c15ef2e4edc71e33
WORKDIR /app
COPY --from=build /repo/.venv /app/.venv
COPY train/data/artifacts/segmenter.onnx train/data/artifacts/segmenter.meta.json /models/segmenter/
ENV PYTHONPATH=/app/.venv/lib/python3.13/site-packages \
    BIFROST_SEGMENTER=/models/segmenter \
    PROMETHEUS_MULTIPROC_DIR=/run/prometheus
# read-only rootfs: multiproc metrics dir must be a writable mount at PROMETHEUS_MULTIPROC_DIR
EXPOSE 8000
# bifrost.api.main.run() reads BIFROST_WORKERS and launches uvicorn (env-driven, no shell needed)
ENTRYPOINT ["python3", "-m", "bifrost.api.main"]
