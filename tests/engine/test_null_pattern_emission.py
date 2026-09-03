"""What the engine writes and renders for a table's null census (SPEC 2.2.10).

The measurement is the adapters'; these cover the half after it - the block reaching the
artifact in schema shape, an absent census staying absent, and `dbprint context` telling a
reader the combinations are observed rather than enforced. Fixtures borrow the shipped
print's real censuses, except a capped one, which nothing ships.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import (
    BaseStats,
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    MockAdapter,
    MockTable,
    NullPattern,
    NullPatterns,
    StatisticsConfig,
    TableCounts,
    TableScope,
)
from dbprint.config import ConnectionConfig
from dbprint.conformance.statistics import check
from dbprint.engine import Engine
from dbprint.engine.context_assembler import AssemblyOptions, assemble


ACCESSION_NULL_PATTERNS = NullPatterns(
    patterns=(
        NullPattern(columns=("storage_temperature_c",), count=2353),
        NullPattern(columns=("storage_temperature_c", "traits"), count=147),
    ),
    coverage=1.0,
    coverage_method="measured",
)

TAXON_NULL_PATTERNS = NullPatterns(
    patterns=(
        NullPattern(columns=(), count=296),
        NullPattern(columns=("parent_taxon_id",), count=4),
    ),
    coverage=1.0,
    coverage_method="measured",
)


class TestEmission:
    def test_the_block_carries_the_coverage_and_every_combination(self, tmp_path: Path) -> None:
        payload = _generate_accession(tmp_path, ACCESSION_NULL_PATTERNS)

        assert payload["null_patterns"] == {
            "coverage": 1.0,
            "coverage_method": "measured",
            "patterns": [
                {"columns": ["storage_temperature_c"], "count": 2353},
                {"columns": ["storage_temperature_c", "traits"], "count": 147},
            ],
        }

    def test_the_block_sits_between_the_file_head_and_the_columns(self, tmp_path: Path) -> None:
        """SPEC 2.2.1 lists it there, and a reader scanning the file expects it there."""

        _generate_accession(tmp_path, ACCESSION_NULL_PATTERNS)
        text = (tmp_path / "w" / "seedbank" / "accession" / "statistics.yaml").read_text()

        assert text.index("null_patterns:") < text.index("columns:")
        assert text.index("row_count:") < text.index("null_patterns:")

    def test_an_unmeasured_census_leaves_no_key_behind(self, tmp_path: Path) -> None:
        """Absence is the claim that no column carries a null - seedbank.vault's real shape."""

        payload = _generate_vault(tmp_path)

        assert "null_patterns" not in payload

    def test_coverage_method_reaches_the_artifact_when_present(self, tmp_path: Path) -> None:
        """SPEC 2.2.10: whether an untruncated census agreed with rows_scanned."""

        payload = _generate_accession(tmp_path, ACCESSION_NULL_PATTERNS)

        assert payload["null_patterns"]["coverage_method"] == "measured"


class _CensusFailingAdapter(MockAdapter):
    """Fails the grouped null scan, as a statement timeout on a wide table would."""

    def compute_null_patterns(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        scope: TableScope | None = None,
    ) -> NullPatterns | None:
        raise RuntimeError("simulated statement timeout")


class TestAFailedCensus:
    """An absent block asserts the table carries no nulls (SPEC 2.2.10), so a run whose scan
    failed says otherwise through the marker - and validates, where an unmarked absence does not.
    """

    def test_the_file_names_the_block_unmeasured(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            "seedbank.accession",
            _accession_fixture(ACCESSION_NULL_PATTERNS),
            adapter=_CensusFailingAdapter,
        )

        assert "null_patterns" not in payload
        assert payload["unmeasured"] == ["null_patterns"]

    def test_the_absent_block_raises_no_census_error(self, tmp_path: Path) -> None:
        """Without the marker: `stats.null-patterns-absent-with-nulls` on a blameless producer."""

        payload = _generate(
            tmp_path,
            "seedbank.accession",
            _accession_fixture(ACCESSION_NULL_PATTERNS),
            adapter=_CensusFailingAdapter,
        )
        codes = {i.code for i in check(payload, "statistics.yaml", "seedbank.accession")}

        assert "stats.null-patterns-absent-with-nulls" not in codes

        unmarked = {k: v for k, v in payload.items() if k != "unmeasured"}

        assert "stats.null-patterns-absent-with-nulls" in {
            i.code for i in check(unmarked, "statistics.yaml", "seedbank.accession")
        }


