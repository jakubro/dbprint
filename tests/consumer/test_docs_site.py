"""The docs site's rendering layer (`dbprint.docs.view`) against the shared claims register.

The view functions are the rendering rules the site must get right, so a claim against them
is a claim against the site.
"""

from __future__ import annotations

import html
import re

import yaml

from dbprint.config import ConnectionConfig
from dbprint.docs import catalogue, view, web
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
    PERCENTILE_INSIDE_RANGE_COLUMN,
    REDACTED_COLUMN,
    SCOPED_COMPLETE_LIST_COLUMN,
    SCOPED_KEY_COLUMN,
    SCOPED_LATEST_COLUMN,
    SCOPED_TABLE,
    SEVERAL_EDGES_TABLE,
    SPELLING_COLUMN,
    SPELLING_VALUES,
    TRUNCATED_FK_COLUMN,
    UNEVALUATED_TABLE,
    UNREADABLE_PROFILED_AT,
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
    assert scope["share"] == 0.25


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
    assert values["coverage_text"] == "40% covered"


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

    assert grain == {"key_list": [], "state": "bounded"}


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


def test_a_delimiter_in_a_value_survives_the_view(adversarial_print: AdversarialPrint) -> None:
    """Each bar is spelled on one line and loads back to exactly the stored literal."""

    statistics = _statistics(adversarial_print, DELIMITER_TABLE)
    assert statistics is not None

    values = view.values_view(statistics["columns"][DELIMITER_COLUMN])

    assert values is not None
    assert all("\n" not in bar["value"] for bar in values["bars"])
    assert {yaml.safe_load(bar["value"]) for bar in values["bars"]} == {
        DELIMITER_VALUE,
        LINE_BREAK_VALUE,
        GRAMMAR_VALUE,
    }


def test_scoped_complete_list_carries_the_clause(adversarial_print: AdversarialPrint) -> None:
    page = _page(adversarial_print, SCOPED_TABLE)
    column = next(c for c in page["columns"] if c["name"] == SCOPED_COMPLETE_LIST_COLUMN)

    assert column["value_list"]["coverage_text"] == "100% covered over the rows scanned"


def test_scoped_candidate_key_carries_the_clause(adversarial_print: AdversarialPrint) -> None:
    page = _page(adversarial_print, SCOPED_TABLE)
    column = next(c for c in page["columns"] if c["name"] == SCOPED_KEY_COLUMN)

    assert "candidate key over the rows scanned" in column["notes"]


def test_scoped_latest_value_carries_the_clause(adversarial_print: AdversarialPrint) -> None:
    page = _page(adversarial_print, SCOPED_TABLE)
    column = next(c for c in page["columns"] if c["name"] == SCOPED_LATEST_COLUMN)

    assert page["cards"]["freshest"]["clause"] == "over the rows scanned"
    assert column["range"]["freshness_clause"] == "over the rows scanned"


def test_an_unscoped_page_carries_no_clause(adversarial_print: AdversarialPrint) -> None:
    page = _page(adversarial_print, "public.cultivar")
    [column] = page["columns"]

    assert column["value_list"]["coverage_text"] == "100% covered"
    assert "over the rows scanned" not in column["notes"]


def _page(adversarial_print: AdversarialPrint, table: str) -> dict:
    found = catalogue.load_connections([_conn(adversarial_print)])[0]

    return view.build_table_view(found, _artifacts(adversarial_print, table))


def test_a_bar_is_spelled_so_it_reads_back_as_itself(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, DELIMITER_TABLE)
    assert statistics is not None

    values = view.values_view(statistics["columns"][SPELLING_COLUMN])

    assert values is not None
    assert [yaml.safe_load(bar["value"]) for bar in values["bars"]] == list(SPELLING_VALUES)
    assert all("\n" not in bar["value"] for bar in values["bars"])


def _page_text(adversarial_print: AdversarialPrint, table: str) -> str:
    client = web.create_app([_conn(adversarial_print)]).test_client()
    page = client.get(f"/t/{_conn(adversarial_print).name}/{table}").data.decode()
    page = re.sub(r"<(script|style)\b.*?</\1>|style=\"[^\"]*\"", " ", page, flags=re.DOTALL)

    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def test_an_extreme_statistic_is_spelled_as_the_artifact_spells_it(
    adversarial_print: AdversarialPrint,
) -> None:
    text = _page_text(adversarial_print, EXTREME_TABLE)

    assert EXPONENT_FORM.findall(text) == []
    assert "mean 18446744073709548000.0" in text
    assert "sum 0.00049" in text


def test_a_share_near_a_boundary_is_not_rounded_onto_it(
    adversarial_print: AdversarialPrint,
) -> None:
    text = _page_text(adversarial_print, EXTREME_TABLE)

    assert "99.98% covered" in text
    assert "99.96% of scanned rows" in text


def test_an_orphan_spelling_stays_its_own_value(adversarial_print: AdversarialPrint) -> None:
    column = next(
        c
        for c in _page(adversarial_print, ORPHAN_SPELLING_TABLE)["columns"]
        if c["name"] == ORPHAN_SPELLING_COLUMN
    )

    assert ORPHAN_SPELLING_VALUE in [bar["value"] for bar in column["value_list"]["bars"]]


def test_a_grain_search_without_outcome_reads_as_not_determined(
    adversarial_print: AdversarialPrint,
) -> None:
    grain = _page(adversarial_print, GRAIN_NO_OUTCOME_TABLE)["grain"]

    assert grain["state"] == "not_determined"


def test_an_unreadable_profiled_at_is_shown_as_written(adversarial_print: AdversarialPrint) -> None:
    text = " ".join(_page_text(adversarial_print, UNREADABLE_PROFILED_TABLE).split())

    assert f"profiled {UNREADABLE_PROFILED_AT}" in text
    assert " ago" not in text


def test_the_bounds_are_the_range_not_a_percentile(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    assert statistics is not None

    rng = view.range_view(statistics["columns"][PERCENTILE_INSIDE_RANGE_COLUMN])

    assert rng is not None
    assert (rng["bounds"]["min"], rng["bounds"]["max"]) == ("2010-03-01", "2014-03-01")
    assert ("p01", "2010-04-01") in rng["percentiles"]


def test_the_notes_name_every_standing_edge(adversarial_print: AdversarialPrint) -> None:
    text = _page_text(adversarial_print, SEVERAL_EDGES_TABLE)

    assert "FK: public.cultivar.id (declared), public.wide_lookup.a (measured)" in text
    assert "public.batch.id (inferred)" not in text.split("Relationships", 1)[0]
