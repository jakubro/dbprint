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
    EMPTY_COLUMNS_TABLE,
    FUTURE_DATED_COLUMN,
    INCOMPLETE_GRAIN_TABLE,
    NEVER_DECLARED_KIND,
    REDACTED_COLUMN,
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
