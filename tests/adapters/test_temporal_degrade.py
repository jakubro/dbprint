"""What a failed temporal block costs a column, and that every adapter says so (SPEC 2.2.4).

The degrade is copied per adapter, so one adapter dropping the marker is what this catches.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib

import pytest

from dbprint.adapters.base import temporal_block_unmeasured
from dbprint.cli.adapter_registry import ADAPTERS


_ADAPTERS_WITH_STATS = sorted(
    name
    for name in ADAPTERS
    if importlib.util.find_spec(f"dbprint.adapters.{name}.stats") is not None
)


def _stats_source(adapter: str) -> pathlib.Path:
    spec = importlib.util.find_spec(f"dbprint.adapters.{adapter}.stats")
    assert spec is not None and spec.origin is not None

    return pathlib.Path(spec.origin)


class TestTheSharedLossList:
    def test_a_timestamp_loses_the_whole_block_including_the_day_truncation(self) -> None:
        assert temporal_block_unmeasured("TIMESTAMP") == (
            "distribution",
            "frequencies",
            "freshness",
            "percentiles",
            "quantized_count",
            "range",
            "values",
        )

    def test_a_date_does_not_name_quantized_count(self) -> None:
        """It is its own day-truncation, so SPEC 2.2.3 never required it - and naming a field the
        matrix does not require is `stats.unmeasured-names-unrequired-field`.
        """

        assert "quantized_count" not in temporal_block_unmeasured("DATE")

    def test_a_time_does_not_name_it_either(self) -> None:
        assert "quantized_count" not in temporal_block_unmeasured("TIME")

    def test_the_names_are_sorted_and_unique(self) -> None:
        """SPEC 2.2.4 requires both of the emitted list, and the engine emits this one verbatim."""

        names = temporal_block_unmeasured("TIMESTAMP")

        assert list(names) == sorted(set(names))


@pytest.mark.parametrize("adapter", _ADAPTERS_WITH_STATS)
def test_no_degrade_returns_a_column_without_naming_what_it_lost(adapter: str) -> None:
    """Every `except` handler that hands back a rebuilt `ColumnStats` must set `unmeasured`.

    `return stats` untouched ships a column missing fields SPEC 2.2.3 marks REQUIRED.
    """

    tree = ast.parse(_stats_source(adapter).read_text(encoding="utf-8"))
    rebuilt = [
        (handler, last.value)
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        for last in [handler.body[-1]]
        if isinstance(last, ast.Return) and isinstance(last.value, ast.Call)
        if isinstance(last.value.func, ast.Name) and last.value.func.id.endswith("replace")
    ]

    assert rebuilt, f"{adapter} has no statistics degrade path at all"

    for handler, call in rebuilt:
        keywords = {kw.arg for kw in call.keywords}

        assert "unmeasured" in keywords, (
            f"{adapter}/stats.py:{handler.lineno} degrades without naming the loss"
        )


@pytest.mark.parametrize("adapter", _ADAPTERS_WITH_STATS)
def test_no_degrade_hands_back_the_untouched_stats_object(adapter: str) -> None:
    """A bare `return stats` is a silent loss: the column ships short with nothing saying so."""

    tree = ast.parse(_stats_source(adapter).read_text(encoding="utf-8"))
    bare = [
        handler.lineno
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        for last in [handler.body[-1]]
        if isinstance(last, ast.Return)
        if isinstance(last.value, ast.Name) and last.value.id == "stats"
    ]

    assert bare == [], f"{adapter}/stats.py degrades silently at line(s) {bare}"
