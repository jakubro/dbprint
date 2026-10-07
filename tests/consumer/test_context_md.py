"""`dbprint context --format md` against the shared claims register.

Byte-level layout is tests/cli/test_context.py's job; this asserts the register's
per-state properties against the same rendering path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from click.testing import CliRunner

from dbprint.cli.main import main
from tests._grammar import split_outside_literals
from tests.fixtures.adversarial import (
    APPROXIMATE_ROW_COUNT_TABLE,
    DECLARED_MISSING_KIND,
    DECLARED_MISSING_TABLE,
    DELIMITER_COLUMN,
    DELIMITER_TABLE,
    DELIMITER_VALUE,
    EMPTY_COLUMNS_TABLE,
    EXPONENT_FORM,
    EXTREME_TABLE,
    FUTURE_DATED_COLUMN,
    GRAIN_NO_OUTCOME_TABLE,
    GRAMMAR_VALUE,
    INCOMPLETE_GRAIN_TABLE,
    LINE_BREAK_VALUE,
    NEVER_DECLARED_KIND,
    ORPHAN_SPELLING_COLUMN,
    ORPHAN_SPELLING_TABLE,
    ORPHAN_SPELLING_VALUE,
    PARTIAL_AS_WHOLE,
    PERCENTILE_INSIDE_RANGE_COLUMN,
    REDACTED_COLUMN,
    REDACTED_PRIMITIVE,
    REJECTED_EDGE_TARGET,
    SCOPED_COMPLETE_LIST_COLUMN,
    SCOPED_KEY_COLUMN,
    SCOPED_LATEST_COLUMN,
    SCOPED_TABLE,
    SEVERAL_EDGES_COLUMN,
    SEVERAL_EDGES_TABLE,
    SPELLING_COLUMN,
    SPELLING_VALUES,
    TRUNCATED_FK_COLUMN,
    UNEVALUATED_TABLE,
    UNREADABLE_PROFILED_TABLE,
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
        "column_with_several_edges",
        "orphan_spelling",
        "percentile_inside_range",
        "grain_search_without_outcome",
        "unreadable_profiled_at",
        "approximate_row_count",
        "incomplete_grain_search",
        "catalog_only_table",
        "declared_missing_artifact",
        "delimiter_in_a_value",
        "value_spelling",
        "scoped_complete_list",
        "scoped_candidate_key",
        "scoped_latest_value",
        "extreme_number_statistics",
        "near_boundary_share",
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

    assert "Scanned: 250 of 1000 rows (25%); sampled\n" in text
    assert "sample 0.25" not in text


def test_redacted_column_never_leaks_the_literal(adversarial_print: AdversarialPrint) -> None:
    text = _render(adversarial_print, SCOPED_TABLE)

    assert "a@example.com" not in text
    assert f"redacted: {REDACTED_PRIMITIVE}" in text


def test_future_dated_temporal_reads_live_not_stale(adversarial_print: AdversarialPrint) -> None:
    text = _render(adversarial_print, SCOPED_TABLE)
    shipped_at_row = _cardinality_row(text, FUTURE_DATED_COLUMN)

    assert "freshness: live" in shipped_at_row


def test_truncated_fk_values_never_show_as_exhaustive(adversarial_print: AdversarialPrint) -> None:
    """The FK cell shows its list with the caveat that it is the top five, never complete."""

    region_line = _cardinality_row(_render(adversarial_print, SCOPED_TABLE), TRUNCATED_FK_COLUMN)

    assert re.search(r"values \(top 5, covering [\d.]+%\): 'rank-00' \(", region_line)
    assert "complete" not in region_line


def test_unevaluated_diff_table_is_never_called_unchanged(
    adversarial_print: AdversarialPrint,
) -> None:
    """`dbprint context` renders a snapshot, never a diff, so it claims neither word."""

    text = _render(adversarial_print, SCOPED_TABLE)

    assert "unchanged" not in text.lower()


def test_empty_columns_map_states_nothing_was_read(adversarial_print: AdversarialPrint) -> None:
    text = _render(adversarial_print, EMPTY_COLUMNS_TABLE)

    assert "Scanned: 0 of 500 rows (0%)" in text
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

        assert "Scanned: 250 of 1000 rows (25%)" in text
        assert "the whole domain over the rows scanned" in text
        assert "1.0 - the list is the whole domain |" not in text

    def test_a_redacted_column_publishes_counts_and_no_literal(
        self,
        adversarial_print: AdversarialPrint,
    ) -> None:
        text = _render_query(adversarial_print, SCOPED_TABLE)

        assert "a@example.com" not in text
        assert f"redacted: {REDACTED_PRIMITIVE}; values: withheld (" in _value_row(
            text,
            REDACTED_COLUMN,
        )

    def test_a_truncated_list_says_so_beside_the_values_it_shows(
        self,
        adversarial_print: AdversarialPrint,
    ) -> None:
        row = _value_row(_render_query(adversarial_print, SCOPED_TABLE), TRUNCATED_FK_COLUMN)

        assert "'rank-00' (1)" in row
        assert "rank-05" not in row
        assert "6.7% - a sample of the most frequent values" in row


def test_a_delimiter_in_a_value_does_not_split_a_row(adversarial_print: AdversarialPrint) -> None:
    """A pipe in a value would open a cell the header never declared."""

    fragment = _render(adversarial_print, DELIMITER_TABLE)
    rows = [l for l in fragment.splitlines() if l.startswith("|") and not set(l) <= set("|- ")]

    assert rows, "the fragment drew no table at all"
    assert all(len(_cells(row)) == 3 for row in rows), rows
    assert DELIMITER_VALUE.replace("|", "\\|") in fragment
    assert "\n".join(LINE_BREAK_VALUE.splitlines()) not in fragment


def test_a_separator_in_a_value_does_not_split_a_fact_or_a_list_entry(
    adversarial_print: AdversarialPrint,
) -> None:
    """The Notes cell and the query values cell each read back to the stored literals."""

    profile = _cardinality_row(_render(adversarial_print, DELIMITER_TABLE), DELIMITER_COLUMN)
    notes = _cells(profile)[-1].strip().replace("\\|", "|")
    values = next(
        f.split(": ", 1)[1]
        for f in split_outside_literals(notes, "; ")
        if f.startswith("values (complete")
    )
    query = _cardinality_row(_render_query(adversarial_print, DELIMITER_TABLE), DELIMITER_COLUMN)
    entries = split_outside_literals(_cells(query)[1].strip().replace("\\|", "|"), ", ")

    listed = [re.sub(r" \([^()]*%[^()]*\)$", "", v) for v in split_outside_literals(values, ", ")]

    assert GRAMMAR_VALUE in [yaml.safe_load(v) for v in listed]
    assert GRAMMAR_VALUE in [yaml.safe_load(re.sub(r" \(\d+\)$", "", v)) for v in entries]


def _cells(row: str) -> list[str]:
    """The row's cells, splitting on delimiters a reader would act on, not escaped ones."""

    return [c for c in re.split(r"(?<!\\)\|", row.strip()) if c.strip()]


