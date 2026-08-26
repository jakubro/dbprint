"""`_empty_stats` is the same body, verbatim, in all three adapters (SPEC 2.2.7).

No import can hold the three together - each adapter module is independent and the conformance
suite must not import any of them - so this is what catches one copy drifting from the others.
"""

from __future__ import annotations

from dataclasses import fields
from types import ModuleType

from dbprint.adapters import ColumnMeta
from dbprint.adapters.mysql import stats as mysql_stats
from dbprint.adapters.postgres import stats as postgres_stats
from dbprint.adapters.snowflake import stats as snowflake_stats


_ADAPTERS = {
    "postgres": (postgres_stats, "integer", "bytea"),
    "mysql": (mysql_stats, "int", "blob"),
    "snowflake": (snowflake_stats, "number", "bytea"),
}


def _column(sql_type: str) -> ColumnMeta:
    return ColumnMeta(name="c", sql_type=sql_type, nullable=True, default=None, ordinal=1)


def _shape(module: ModuleType, sql_type: str) -> dict[str, object]:
    """Every `_empty_stats` field but `sql_type`, which each vendor spells its own way."""

    stats = module._empty_stats(_column(sql_type))

    return {f.name: getattr(stats, f.name) for f in fields(stats) if f.name != "sql_type"}


def test_a_supported_column_produces_the_same_shape_everywhere() -> None:
    shapes = {
        vendor: _shape(module, supported) for vendor, (module, supported, _u) in _ADAPTERS.items()
    }

    assert len({tuple(sorted(s.items())) for s in shapes.values()}) == 1, shapes


def test_an_unsupported_column_produces_the_same_shape_everywhere() -> None:
    shapes = {
        vendor: _shape(module, unsupported)
        for vendor, (module, _s, unsupported) in _ADAPTERS.items()
    }

    assert len({tuple(sorted(s.items())) for s in shapes.values()}) == 1, shapes


def test_unsupported_carries_no_cardinality() -> None:
    for module, _supported, unsupported in _ADAPTERS.values():
        stats = module._empty_stats(_column(unsupported))

        assert stats.cardinality is None
        assert stats.cardinality_method is None


def test_supported_carries_a_zero_exact_cardinality() -> None:
    for module, supported, _unsupported in _ADAPTERS.values():
        stats = module._empty_stats(_column(supported))

        assert stats.cardinality == 0
        assert stats.cardinality_method == "exact"
