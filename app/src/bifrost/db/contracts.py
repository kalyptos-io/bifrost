"""versioned serving contracts over the immutable dataset generations.

a contract pins one physical build fingerprint (bifrost.db.shape.build_fingerprint) to a contract
version. the app serves the CURRENT contract and, for a rollback window, the PREVIOUS one - exactly
those two, never a generic N. fingerprints are hard-coded literals so this registry, not a live
recompute, is the source of truth; the contract test in test_generations trips when
build_fingerprint() drifts from CURRENT.fingerprint.

to bump: add a new Contract as CURRENT and demote the old CURRENT to PREVIOUS.

demoting carries a compatibility floor: select_current falls back to PREVIOUS when no current-pair
generation exists yet, so the current binary must remain able to read the PREVIOUS contract's
schema - degraded new-feature endpoints are acceptable, crashes are not.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Contract:
    version: int
    fingerprint: str


CURRENT = Contract(3, "34faa59a3e2683d14786efc7f0a1b41ce640f409d7a9777d84215320ac5c9714")
PREVIOUS: Contract | None = Contract(
    2, "3b78577a7c3f1bcc4821d37629f5571ee569e1eabab3c63d0c2d6b5ba01571c5"
)