def test_scoped_complete_list_carries_the_clause(adversarial_print: AdversarialPrint) -> None:
    row = _cardinality_row(_render(adversarial_print, SCOPED_TABLE), SCOPED_COMPLETE_LIST_COLUMN)

    assert "values (complete over the rows scanned): " in row


def test_scoped_candidate_key_carries_the_clause(adversarial_print: AdversarialPrint) -> None:
    row = _cardinality_row(_render(adversarial_print, SCOPED_TABLE), SCOPED_KEY_COLUMN)

    assert "candidate key over the rows scanned" in row


def test_scoped_latest_value_carries_the_clause(adversarial_print: AdversarialPrint) -> None:
    row = _cardinality_row(_render(adversarial_print, SCOPED_TABLE), SCOPED_LATEST_COLUMN)

    assert "freshness: dormant over the rows scanned" in row


def test_an_unscoped_table_carries_no_clause(adversarial_print: AdversarialPrint) -> None:
    assert "over the rows scanned" not in _render(adversarial_print, "public.cultivar")


def test_a_value_is_spelled_so_it_reads_back_as_itself(
    adversarial_print: AdversarialPrint,
) -> None:
    """A stored 'NULL' or '' printed raw reads as a null or as nothing; a long one folds."""

    row = _cardinality_row(_render(adversarial_print, DELIMITER_TABLE), SPELLING_COLUMN)
    facts = split_outside_literals(_cells(row)[-1].strip(), "; ")
    listed = next(f.split(": ", 1)[1] for f in facts if f.startswith("values (complete"))
    entries = split_outside_literals(listed, ", ")

    assert [yaml.safe_load(re.sub(r" \([^()]*%[^()]*\)$", "", v)) for v in entries] == list(
        SPELLING_VALUES,
    )


