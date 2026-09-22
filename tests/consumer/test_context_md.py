"""`dbprint context --format md` against the shared claims register.

Byte-level layout is tests/cli/test_context.py's job; this asserts the register's
per-state properties against the same rendering path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from click.testing import CliRunner

from dbprint.cli.main import main
from tests.fixtures.adversarial import (
    APPROXIMATE_ROW_COUNT_TABLE,
    DECLARED_MISSING_KIND,
    DECLARED_MISSING_TABLE,
    DELIMITER_TABLE,
    DELIMITER_VALUE,
    EMPTY_COLUMNS_TABLE,
    FUTURE_DATED_COLUMN,
    INCOMPLETE_GRAIN_TABLE,
    LINE_BREAK_VALUE,
    NEVER_DECLARED_KIND,
    REDACTED_COLUMN,
    REDACTED_PRIMITIVE,
    SCOPED_TABLE,
    TRUNCATED_FK_COLUMN,
    UNEVALUATED_TABLE,
    AdversarialPrint,
)


COVERS = frozenset(
    {
        "scoped_table",
        "redacted_column",
        "future_dated_temporal",
        "truncated_fk_values",
        "unevaluated_diff_table",
        "empty_columns_map",
        "approximate_row_count",
        "incomplete_grain_search",
        "catalog_only_table",
        "declared_missing_artifact",
        "delimiter_in_a_value",
    },
)


def _render(adversarial_print: AdversarialPrint, table: str) -> str:
    """The md fragment `dbprint context <table>` renders against the adversarial print."""

    runner = CliRunner()
    old_cwd = Path.cwd()
    os.chdir(adversarial_print.conn.output.parent)

    try:
        result = runner.invoke(main, ["context", table, "--format", "md"])
    finally:
        os.chdir(old_cwd)

    assert result.exit_code == 0, result.output

    return result.output


def _render_query(adversarial_print: AdversarialPrint, table: str) -> str:
    """The same fragment under `--purpose query`, which renders the value lists directly."""

    runner = CliRunner()
    old_cwd = Path.cwd()
    os.chdir(adversarial_print.conn.output.parent)

    try:
        result = runner.invoke(main, ["context", table, "--purpose", "query"])
    finally:
        os.chdir(old_cwd)

    assert result.exit_code == 0, result.output

    return result.output


def _value_row(text: str, column: str) -> str:
    """The `## Column values` row for `column`."""

    section = text.split("## Column values", 1)[-1]

    return next(line for line in section.splitlines() if line.startswith(f"| {column} |"))


def _cardinality_row(text: str, column: str) -> str:
    """The Cardinality & key columns table row for `column` - never the DDL, which also
    names every column and would otherwise satisfy a naive substring search."""

    return next(line for line in text.splitlines() if line.startswith(f"| {column} |"))


def test_scoped_table_states_the_population(adversarial_print: AdversarialPrint) -> None:
    text = _render(adversarial_print, SCOPED_TABLE)

    assert "Scanned: 250 of 1,000 rows (25.0%)" in text


def test_redacted_column_never_leaks_the_literal(adversarial_print: AdversarialPrint) -> None:
    text = _render(adversarial_print, SCOPED_TABLE)

    assert "a@example.com" not in text
    assert f"redacted ({REDACTED_PRIMITIVE})" in text


def test_future_dated_temporal_reads_live_not_stale(adversarial_print: AdversarialPrint) -> None:
    text = _render(adversarial_print, SCOPED_TABLE)
    shipped_at_row = _cardinality_row(text, FUTURE_DATED_COLUMN)

    assert "freshness live" in shipped_at_row


def test_truncated_fk_values_never_show_as_exhaustive(adversarial_print: AdversarialPrint) -> None:
    """The FK cell never renders the value list, so it never mis-caveats one either."""

    text = _render(adversarial_print, SCOPED_TABLE)
    region_line = _cardinality_row(text, TRUNCATED_FK_COLUMN)
    shows_a_literal_region_value = any(f"region-{i:02d}" in region_line for i in range(30))

    # Asserted as the current shape rather than guarded behind it: a conditional whose
    # branch never runs discharges the register entry without checking anything.
    assert not shows_a_literal_region_value, (
        "the FK cell now renders values; a truncated list needs its coverage caveat beside them"
    )


def test_unevaluated_diff_table_is_never_called_unchanged(
    adversarial_print: AdversarialPrint,
) -> None:
    """`dbprint context` renders a snapshot, never a diff, so it claims neither word."""

    text = _render(adversarial_print, SCOPED_TABLE)

    assert "unchanged" not in text.lower()


def test_empty_columns_map_states_nothing_was_read(adversarial_print: AdversarialPrint) -> None:
    text = _render(adversarial_print, EMPTY_COLUMNS_TABLE)

    assert "Scanned: 0 of 500 rows (0.0%)" in text
    assert "no columns" not in text.lower()


def test_approximate_row_count_never_narrates_growth(adversarial_print: AdversarialPrint) -> None:
    """A snapshot render computes no delta; narrating one needs the estimate labelled."""

    text = _render(adversarial_print, APPROXIMATE_ROW_COUNT_TABLE)

    for word in ("grew", "growth", "increased", "compared to", "delta"):
        assert word not in text.lower()


def test_incomplete_grain_search_reads_as_bounded_not_resolved(
    adversarial_print: AdversarialPrint,
) -> None:
    text = _render(adversarial_print, INCOMPLETE_GRAIN_TABLE)

    assert "Grain: search bounded, none found within the cap" in text
    assert "Grain: searched, none found" not in text


def test_catalog_only_table_renders_no_dependency_or_layout_claim(
    adversarial_print: AdversarialPrint,
) -> None:
    """Neither field was queried; rendering an empty one would claim a measurement."""

    text = _render(adversarial_print, UNEVALUATED_TABLE)

    assert "## Physical layout" not in text
    assert "Clustered by" not in text
    assert "Partitioned by" not in text


def test_declared_missing_artifact_is_named_not_conflated_with_never_declared(
    adversarial_print: AdversarialPrint,
) -> None:
    text = _render(adversarial_print, DECLARED_MISSING_TABLE)

    assert f"Missing: {DECLARED_MISSING_KIND}" in text
    assert NEVER_DECLARED_KIND not in text


class TestTheQueryPurposeHonoursTheSameRegister:
    """The value table states what the Notes summary used to state in prose."""

    def test_a_scoped_exhaustive_list_is_exhaustive_over_the_rows_scanned(
        self,
        adversarial_print: AdversarialPrint,
    ) -> None:
        text = _render_query(adversarial_print, SCOPED_TABLE)

        assert "Scanned: 250 of 1,000 rows (25.0%)" in text
        assert "the whole domain over the rows scanned" in text
        assert "1.0 - the list is the whole domain |" not in text

    def test_a_redacted_column_publishes_counts_and_no_literal(
        self,
        adversarial_print: AdversarialPrint,
    ) -> None:
        text = _render_query(adversarial_print, SCOPED_TABLE)

        assert "a@example.com" not in text
        assert f"values withheld ({REDACTED_PRIMITIVE})" in _value_row(text, REDACTED_COLUMN)

    def test_a_truncated_list_says_so_beside_the_values_it_shows(
        self,
        adversarial_print: AdversarialPrint,
    ) -> None:
        row = _value_row(_render_query(adversarial_print, SCOPED_TABLE), TRUNCATED_FK_COLUMN)

        assert "rank-00 (1)" in row
        assert "rank-05" not in row
        assert "0.0667 - a sample of the most frequent values" in row


def test_a_delimiter_in_a_value_does_not_split_a_row(adversarial_print: AdversarialPrint) -> None:
    """A pipe in a value would open a cell the header never declared."""

    fragment = _render(adversarial_print, DELIMITER_TABLE)
    rows = [l for l in fragment.splitlines() if l.startswith("|") and not set(l) <= set("|- ")]

    assert rows, "the fragment drew no table at all"
    assert all(len(_cells(row)) == 3 for row in rows), rows
    assert DELIMITER_VALUE.replace("|", "\\|") in fragment
    assert "\n".join(LINE_BREAK_VALUE.splitlines()) not in fragment


def _cells(row: str) -> list[str]:
    """The row's cells, splitting on delimiters a reader would act on, not escaped ones."""

    return [c for c in re.split(r"(?<!\\)\|", row.strip()) if c.strip()]
