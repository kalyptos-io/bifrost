"""build.py pure ports (no db): the captured-index IN-list."""

from __future__ import annotations

import re

from bifrost_sync.snapshot.build import _CAPTURE_INDEXES


def test_capture_indexes_covers_the_bulk_loaded_tables():
    in_list = re.search(r"tablename IN \(([^)]+)\)", _CAPTURE_INDEXES).group(1)
    names = set(re.findall(r"'([^']+)'", in_list))
    assert names == {"addresses", "street_dim", "street_postcode", "matrikel"}