def test_a_value_is_spelled_the_same_way_under_purpose_query(
    adversarial_print: AdversarialPrint,
) -> None:
    row = _cardinality_row(_render_query(adversarial_print, DELIMITER_TABLE), SPELLING_COLUMN)
    listed = split_outside_literals(_cells(row)[1].strip(), ", ")

    assert [yaml.safe_load(re.sub(r" \(\d+\)$", "", v)) for v in listed] == list(SPELLING_VALUES)


def test_an_extreme_statistic_is_spelled_as_the_artifact_spells_it(
    adversarial_print: AdversarialPrint,
) -> None:
    text = _render(adversarial_print, EXTREME_TABLE) + _render_query(
        adversarial_print,
        EXTREME_TABLE,
    )

    assert EXPONENT_FORM.findall(text) == []
    assert "P50: 18446744073709548000.0" in text
    assert "range: 0.000000001 -> 0.000000097; P50: 0.000000049; mean: 0.00000005" in text


def test_a_share_near_a_boundary_is_not_rounded_onto_it(
    adversarial_print: AdversarialPrint,
) -> None:
    text = _render(adversarial_print, EXTREME_TABLE) + _render_query(
        adversarial_print,
        EXTREME_TABLE,
    )

    partial = [line for line in text.splitlines() if line.startswith(("| status |", "| sparse |"))]

    assert [PARTIAL_AS_WHOLE.findall(line) for line in partial if "whole domain" not in line] == [
        [],
        [],
        [],
    ]
    assert "nulls: 99.96%" in text
    assert "99.96% of scanned rows" in text
    assert "'bad' (0.02%)" in text


def test_an_orphan_spelling_stays_its_own_value(adversarial_print: AdversarialPrint) -> None:
    profile = _cardinality_row(
        _render(adversarial_print, ORPHAN_SPELLING_TABLE),
        ORPHAN_SPELLING_COLUMN,
    )
    query = _value_row(
        _render_query(adversarial_print, ORPHAN_SPELLING_TABLE),
        ORPHAN_SPELLING_COLUMN,
    )

    assert ORPHAN_SPELLING_VALUE in profile
    assert ORPHAN_SPELLING_VALUE in query


def test_a_grain_search_without_outcome_reads_as_not_determined(
    adversarial_print: AdversarialPrint,
) -> None:
    assert "Grain: not determined" in _render(adversarial_print, GRAIN_NO_OUTCOME_TABLE)


def test_an_unreadable_profiled_at_is_never_called_fresh(
    adversarial_print: AdversarialPrint,
) -> None:
    text = _render(adversarial_print, UNREADABLE_PROFILED_TABLE).casefold()

    assert UNREADABLE_PROFILED_TABLE in text
    assert not {"live", "fresh"} & set(re.findall(r"[a-z]+", text))


def test_a_percentile_band_never_reads_as_the_range(adversarial_print: AdversarialPrint) -> None:
    row = _cardinality_row(_render(adversarial_print, SCOPED_TABLE), PERCENTILE_INSIDE_RANGE_COLUMN)

    assert "range: '2010-03-01' -> '2014-03-01' (1461 days); P1-P99: '2010-04-01' -> " in row


def test_every_standing_edge_is_named_surest_first(adversarial_print: AdversarialPrint) -> None:
    row = _cardinality_row(_render(adversarial_print, SEVERAL_EDGES_TABLE), SEVERAL_EDGES_COLUMN)

    assert "FK: public.cultivar.id (declared), public.wide_lookup.a (measured); " in row
    assert REJECTED_EDGE_TARGET not in row