class TestTruncatedCoverageMethod:
    """A capped census - `coverage` short of 1.0 - carries no method (SPEC 2.2.10).

    Nothing shipped truncates, so this uses a structural fixture rather than a seedbank name
    for a shape it does not have.
    """

    def test_coverage_method_absent_leaves_no_key_behind(self, tmp_path: Path) -> None:
        census = NullPatterns(
            patterns=(NullPattern(columns=("b",), count=1),),
            coverage=0.5,
        )
        payload = _generate(tmp_path, "public.t", _structural_fixture(census))

        assert "coverage_method" not in payload["null_patterns"]


class TestContextRendering:
    def test_the_combinations_reach_the_markdown_a_model_reads(self, tmp_path: Path) -> None:
        text = _context_accession(tmp_path, ACCESSION_NULL_PATTERNS, fmt="md")

        assert "## Columns null on the same rows" in text
        assert "| 2,353 | storage_temperature_c |" in text
        assert "| 147 | storage_temperature_c, traits |" in text

    def test_the_fully_populated_rows_are_named_not_left_blank(self, tmp_path: Path) -> None:
        text = _context_taxon(tmp_path, TAXON_NULL_PATTERNS, fmt="md")

        assert "(none - fully populated)" in text

    def test_the_rendering_says_the_combinations_were_observed(self, tmp_path: Path) -> None:
        """A consumer must not read a pattern as a constraint the database enforces."""

        text = _context_accession(tmp_path, ACCESSION_NULL_PATTERNS, fmt="md")

        assert "Observed over every scanned row." in text

    def test_a_table_without_a_census_renders_no_section(self, tmp_path: Path) -> None:
        text = _context_vault(tmp_path, fmt="md")

        assert "null on the same rows" not in text

    def test_the_structured_formats_carry_the_block_whole(self, tmp_path: Path) -> None:
        """json/yaml pass the statistics through, so the census needs no second projection."""

        text = _context_accession(tmp_path, ACCESSION_NULL_PATTERNS, fmt="yaml")
        payload = yaml.safe_load(text)

        assert payload["statistics"]["null_patterns"]["coverage"] == 1.0


def _generate(
    tmp_path: Path,
    table_key: str,
    fixture: dict[str, MockTable],
    adapter: type[MockAdapter] = MockAdapter,
) -> dict[str, Any]:
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
    )
    Engine(adapter(fixture), conn, tmp_path).generate()
    schema, name = table_key.split(".")

    return yaml.safe_load((tmp_path / "w" / schema / name / "statistics.yaml").read_text())


def _context(tmp_path: Path, table_key: str, fixture: dict[str, MockTable], fmt: str) -> str:
    _generate(tmp_path, table_key, fixture)
    root = tmp_path / "w"
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())

    return assemble(
        manifest,
        root,
        [table_key],
        AssemblyOptions(format=fmt, include_ddl=False),
    ).text


def _generate_accession(tmp_path: Path, census: NullPatterns | None) -> dict[str, Any]:
    return _generate(tmp_path, "seedbank.accession", _accession_fixture(census))


def _generate_vault(tmp_path: Path) -> dict[str, Any]:
    return _generate(tmp_path, "seedbank.vault", _vault_fixture())


def _context_accession(tmp_path: Path, census: NullPatterns | None, fmt: str) -> str:
    return _context(tmp_path, "seedbank.accession", _accession_fixture(census), fmt)


def _context_taxon(tmp_path: Path, census: NullPatterns | None, fmt: str) -> str:
    return _context(tmp_path, "seedbank.taxon", _taxon_fixture(census), fmt)


def _context_vault(tmp_path: Path, fmt: str) -> str:
    return _context(tmp_path, "seedbank.vault", _vault_fixture(), fmt)


