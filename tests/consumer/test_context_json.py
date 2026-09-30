"""`dbprint context --format json` against the shared claims register.

The structured object is the raw `statistics.yaml` payload minus redaction, so claims check
the field a consumer reads is present and correct, not that prose narrates it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from dbprint.cli.main import main
from tests.fixtures.adversarial import (
    APPROXIMATE_ROW_COUNT_TABLE,
    DECLARED_MISSING_KIND,
    DECLARED_MISSING_TABLE,
    DELIMITER_COLUMN,
    DELIMITER_TABLE,
    DELIMITER_VALUE,
    EMPTY_COLUMNS_TABLE,
    EXTREME_NULL_RATE,
    EXTREME_TABLE,
    EXTREME_TINY_MEAN,
    EXTREME_WIDE_P50,
    FUTURE_DATED_COLUMN,
    INCOMPLETE_GRAIN_TABLE,
    LINE_BREAK_VALUE,
    NEVER_DECLARED_KIND,
    REDACTED_COLUMN,
    SCOPED_COMPLETE_LIST_COLUMN,
    SCOPED_KEY_COLUMN,
    SCOPED_LATEST_COLUMN,
    SCOPED_TABLE,
    SPELLING_COLUMN,
    SPELLING_VALUES,
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
        "value_spelling",
        "scoped_complete_list",
        "scoped_candidate_key",
        "scoped_latest_value",
        "extreme_number_statistics",
        "near_boundary_share",
    },
)


def _payload(adversarial_print: AdversarialPrint, table: str) -> dict:
    """The full structured object `dbprint context --format json` returns for `table`."""

    runner = CliRunner()
    old_cwd = Path.cwd()
    os.chdir(adversarial_print.conn.output.parent)

    try:
        result = runner.invoke(main, ["context", table, "--format", "json"])
    finally:
        os.chdir(old_cwd)

    assert result.exit_code == 0, result.output

    return json.loads(result.output)


def _statistics(adversarial_print: AdversarialPrint, table: str) -> dict:
    """The `statistics` object `dbprint context --format json` returns for `table`."""

    return _payload(adversarial_print, table)["statistics"]


def test_scoped_table_carries_the_population_alongside_every_ratio(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)

    assert statistics["scope"]["rows_scanned"] == 250
    assert statistics["row_count"] == 1000


def test_redacted_column_carries_no_real_literal(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    email = statistics["columns"][REDACTED_COLUMN]

    assert email["redacted"] == "mask"
    assert all(entry["value"] != "a@example.com" for entry in email["values"])


def test_future_dated_temporal_freshness_reads_live(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    shipped_at = statistics["columns"][FUTURE_DATED_COLUMN]

    assert shipped_at["freshness"]["classification"] == "live"
    assert shipped_at["freshness"]["max_age_days"] == 0


def test_truncated_fk_values_carry_their_own_coverage(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    region_id = statistics["columns"][TRUNCATED_FK_COLUMN]

    assert region_id["values"]
    assert region_id["values_coverage"] < 1.0


def test_unevaluated_diff_table_is_never_called_unchanged(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)

    assert "unchanged" not in json.dumps(statistics).lower()


def test_empty_columns_map_carries_the_zero_scan_marker(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, EMPTY_COLUMNS_TABLE)

    assert statistics["columns"] == {}
    assert statistics["scope"]["rows_scanned"] == 0


def test_approximate_row_count_carries_its_own_method(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, APPROXIMATE_ROW_COUNT_TABLE)

    assert statistics["row_count_method"] == "approximate"


def test_incomplete_grain_search_carries_exhausted_false(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, INCOMPLETE_GRAIN_TABLE)

    assert statistics["grain"]["search"]["exhausted"] is False


def test_catalog_only_table_carries_no_dependency_or_layout_key(
    adversarial_print: AdversarialPrint,
) -> None:
    """SPEC 2.2.15 forbids both under the marker; absence is licensed, not a producer gap."""

    statistics = _statistics(adversarial_print, UNEVALUATED_TABLE)

    assert statistics["catalog_only"] is True
    assert "physical_layout" not in statistics
    assert "dependencies" not in statistics


def test_declared_missing_artifact_is_named_not_conflated_with_never_declared(
    adversarial_print: AdversarialPrint,
) -> None:
    payload = _payload(adversarial_print, DECLARED_MISSING_TABLE)

    assert payload.get("_missing") == [DECLARED_MISSING_KIND]
    assert NEVER_DECLARED_KIND not in payload.get("_missing", [])


def test_a_delimiter_in_a_value_reaches_json_unescaped(
    adversarial_print: AdversarialPrint,
) -> None:
    """A machine format carries the literal, never the Markdown escaping a table needs."""

    statistics = _statistics(adversarial_print, DELIMITER_TABLE)
    values = [entry["value"] for entry in statistics["columns"][DELIMITER_COLUMN]["values"]]

    assert DELIMITER_VALUE in values
    assert LINE_BREAK_VALUE in values


def test_scoped_complete_list_rides_beside_the_top_level_scope(
    adversarial_print: AdversarialPrint,
) -> None:
    payload = _payload(adversarial_print, SCOPED_TABLE)

    assert payload["scope"]["rows_scanned"] == 250
    assert payload["statistics"]["columns"][SCOPED_COMPLETE_LIST_COLUMN]["values_coverage"] == 1.0


def test_scoped_candidate_key_rides_beside_the_top_level_scope(
    adversarial_print: AdversarialPrint,
) -> None:
    payload = _payload(adversarial_print, SCOPED_TABLE)

    assert payload["scope"]["sample"] == 0.25
    assert payload["statistics"]["columns"][SCOPED_KEY_COLUMN]["inferred"]["candidate_key"] is True


def test_scoped_latest_value_rides_beside_the_top_level_scope(
    adversarial_print: AdversarialPrint,
) -> None:
    payload = _payload(adversarial_print, SCOPED_TABLE)

    assert payload["row_count"] == 1000
    assert "freshness" in payload["statistics"]["columns"][SCOPED_LATEST_COLUMN]


def test_an_unscoped_payload_carries_no_scope(adversarial_print: AdversarialPrint) -> None:
    assert "scope" not in _payload(adversarial_print, "public.cultivar")


def test_a_value_needing_a_spelling_is_served_verbatim(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, DELIMITER_TABLE)
    values = [entry["value"] for entry in statistics["columns"][SPELLING_COLUMN]["values"]]

    assert values == list(SPELLING_VALUES)


def test_an_extreme_statistic_is_carried_unrounded(adversarial_print: AdversarialPrint) -> None:
    columns = _statistics(adversarial_print, EXTREME_TABLE)["columns"]

    assert columns["wide"]["percentiles"]["p50"] == EXTREME_WIDE_P50
    assert columns["tiny"]["mean"] == EXTREME_TINY_MEAN


def test_a_share_near_a_boundary_is_carried_unrounded(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, EXTREME_TABLE)

    assert statistics["columns"]["sparse"]["null_rate"] == EXTREME_NULL_RATE
    assert statistics["null_patterns"]["coverage"] == EXTREME_NULL_RATE
