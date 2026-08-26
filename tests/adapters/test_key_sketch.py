"""KMV key sketch cross-adapter agreement and determinism (SPEC 2.2.14).

Two producers must hash the same logical value to the same 64-bit integer or the sketches are
incomparable, so the same seeded edge is sketched across real Postgres, real MariaDB and the
duckdb Snowflake substrate, never a Postgres sketch compared to itself.
"""

from __future__ import annotations

from dbprint.adapters import Adapter
from dbprint.spec.sketch import K


# seedbank.curator.herbarium_id -> seedbank.herbarium.id, the contract schema's own declared
# FK: three curator rows, two herbaria, both UUID, identically seeded everywhere.
_CURATOR_TABLE_BY_VENDOR = {
    "postgres": "seedbank.curator",
    "mysql": "fixture.curator",
    "snowflake": "memory.seedbank.curator",
}
_HERBARIUM_TABLE_BY_VENDOR = {
    "postgres": "seedbank.herbarium",
    "mysql": "fixture.herbarium",
    "snowflake": "memory.seedbank.herbarium",
}


def _table_for(vendor: str, by_vendor: dict[str, str], adapter: Adapter) -> str:
    """The seeded table's own fqn, resolved against what `list_tables` reports: the three
    substrates spell it differently, and MySQL's database is per-test, not the guess above.
    """

    suffix = by_vendor[vendor].split(".")[-1]
    tables = adapter.list_tables(include=["*"], exclude=[])

    return next(t.fqn for t in tables if t.fqn.split(".")[-1] == suffix)


class TestCrossAdapterAgreement:
    """The same edge, sketched on three real substrates, must agree exactly."""

    def test_the_same_seeded_edge_sketches_identically_on_every_adapter(
        self,
        all_sql_adapters: dict[str, Adapter],
    ) -> None:
        sketches: dict[str, tuple[int, ...]] = {}

        for vendor, adapter in all_sql_adapters.items():
            curator_fqn = _table_for(vendor, _CURATOR_TABLE_BY_VENDOR, adapter)
            adapter.introspect_columns(curator_fqn)  # populates Snowflake's Identity.columns

            sketches[vendor] = adapter.compute_key_sketch(
                curator_fqn,
                "herbarium_id",
                "uuid",
                "text",
                K,
            )
            adapter.close()

        values = list(sketches.values())
        assert all(v == values[0] for v in values), f"adapters disagree: {sketches}"
        # Not vacuous: 3 curator reference 2 distinct herbaria, so an empty sketch everywhere
        # would pass the equality check above.
        assert len(values[0]) == 2

    def test_the_target_side_of_the_same_edge_also_agrees(
        self,
        all_sql_adapters: dict[str, Adapter],
    ) -> None:
        """The parent column, not just the child - both sides of one edge."""

        sketches: dict[str, tuple[int, ...]] = {}

        for vendor, adapter in all_sql_adapters.items():
            herbarium_fqn = _table_for(vendor, _HERBARIUM_TABLE_BY_VENDOR, adapter)
            adapter.introspect_columns(herbarium_fqn)

            sketches[vendor] = adapter.compute_key_sketch(herbarium_fqn, "id", "uuid", "text", K)
            adapter.close()

        values = list(sketches.values())
        assert all(v == values[0] for v in values), f"adapters disagree: {sketches}"
        assert len(values[0]) == 3


class TestDeterminism:
    """Two reads of unchanged data must return byte-identical sketches (SPEC 2.2.14)."""

    def test_repeated_reads_agree(self, all_sql_adapters: dict[str, Adapter]) -> None:
        for adapter in all_sql_adapters.values():
            fqn = next(
                t.fqn
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "herbarium"
            )
            adapter.introspect_columns(fqn)

            first = adapter.compute_key_sketch(fqn, "id", "uuid", "text", K)
            second = adapter.compute_key_sketch(fqn, "id", "uuid", "text", K)
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
            adapter.introspect_columns(curator_fqn)
            # `seed_count`: values 30/31/NULL, and SPEC 2.2.14 sketches distinct non-nulls only.
            sketches[vendor] = adapter.compute_key_sketch(
                curator_fqn,
                "seed_count",
                "integer",
                "integer",
                K,
            )
            adapter.close()

        values = list(sketches.values())
        assert all(v == values[0] for v in values), f"adapters disagree: {sketches}"
        assert len(values[0]) == 2
