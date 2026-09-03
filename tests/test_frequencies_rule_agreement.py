"""SPEC 2.2.5's priority order is spelled twice for `numeric`/`temporal` columns.

`dbprint.spec.distribution` computes it from a top-N fetch's ordered counts;
`conformance.statistics` recomputes it arithmetically from the four `frequencies` integers,
because the conformance suite must not import the engine or its adapters. The duplication
is deliberate, drift between the two is not - this test is what fails it.
"""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.conformance import statistics
from dbprint.spec.classification import compute_cardinality_ratio
from dbprint.spec.distribution import Frequencies, classify, summarize


PATH = "public/t/statistics.yaml"
FQN = "public.t"

# (counts, non_null, exhaustive), `counts` ordered DESC. Covers SPEC 2.2.5's priority
# order, 2.2.7's single-value case and the incoherent-ratio fallback.
CASES = [
    pytest.param([95, 5], 100, True, id="dominant-value"),
    pytest.param([10, 8, 5], 100, False, id="long-tail"),
    pytest.param([10, 8, 5], 100, True, id="long-tail-skipped-when-exhaustive"),
    pytest.param([10, 2], 12, True, id="imbalanced"),
    pytest.param([6, 6], 12, True, id="uniform"),
    pytest.param([100], 100, True, id="single-value-dominant"),
    pytest.param([40, 20], 5, True, id="incoherent-ratio-multi-entry"),
    pytest.param([40, 20], 5, False, id="incoherent-ratio-non-exhaustive"),
]


@pytest.mark.parametrize(("counts", "non_null", "exhaustive"), CASES)
def test_the_conformance_check_agrees_with_the_producers_verdict(
    counts: list[int],
    non_null: int,
    exhaustive: bool,
) -> None:
    verdict = classify(counts, non_null, exhaustive=exhaustive)
    freq = summarize(counts)
    cardinality = freq.listed if exhaustive else freq.listed + 1

    payload = _payload(verdict, freq, cardinality, non_null)

    assert _errors(payload) == []


@pytest.mark.parametrize(("counts", "non_null", "exhaustive"), CASES)
def test_a_verdict_the_producer_would_not_have_published_is_caught(
    counts: list[int],
    non_null: int,
    exhaustive: bool,
) -> None:
    verdict = classify(counts, non_null, exhaustive=exhaustive)
    freq = summarize(counts)
    cardinality = freq.listed if exhaustive else freq.listed + 1
    wrong = next(
        v for v in ("uniform", "imbalanced", "dominant_value", "long_tail") if v != verdict
    )

    payload = _payload(wrong, freq, cardinality, non_null)

    assert "stats.distribution-contradicts-frequencies" in _codes(payload)


def _payload(
    distribution: str,
    freq: Frequencies,
    cardinality: int,
    non_null: int,
) -> dict[str, Any]:
    return {
        "format_version": 1,
        "table": FQN,
        "type": "table",
        "profiled_at": "2026-01-01T00:00:00Z",
        "row_count": non_null,
        "row_count_method": "exact",
        "columns": {
            "n": {
                "sql_type": "numeric",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": cardinality,
                "cardinality_ratio": compute_cardinality_ratio(cardinality, non_null),
                "cardinality_method": "exact",
                "classification": "numeric",
                "range": {"min": 1, "max": 100},
                "percentiles": {"p50": 50},
                "mean": 50.0,
                "sum": 5000.0,
                "zero_count": 0,
                "negative_count": 0,
                "quantized_count": non_null,
                "values": [{"value": 1, "count": freq.top}],
                "distribution": distribution,
                "frequencies": {
                    "top": freq.top,
                    "bottom": freq.bottom,
                    "listed": freq.listed,
                    "total": freq.total,
                },
            },
        },
    }


def _errors(payload: dict[str, Any]) -> list[Any]:
    return [i for i in statistics.check(payload, PATH, FQN) if i.severity == "error"]


def _codes(payload: dict[str, Any]) -> set[str]:
    return {i.code for i in statistics.check(payload, PATH, FQN)}
