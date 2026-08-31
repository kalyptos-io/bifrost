"""street: trigram `<->` similarity belief. tier A, source - the one graded driver stream."""

from bifrost.core.types import Axis, Belief, Capability, Decomposition, Grade

from ._params import PARAMS


def street(decomposition: Decomposition) -> Belief | None:
    if decomposition.street is None:
        return None
    return Belief(
        axis=Axis.STREET,
        value=decomposition.street,
        weight=PARAMS.weights[Axis.STREET],
        grade=Grade.TRIGRAM,
        capability=Capability.SOURCE,
    )
