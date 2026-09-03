"""KMV key sketch cross-adapter agreement and determinism (SPEC 2.2.14) - two producers must
hash one logical value identically, so the same edge is sketched on every real substrate.
"""

from __future__ import annotations

from dbprint.adapters import Adapter
from dbprint.spec.sketch import K, SketchKind, sketch_kind


# seedbank.curator.herbarium_id -> seedbank.herbarium.id, the contract schema's own declared
# FK: three curator rows, two herbaria, both UUID, identically seeded everywhere.
_CURATOR_TABLE_BY_VENDOR = {
    "postgres": "seedbank.curator",
    "mysql": "fixture.curator",
    "snowflake": "memory.seedbank.curator",
    "duckdb": "memory.seedbank.curator",
    "clickhouse": "seedbank.curator",
    "redshift": "seedbank.curator",
    "databricks": "seedbank.curator",
    "bigquery": "seedbank.curator",
}
_HERBARIUM_TABLE_BY_VENDOR = {
    "postgres": "seedbank.herbarium",
    "mysql": "fixture.herbarium",
    "snowflake": "memory.seedbank.herbarium",
    "duckdb": "memory.seedbank.herbarium",
    "clickhouse": "seedbank.herbarium",
    "redshift": "seedbank.herbarium",
    "databricks": "seedbank.herbarium",
    "bigquery": "seedbank.herbarium",
}


def _table_for(vendor: str, by_vendor: dict[str, str], adapter: Adapter) -> str:
    """The seeded table's own fqn, resolved against what `list_tables` reports: the three
    substrates spell it differently, and MySQL's database is per-test, not the guess above.
    """

    suffix = by_vendor[vendor].split(".")[-1]
    tables = adapter.list_tables(include=["*"], exclude=[])

    return next(t.fqn for t in tables if t.fqn.split(".")[-1] == suffix)


def _sql_type_and_kind(adapter: Adapter, fqn: str, column: str) -> tuple[str, SketchKind]:
    """The column's real catalog spelling and the kind `sketch_kind` maps it to - exercised against
    what an adapter reports, not a literal chosen to pass. Also fills Snowflake's column cache.
    """

    col = next(c for c in adapter.introspect_columns(fqn) if c.name == column)
    kind = sketch_kind(col.sql_type)
    assert kind is not None, f"{fqn}.{column} ({col.sql_type!r}) has no sketch kind"

    return col.sql_type, kind


def _assert_agrees(sketches: dict[str, tuple[int, ...]], expected_len: int) -> None:
    """Bit-exact agreement across every adapter."""

    values = list(sketches.values())
    assert all(v == values[0] for v in values), f"adapters disagree: {sketches}"
    assert len(values[0]) == expected_len


class TestCrossAdapterAgreement:
    """The same edge, sketched on every real substrate, must agree exactly."""

    def test_the_same_seeded_edge_sketches_identically_on_every_adapter(
        self,
        all_sql_adapters: dict[str, Adapter],
    ) -> None:
        sketches: dict[str, tuple[int, ...]] = {}

        for vendor, adapter in all_sql_adapters.items():
            curator_fqn = _table_for(vendor, _CURATOR_TABLE_BY_VENDOR, adapter)
            sql_type, kind = _sql_type_and_kind(adapter, curator_fqn, "herbarium_id")

            sketches[vendor] = adapter.compute_key_sketch(
                curator_fqn,
                "herbarium_id",
                sql_type,
                kind,
                K,
            )
            adapter.close()

        # Not vacuous: 3 curator reference 2 distinct herbaria, so an empty sketch everywhere
        # would pass the equality check.
        _assert_agrees(sketches, expected_len=2)

    def test_the_target_side_of_the_same_edge_also_agrees(
        self,
        all_sql_adapters: dict[str, Adapter],
    ) -> None:
        """The parent column, not just the child - both sides of one edge."""

        sketches: dict[str, tuple[int, ...]] = {}

        for vendor, adapter in all_sql_adapters.items():
            herbarium_fqn = _table_for(vendor, _HERBARIUM_TABLE_BY_VENDOR, adapter)
            sql_type, kind = _sql_type_and_kind(adapter, herbarium_fqn, "id")

            sketches[vendor] = adapter.compute_key_sketch(herbarium_fqn, "id", sql_type, kind, K)
            adapter.close()

        _assert_agrees(sketches, expected_len=3)


class TestDeterminism:
    """Two reads of unchanged data must return byte-identical sketches (SPEC 2.2.14)."""

    def test_repeated_reads_agree(self, all_sql_adapters: dict[str, Adapter]) -> None:
        for adapter in all_sql_adapters.values():
            fqn = next(
                t.fqn
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "herbarium"
            )
            sql_type, kind = _sql_type_and_kind(adapter, fqn, "id")

            first = adapter.compute_key_sketch(fqn, "id", sql_type, kind, K)
            second = adapter.compute_key_sketch(fqn, "id", sql_type, kind, K)
            adapter.close()

            assert first == second


class TestIntegerKindAlsoAgrees:
    """UUID/text is the common case; an integer join key must agree too."""

    def test_the_integer_kind_sketches_identically_on_every_adapter(
        self,
        all_sql_adapters: dict[str, Adapter],
    ) -> None:
        sketches: dict[str, tuple[int, ...]] = {}

        for vendor, adapter in all_sql_adapters.items():
            curator_fqn = _table_for(vendor, _CURATOR_TABLE_BY_VENDOR, adapter)
            sql_type, kind = _sql_type_and_kind(adapter, curator_fqn, "seed_count")
            # `seed_count`: values 30/31/NULL, and SPEC 2.2.14 sketches distinct non-nulls only.
            sketches[vendor] = adapter.compute_key_sketch(
                curator_fqn,
                "seed_count",
                sql_type,
                kind,
                K,
            )
            adapter.close()

        _assert_agrees(sketches, expected_len=2)
