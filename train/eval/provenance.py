"""run provenance for the eval artifacts: which commit, when, over exactly which input bytes.

stdlib only - imported by the bench + snapshot + calibrate CLIs so every emitted artifact carries
the same block. named inputs are hashed; any other keyword is carried verbatim.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str:
    try:
        r = subprocess.run(("git", *args), capture_output=True, text=True, cwd=_ROOT, check=True)
    except (OSError, subprocess.CalledProcessError):  # no git, or not a checkout
        return ""
    return r.stdout.strip()


def _sha256(path: str | Path) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(path: str | Path) -> str:
    """repo-relative where possible: an absolute local path is not publishable provenance."""
    p = Path(path)
    try:
        return str(p.resolve().relative_to(_ROOT))
    except ValueError:
        return p.name


def manifest(files: Mapping[str, str | Path | None] | None = None, **extra: Any) -> dict:
    return {
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": sys.version.split()[0],
        "command": " ".join((_rel(sys.argv[0]), *sys.argv[1:])),
        "inputs": {
            name: {"path": _rel(p), "sha256": _sha256(p)}
            for name, p in (files or {}).items()
            if p is not None
        },
        **extra,
    }
