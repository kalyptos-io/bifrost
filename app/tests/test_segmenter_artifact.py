"""Real-artifact smoke: load the trained segmenter and assert it yields labeled spans.

skips unless the gitignored artifact is present, so it guards the serving path (the .onnx + meta
delivered into the image at /models/segmenter) without needing docker or a db.
"""

from pathlib import Path

import pytest
from bifrost.arms import segmenter

_DIR = Path(__file__).resolve().parents[2] / "train" / "data" / "artifacts"
_has = (_DIR / segmenter.ONNX_NAME).exists() and (_DIR / segmenter.META_NAME).exists()


@pytest.mark.skipif(not _has, reason="trained segmenter artifact absent")
def test_artifact_loads_and_segments():
    segmenter.load(_DIR)
    spans = segmenter.segment("Vestergade 41A 2 tv 4850 Stubbekøbing")
    assert spans and {s[0] for s in spans} & set(segmenter.LABELS)
