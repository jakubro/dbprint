"""What the engine writes for a column's `unmeasured` marker (SPEC 2.2.4).

The adapter reports what its read lost; the engine names only what the artifact owed and lacks.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    Frequencies,
    MockAdapter,
    MockTable,
    Range,
    ValueCount,
)
from dbprint.config import ConnectionConfig
from dbprint.conformance.schema_validation import check_statistics
from dbprint.conformance.statistics import check
from dbprint.engine import Engine


_LOST = ("distribution", "frequencies", "values")


class TestWhatReachesTheArtifact:
    def test_a_genuinely_lost_required_field_is_named(self, tmp_path: Path) -> None:
        column = _degraded(unmeasured=_LOST)

        assert _column(_generate(tmp_path, column))["unmeasured"] == sorted(_LOST)

    def test_a_name_the_column_also_emits_is_dropped(self, tmp_path: Path) -> None:
        """`cardinality_method` is computed before the failing statement and emitted regardless,
        so naming it would be `stats.unmeasured-names-emitted-field` on every degraded column.
        """

        column = _degraded(unmeasured=("cardinality_method", *_LOST))
        payload = _column(_generate(tmp_path, column))

        assert payload["unmeasured"] == sorted(_LOST)
        assert payload["cardinality_method"] == "exact"

    def test_a_name_the_classification_never_required_is_dropped(self, tmp_path: Path) -> None:
        """`mean` is forbidden outside `numeric`, so its absence is structural and needs no
        marker - naming it is `stats.unmeasured-names-unrequired-field`.
        """

        column = _degraded(unmeasured=("mean", *_LOST))

        assert _column(_generate(tmp_path, column))["unmeasured"] == sorted(_LOST)

    def test_a_column_that_lost_nothing_carries_no_key(self, tmp_path: Path) -> None:
        assert "unmeasured" not in _column(_generate(tmp_path, _measured()))

    def test_the_degraded_column_validates(self, tmp_path: Path) -> None:
        """Eight required-field errors become none; both misuses are filtered already."""

        payload = _generate(tmp_path, _degraded(unmeasured=("cardinality_method", *_LOST)))
        codes = {i.code for i in check(payload, "statistics.yaml", "seedbank.accession")}

        assert codes == set()
        # Both layers run over the same file in one `dbprint check`, so the schema has to
        # relax the classification's own required list wherever the marker is present.
        assert check_statistics(payload, "statistics.yaml") == []


def _generate(tmp_path: Path, column: ColumnStats) -> dict[str, Any]:
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
    )
    Engine(MockAdapter(_fixture(column)), conn, tmp_path).generate()

    return yaml.safe_load(
        (tmp_path / "w" / "seedbank" / "accession" / "statistics.yaml").read_text(),
    )


def _column(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["columns"]["logged_at"]


def _measured() -> ColumnStats:
    """A temporal column whose every read answered - the control the degrades vary from."""

    return ColumnStats(
        sql_type="timestamp",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=400,
        cardinality_ratio=1.0,
        cardinality_method="exact",
        values=(ValueCount(value="2025-12-31T00:00:00Z", count=400),),
        distribution="uniform",
        frequencies=Frequencies(top=400, bottom=400, listed=1, total=400),
        range=Range(min="2025-11-01T00:00:00Z", max="2025-12-31T00:00:00Z", span_days=60),
        percentiles={"p50": "2025-12-01T00:00:00Z"},
        quantized_count=60,
    )


def _degraded(*, unmeasured: tuple[str, ...]) -> ColumnStats:
    """The same column with the top-N outputs absent and `unmeasured` naming them."""

    return replace(
        _measured(),
        values=None,
        distribution=None,
        frequencies=None,
        unmeasured=unmeasured,
    )


def _fixture(column: ColumnStats) -> dict[str, MockTable]:
    return {
        "seedbank.accession": MockTable(
            type="table",
            namespace_path=("seedbank", "accession"),
            ddl="CREATE TABLE seedbank.accession (logged_at timestamp);\n",
            columns=[
                ColumnMeta(
                    name="logged_at",
                    sql_type="timestamp",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={"logged_at": column},
            samples={},
            row_count=400,
        ),
    }
