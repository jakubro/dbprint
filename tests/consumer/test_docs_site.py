"""The docs site's rendering layer (`dbprint.docs.view`) against the shared claims register.

The view functions are the rendering rules the site must get right, so a claim against them
is a claim against the site.
"""

from __future__ import annotations

from dbprint.config import ConnectionConfig
from dbprint.docs import catalogue, view
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


def _conn(adversarial_print: AdversarialPrint) -> ConnectionConfig:
    return adversarial_print.conn


def _artifacts(adversarial_print: AdversarialPrint, table: str) -> catalogue.TableArtifacts:
    found = catalogue.load_connections([_conn(adversarial_print)])[0]
    artifacts = catalogue.load_table(found, table)
    assert artifacts is not None

    return artifacts


def _statistics(adversarial_print: AdversarialPrint, table: str) -> dict | None:
    return _artifacts(adversarial_print, table).statistics


def test_scoped_table_states_the_population(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    assert statistics is not None

    scope = view.scope_view(statistics)

    assert scope is not None
    assert scope["rows_scanned"] == 250
    assert scope["row_count"] == 1000
    assert scope["share_pct"] == 25.0


def test_redacted_column_carries_no_real_literal(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    assert statistics is not None

    values = view.values_view(statistics["columns"][REDACTED_COLUMN])

    assert values is not None
    assert all(bar["value"] != "a@example.com" for bar in values["bars"])


def test_future_dated_temporal_freshness_reads_live(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    assert statistics is not None

    rng = view.range_view(statistics["columns"][FUTURE_DATED_COLUMN])

    assert rng is not None
    assert rng["freshness"]["classification"] == "live"
    assert rng["freshness"]["max_age_days"] == 0


def test_truncated_fk_values_are_never_flagged_exhaustive(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    assert statistics is not None

    values = view.values_view(statistics["columns"][TRUNCATED_FK_COLUMN])

    assert values is not None
    assert values["exhaustive"] is False
    assert values["coverage"] < 1.0


def test_unevaluated_diff_table_carries_no_statistics_to_call_unchanged(
    adversarial_print: AdversarialPrint,
) -> None:
    """No view function renders `diff.yaml`.

    A table the diff never evaluated is therefore never rendered as though it did.
    """

    statistics = _statistics(adversarial_print, UNEVALUATED_TABLE)

    assert statistics is not None
    assert statistics["catalog_only"] is True
    assert view.scope_view(statistics) is None


def test_empty_columns_map_says_the_scan_read_nothing(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, EMPTY_COLUMNS_TABLE)

    notice = view.columns_empty_notice(statistics)

    assert (
        notice == "No columns were read - the scoped read that produced this print matched no rows."
    )


def test_approximate_row_count_carries_its_own_method(adversarial_print: AdversarialPrint) -> None:
    artifacts = _artifacts(adversarial_print, APPROXIMATE_ROW_COUNT_TABLE)
    assert artifacts.statistics is not None

    row_count = view.row_count_view(artifacts.entry, artifacts.statistics)

    assert row_count["method"] == "approximate"


def test_incomplete_grain_search_reads_as_bounded_not_resolved(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, INCOMPLETE_GRAIN_TABLE)
    assert statistics is not None

    grain = view.grain_view(statistics)

    assert grain == {"key_list": [], "search_ran": True, "exhausted": False}


def test_catalog_only_table_renders_no_dependency_or_layout_claim(
    adversarial_print: AdversarialPrint,
) -> None:
    """Neither field was queried; rendering an empty one would claim a measurement."""

    statistics = _statistics(adversarial_print, UNEVALUATED_TABLE)
    assert statistics is not None
    assert statistics["catalog_only"] is True

    assert view.physical_layout_view(statistics) is None
    assert view.dependencies_view(statistics) == []


def test_declared_missing_artifact_is_named_and_distinguished(
    adversarial_print: AdversarialPrint,
) -> None:
    artifacts = _artifacts(adversarial_print, DECLARED_MISSING_TABLE)

    assert artifacts.missing == (DECLARED_MISSING_KIND,)

    notice = view.missing_artifacts_notice(artifacts.missing)

    assert notice is not None
    assert DECLARED_MISSING_KIND in notice
    assert NEVER_DECLARED_KIND not in notice
