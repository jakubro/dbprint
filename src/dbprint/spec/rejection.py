"""A column's inferred fields a human rejected, and what a consumer withholds for each (SPEC 2.7.1)."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Literal, cast


RejectableField = Literal[
    "candidate_key",
    "looks_like",
    "looks_like_candidate",
    "sensitivity",
    "epoch_unit",
]

REJECTABLE: Mapping[RejectableField, tuple[str, ...]] = {
    "candidate_key": ("candidate_key", "candidate_key_exception"),
    "looks_like": ("looks_like", "sampled", "matched"),
    "looks_like_candidate": ("looks_like_candidate", "looks_like_candidate_share"),
    "sensitivity": ("sensitivity",),
    "epoch_unit": ("epoch_unit",),
}


def rejected_fields(entry: Mapping[str, Any] | None) -> frozenset[RejectableField]:
    """The fields an admitted `statistics.annotations.yaml` column entry lists under `rejected`."""

    return frozenset(cast(list[RejectableField], (entry or {}).get("rejected") or []))


def without_rejected(
    column: dict[str, Any],
    rejected: Collection[RejectableField],
) -> dict[str, Any]:
    """`column` less the `inferred` keys `rejected` withholds; `inferred` goes when it empties.

    Never mutates: returns `column` itself when it carries nothing to withhold, else a copy.
    """

    withheld = {key for field in rejected for key in REJECTABLE[field]}
    inferred = column.get("inferred")

    if not isinstance(inferred, Mapping) or not withheld & inferred.keys():
        return column

    kept = {key: value for key, value in inferred.items() if key not in withheld}

    return {
        key: kept if key == "inferred" else value
        for key, value in column.items()
        if key != "inferred" or kept
    }
