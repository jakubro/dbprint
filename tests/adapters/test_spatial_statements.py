"""Every adapter's spatial read emits one grouped statement its own engine's dialect accepts.

Most substrates cannot run these accessors, so the statement is captured rather than executed.
"""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from dbprint.adapters import ColumnMeta
from dbprint.adapters.base import ExtentNotMeasured
from dbprint.adapters.dialect import Vendor
from dbprint.adapters.errors import QueryFailed
from tests.adapters._dialects import STATS_MODULES, foreign_fragments
from tests.adapters._sql_style import alias_violations, layout_violations, violations


_SPATIAL_TYPES: dict[str, str] = {
    "postgres": "geometry",
    "mysql": "geometry",
    "snowflake": "GEOGRAPHY",
    "duckdb": "GEOMETRY",
    "clickhouse": "Geometry",
    "redshift": "geography",
    "databricks": "geography(4326)",
    "bigquery": "GEOGRAPHY",
}

_TAKES_IDENTITY = frozenset({"snowflake", "bigquery"})


class _GeographicSrs(Exception):
    errno = 3618


class _Recorded:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


@pytest.mark.parametrize("vendor", sorted(_SPATIAL_TYPES))
def test_the_spatial_read_speaks_its_own_dialect(
    vendor: Vendor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _read(vendor, monkeypatch, STATS_MODULES[vendor])
    grouped = [s for s in statements if "group by" in s.lower()]

    assert len(grouped) == 1, statements
    assert foreign_fragments(grouped[0], vendor) == []
    assert violations(grouped[0], vendor) + alias_violations(grouped[0], vendor) == []
    assert layout_violations(grouped[0], vendor) == []


def test_a_geographic_extent_failure_keeps_the_mysql_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = STATS_MODULES["mysql"]
    seen: list[str] = []

    def execute(_cursor: Any, sql: str, *_params: Any) -> _Recorded:
        seen.append(sql)

        if "ST_ENVELOPE" in sql:
            raise QueryFailed(_GeographicSrs(), sql)

        return _Recorded([("POINT", 4326, 3, 0, 0, None, None, None, None)])

    monkeypatch.setattr(module, "exec_query", execute)
    column = ColumnMeta(name="site", sql_type="point", nullable=True, default=None, ordinal=1)

    with pytest.raises(ExtentNotMeasured) as raised:
        module._fetch_spatial(object(), "`t`", column)

    assert raised.value.geometry.kinds == (("point", 3),)
    assert raised.value.geometry.srids == ((4326, 3),)
    assert len(seen) == 2


def _read(vendor: str, monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> list[str]:
    seen: list[str] = []

    def execute(_cursor: Any, sql: str, *_params: Any) -> _Recorded:
        seen.append(sql)

        return _Recorded([])

    monkeypatch.setattr(module, "exec_query", execute)
    column = ColumnMeta(
        name="site",
        sql_type=_SPATIAL_TYPES[vendor],
        nullable=True,
        default=None,
        ordinal=1,
    )
    source = "src_table src"

    if vendor in _TAKES_IDENTITY:
        identity = SimpleNamespace(source_column=lambda name: f"src.{name}")
        module._fetch_spatial(object(), identity, source, column)
    else:
        module._fetch_spatial(object(), source, column)

    return seen
