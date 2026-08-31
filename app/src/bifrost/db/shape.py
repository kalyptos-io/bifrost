"""data-shape fingerprint: a sha256 over the COPY column contracts + the normalizer version.

sync snapshots a new generation on shape drift and the app only serves a matching one, so this
must change iff the on-disk shape changes. column ORDER is significant (it is the COPY tuple
contract). stdlib + two leaf imports only; no `train` import - app can't depend on train.
"""

from __future__ import annotations

import hashlib
import json

from bifrost.arms import normalize
from bifrost.db import (
    ADDRESS_COLUMNS,
    ADMIN_AREA_COLUMNS,
    AREA_ALIAS_COLUMNS,
    AUX_CITY_MAP_COLUMNS,
    AUX_POSTCODE_DIM_COLUMNS,
    AUX_SUBLOC_MAP_COLUMNS,
    EJENDOM_COLUMNS,
    MATRIKEL_COLUMNS,
    ROAD_COLUMNS,
    STEDNAVNE_COLUMNS,
    STREET_ALIAS_COLUMNS,
    STREET_DIM_COLUMNS,
)

# bump when ingested content changes without altering a column contract (a new admin_area kind, or
# a geometry re-encoding) - forces a one-time reseed via the same drift path
SEED_CONTENT_VERSION = 6


def build_fingerprint() -> str:
    # order-preserving: the tuples ARE the COPY contract; json canonicalizes the structure
    payload = json.dumps(
        [
            ADDRESS_COLUMNS,
            STREET_DIM_COLUMNS,
            ROAD_COLUMNS,
            ADMIN_AREA_COLUMNS,
            MATRIKEL_COLUMNS,
            STEDNAVNE_COLUMNS,
            EJENDOM_COLUMNS,
            STREET_ALIAS_COLUMNS,
            AREA_ALIAS_COLUMNS,
            AUX_POSTCODE_DIM_COLUMNS,
            AUX_CITY_MAP_COLUMNS,
            AUX_SUBLOC_MAP_COLUMNS,
            normalize.NORMALIZER_VERSION,
            SEED_CONTENT_VERSION,
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()
