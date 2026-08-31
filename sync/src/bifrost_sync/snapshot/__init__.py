"""snapshot: derive the serving tables from dlt staging into an immutable gen_<ts> schema.

SQL-streaming (server-side cursors + postgres joins), not today's multi-GB python dicts: python
holds only StreetIds (~130k), the district STRtrees, and per-batch COPY buffers. the app serving
path is untouched - it selects generations by shape fingerprint and never sees who produced them.
"""

from __future__ import annotations

STAGING = "datafordeler"  # the persistent dlt staging dataset the snapshot reads from
