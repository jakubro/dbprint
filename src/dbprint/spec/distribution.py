"""`distribution` classification per SPEC 2.2.5.

`classify` is the single source both the value-list and top-N paths call, so the two can never
disagree on the priority order, nor publish a shape from a ratio `is_incoherent` rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .coverage import is_incoherent


Distribution = Literal["uniform", "imbalanced", "dominant_value", "long_tail"]

DOMINANT_VALUE_THRESHOLD = 0.95
LONG_TAIL_SHARE_THRESHOLD = 0.30
IMBALANCE_RATIO = 2


def classify(counts: list[int], non_null: int, *, exhaustive: bool) -> Distribution:
    """SPEC 2.2.5 priority order over a column's ordered counts (count DESC).

    `counts` is a value list's per-entry counts or a top-N frequency fetch, ordered the same
    way; only the order and the sum are read. `long_tail` is skipped for an exhaustive list
    (no tail beyond itself) and when the counts exceed `non_null`, a ratio `is_incoherent`
    already rejected.
    """

    if not counts or non_null <= 0:
        return "uniform"

    if is_incoherent(sum(counts), non_null):
        return _imbalance_or_uniform(counts, exhaustive=exhaustive)

    if counts[0] / non_null >= DOMINANT_VALUE_THRESHOLD:
        return "dominant_value"

    if not exhaustive and sum(counts) / non_null < LONG_TAIL_SHARE_THRESHOLD:
        return "long_tail"

    return _imbalance_or_uniform(counts, exhaustive=exhaustive)


@dataclass(frozen=True)
class Frequencies:
    """SPEC 2.2.5's four-integer top-N summary, published on `numeric`/`temporal` columns.

    Reproduces the verdict's arithmetic without a literal: `top` decides `dominant_value`,
    `total` decides `long_tail`'s ratio against `non_null`, and the `top`-to-`bottom` spread
    decides `imbalanced` against `uniform`. `listed` tells a truncated read from an exhaustive
    one without `top_n_values` itself reaching the artifact.
    """

    top: int
    bottom: int
    listed: int
    total: int


def summarize(counts: list[int]) -> Frequencies:
    """The `Frequencies` summary for one top-N fetch, already capped to `n`."""

    if not counts:
        return Frequencies(top=0, bottom=0, listed=0, total=0)

    return Frequencies(top=max(counts), bottom=min(counts), listed=len(counts), total=sum(counts))


def _imbalance_or_uniform(counts: list[int], *, exhaustive: bool) -> Distribution:
    """Fallback verdict, plus SPEC 2.2.7's single-value edge case.

    An exhaustive list holding exactly one non-zero count evidences `cardinality = 1`, which
    SPEC 2.2.7 requires to read `dominant_value` whatever the ratio's coherence. A truncated
    list of one entry proves nothing about cardinality, so the rule is exhaustive-only.
    """

    non_zero = [c for c in counts if c > 0]

    if exhaustive and len(non_zero) == 1:
        return "dominant_value"

    if not non_zero:
        return "uniform"

    return "imbalanced" if max(counts) / min(non_zero) > IMBALANCE_RATIO else "uniform"