def _accession_fixture(census: NullPatterns | None) -> dict[str, MockTable]:
    """seedbank.accession's null-bearing columns.

    storage_temperature_c is unmeasured on every row; traits is opaque JSON absent on some.
    """

    return {
        "seedbank.accession": MockTable(
            type="table",
            namespace_path=("seedbank", "accession"),
            ddl=(
                "CREATE TABLE seedbank.accession (\n"
                "    accession_id bigint NOT NULL,\n"
                "    traits jsonb,\n"
                "    storage_temperature_c numeric(4,1)\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="accession_id",
                    sql_type="bigint",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(name="traits", sql_type="jsonb", nullable=True, default=None, ordinal=2),
                ColumnMeta(
                    name="storage_temperature_c",
                    sql_type="numeric(4,1)",
                    nullable=True,
                    default=None,
                    ordinal=3,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "accession_id": ColumnStats(
                    sql_type="bigint",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=2500,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                ),
                "traits": ColumnStats(
                    sql_type="jsonb",
                    nullable=True,
                    null_count=147,
                    null_rate=0.0588,
                    cardinality=161,
                    cardinality_ratio=0.0644,
                    cardinality_method="exact",
                ),
                "storage_temperature_c": ColumnStats(
                    sql_type="numeric(4,1)",
                    nullable=True,
                    null_count=2500,
                    null_rate=1.0,
                    cardinality=0,
                    cardinality_ratio=0.0,
                    cardinality_method="exact",
                    values=(),
                    values_coverage=1.0,
                    distribution="uniform",
                ),
            },
            samples={},
            null_patterns=census,
            row_count=2500,
        ),
    }


def _taxon_fixture(census: NullPatterns | None) -> dict[str, MockTable]:
    """seedbank.taxon's parent_taxon_id: null wherever a taxon has no parent."""

    return {
        "seedbank.taxon": MockTable(
            type="table",
            namespace_path=("seedbank", "taxon"),
            ddl=(
                "CREATE TABLE seedbank.taxon (\n"
                "    taxon_id integer NOT NULL,\n"
                "    parent_taxon_id integer\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="taxon_id",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="parent_taxon_id",
                    sql_type="integer",
                    nullable=True,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "taxon_id": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=300,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                ),
                "parent_taxon_id": ColumnStats(
                    sql_type="integer",
                    nullable=True,
                    null_count=4,
                    null_rate=0.013333,
                    cardinality=12,
                    cardinality_ratio=0.04,
                    cardinality_method="exact",
                ),
            },
            samples={},
            null_patterns=census,
            row_count=300,
        ),
    }


def _vault_fixture() -> dict[str, MockTable]:
    """seedbank.vault: every column is declared NOT NULL, so no census applies at all."""

    return {
        "seedbank.vault": MockTable(
            type="table",
            namespace_path=("seedbank", "vault"),
            ddl=(
                "CREATE TABLE seedbank.vault (\n"
                "    vault_id integer NOT NULL,\n"
                "    shelf_code character varying(8) NOT NULL\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="vault_id",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="shelf_code",
                    sql_type="character varying(8)",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "vault_id": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=8,
                    cardinality_ratio=0.166667,
                    cardinality_method="exact",
                ),
                "shelf_code": ColumnStats(
                    sql_type="character varying(8)",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=6,
                    cardinality_ratio=0.125,
                    cardinality_method="exact",
                ),
            },
            samples={},
            null_patterns=None,
            row_count=48,
        ),
    }


def _structural_fixture(census: NullPatterns) -> dict[str, MockTable]:
    """A placeholder shape for the one state nothing shipped exercises: a truncated census."""

    return {
        "public.t": MockTable(
            type="table",
            namespace_path=("public", "t"),
            ddl="CREATE TABLE public.t (a text, b text);\n",
            columns=[
                ColumnMeta(name="a", sql_type="text", nullable=True, default=None, ordinal=1),
                ColumnMeta(name="b", sql_type="text", nullable=True, default=None, ordinal=2),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "a": ColumnStats(
                    sql_type="text",
                    nullable=True,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=1,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                ),
                "b": ColumnStats(
                    sql_type="text",
                    nullable=True,
                    null_count=1,
                    null_rate=1.0,
                    cardinality=0,
                    cardinality_ratio=0.0,
                    cardinality_method="exact",
                ),
            },
            samples={},
            null_patterns=census,
            row_count=1,
        ),
    }
