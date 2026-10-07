"""MCP tool channel (`dispatch`) against the shared claims register.

In-process dispatch: this checks the tool layer does not corrupt or drop a claim on its way
out, not wire framing, which tests/mcp/test_server.py owns.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import yaml
from click.testing import CliRunner

from dbprint.cli.main import main
from dbprint.mcp import ServedConnections, dispatch
from dbprint.mcp.tools import TOOL_NAMES
from tests import _mcp_pages
from tests._grammar import split_outside_literals
from tests.fixtures.adversarial import (
    APPROXIMATE_ROW_COUNT_TABLE,
    DECLARED_MISSING_KIND,
    DECLARED_MISSING_TABLE,
    DELIMITER_TABLE,
    DELIMITER_VALUE,
    EMPTY_COLUMNS_TABLE,
    EXPONENT_FORM,
    EXTREME_NULL_RATE,
    EXTREME_TABLE,
    FUTURE_DATED_COLUMN,
    GRAIN_NO_OUTCOME_TABLE,
    INCOMPLETE_GRAIN_TABLE,
    NEVER_DECLARED_KIND,
    ORPHAN_SPELLING_COLUMN,
    ORPHAN_SPELLING_TABLE,
    ORPHAN_SPELLING_VALUE,
    PARTIAL_AS_WHOLE,
    PERCENTILE_INSIDE_RANGE_COLUMN,
    REDACTED_COLUMN,
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


def _state(adversarial_print: AdversarialPrint) -> ServedConnections:
    return ServedConnections(
        served={adversarial_print.conn.name: adversarial_print.conn},
        default=adversarial_print.conn.name,
    )


def _dict_result(adversarial_print: AdversarialPrint, name: str, arguments: dict) -> dict:
    """Dict-returning `dispatch`; md `get_table_context` is the only string (MCP.md 4.1)."""

    result = dispatch(_state(adversarial_print), name, arguments)
    assert isinstance(result, dict)

    return result


def _statistics(adversarial_print: AdversarialPrint, table: str) -> dict:
    arguments = {"table": table, "format": "json"}

    return _mcp_pages.merged(
        _mcp_pages.pages(_state(adversarial_print), "get_table_context", arguments),
    )["statistics"]


def _diff(adversarial_print: AdversarialPrint) -> dict:
    return _dict_result(adversarial_print, "get_diff", {})


def _md(adversarial_print: AdversarialPrint, table: str) -> str:
    arguments = {"table": table, "format": "md"}

    return _mcp_pages.joined(
        _mcp_pages.pages(_state(adversarial_print), "get_table_context", arguments),
    )


def test_scoped_table_carries_the_population(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)

    assert statistics["scope"]["rows_scanned"] == 250
    md = _md(adversarial_print, SCOPED_TABLE)

    assert "Scanned: 250 of 1000 rows (25%); sampled\n" in md
    assert "sample 0.25" not in md


def test_redacted_column_carries_no_real_literal(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    email = statistics["columns"][REDACTED_COLUMN]

    assert all(entry["value"] != "a@example.com" for entry in email["values"])
    assert "a@example.com" not in _md(adversarial_print, SCOPED_TABLE)


def test_resolve_value_refuses_a_redacted_column(adversarial_print: AdversarialPrint) -> None:
    """The lookup reads the same value list; a redacted one has no literal to resolve against."""

    result = _dict_result(
        adversarial_print,
        "resolve_value",
        {"table": SCOPED_TABLE, "column": REDACTED_COLUMN, "text": "a@example.com"},
    )

    assert (result["match"], result["exhaustive"]) == ("unavailable", False)
    assert "redacted" in result["reason"]
    assert (result["scope"]["rows_scanned"], result["row_count"]) == (250, 1000)
    assert "domain" not in result
    assert "a@example.com" not in repr({k: v for k, v in result.items() if k != "text"})


def test_future_dated_temporal_freshness_reads_live(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    shipped_at = statistics["columns"][FUTURE_DATED_COLUMN]

    assert shipped_at["freshness"]["classification"] == "live"


def test_truncated_fk_values_carry_their_own_coverage(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    region_id = statistics["columns"][TRUNCATED_FK_COLUMN]

    assert region_id["values"]
    assert region_id["values_coverage"] < 1.0


def test_unevaluated_diff_table_is_never_folded_into_unchanged(
    adversarial_print: AdversarialPrint,
) -> None:
    diff = _diff(adversarial_print)

    assert diff["summary"]["unevaluated_tables"] > 0
    assert diff["summary"]["unchanged_tables"] == 0


def test_empty_columns_map_carries_the_zero_scan_marker(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, EMPTY_COLUMNS_TABLE)

    assert statistics["columns"] == {}
    assert "Scanned: 0 of 500 rows (0%)" in _md(adversarial_print, EMPTY_COLUMNS_TABLE)
    assert "no columns" not in _md(adversarial_print, EMPTY_COLUMNS_TABLE).lower()


def test_approximate_row_count_carries_its_own_method(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, APPROXIMATE_ROW_COUNT_TABLE)

    assert statistics["row_count_method"] == "approximate"

    diff = _diff(adversarial_print)
    row_count_changes = [c for c in diff["changes"] if c.get("kind") == "table_row_count_changed"]

    # No delta exists in this fixture to mislabel; the property that would catch a
    # regression is that a delta, whenever one appears, always names both sides' method.
    for change in row_count_changes:
        assert "before_method" in change
        assert "after_method" in change


def test_incomplete_grain_search_carries_exhausted_false(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, INCOMPLETE_GRAIN_TABLE)

    assert statistics["grain"]["search"]["exhausted"] is False
    assert "Grain: search bounded, none found within the cap" in _md(
        adversarial_print,
        INCOMPLETE_GRAIN_TABLE,
    )


def test_catalog_only_table_carries_no_dependency_or_layout_claim(
    adversarial_print: AdversarialPrint,
) -> None:
    """SPEC 2.2.15 forbids both under the marker; absence is licensed, not a producer gap."""

    statistics = _statistics(adversarial_print, UNEVALUATED_TABLE)

    assert statistics["catalog_only"] is True
    assert "physical_layout" not in statistics
    assert "dependencies" not in statistics
    assert "Clustered by" not in _md(adversarial_print, UNEVALUATED_TABLE)
    assert "Partitioned by" not in _md(adversarial_print, UNEVALUATED_TABLE)


def test_declared_missing_artifact_is_named_not_conflated_with_never_declared(
    adversarial_print: AdversarialPrint,
) -> None:
    result = _dict_result(
        adversarial_print,
        "get_table_context",
        {"table": DECLARED_MISSING_TABLE, "format": "json"},
    )

    assert result.get("_missing") == [DECLARED_MISSING_KIND]
    assert DECLARED_MISSING_KIND not in result.get("_corrupted", {})

    md = _md(adversarial_print, DECLARED_MISSING_TABLE)

    assert f"Missing: {DECLARED_MISSING_KIND}" in md
    assert NEVER_DECLARED_KIND not in md


def test_a_delimiter_in_a_value_does_not_split_a_row(adversarial_print: AdversarialPrint) -> None:
    """The tool renders the same Markdown the CLI does, so it owes the same guarantee."""

    fragment = _md(adversarial_print, DELIMITER_TABLE)
    rows = [l for l in fragment.splitlines() if l.startswith("|") and not set(l) <= set("|- ")]

    assert rows, "the tool returned no table at all"
    assert all(
        len([c for c in re.split(r"(?<!\\)\|", row.strip()) if c.strip()]) == 3 for row in rows
    )
    assert DELIMITER_VALUE.replace("|", "\\|") in fragment


_SCOPE_EXEMPT_TOOLS = {
    "list_tables": "table-wide manifest fields only",
    "get_manifest": "table-wide manifest fields only",
    "get_diff": "comparability between reads, not a statement about the table",
    "get_reference": "reads no print",
}


def test_every_tool_is_exercised_by_the_sweep_or_exempt() -> None:
    called = _tools_called(ast.parse(Path(__file__).read_text(encoding="utf-8")))

    assert set(TOOL_NAMES) - called - set(_SCOPE_EXEMPT_TOOLS) == set()
    assert set(_SCOPE_EXEMPT_TOOLS) - set(TOOL_NAMES) == set(), "stale exemptions"


def test_the_coverage_sweep_sees_an_uncalled_tool() -> None:
    planted = ast.parse(
        "def _helper(p):\n"
        "    return _dict_result(p, 'search_columns', {})\n"
        "def _unused(p):\n"
        "    return dispatch(state, 'get_manifest', {})\n"
        "def test_a(p):\n"
        "    _helper(p)\n"
        "    _dict_result(p, 'resolve_value', {})\n",
    )

    assert _tools_called(planted) == {"search_columns", "resolve_value"}


def test_scoped_complete_list_resolves_as_the_scanned_domain(
    adversarial_print: AdversarialPrint,
) -> None:
    reply = _dict_result(
        adversarial_print,
        "resolve_value",
        {"table": SCOPED_TABLE, "column": SCOPED_COMPLETE_LIST_COLUMN, "text": "abandoned"},
    )

    assert reply["match"] == "none"
    assert reply["exhaustive"] is False
    assert reply["scope"]["rows_scanned"] == 250
    assert "the list is the whole domain over the rows scanned" in reply["sample_caveat"]


def test_a_scoped_stored_phrase_resolves_over_the_scanned_domain(
    adversarial_print: AdversarialPrint,
) -> None:
    reply = _dict_result(
        adversarial_print,
        "resolve_value",
        {"table": SCOPED_TABLE, "column": SCOPED_COMPLETE_LIST_COLUMN, "text": "SOWN"},
    )

    assert (reply["match"], reply["exhaustive"]) == ("stored", False)
    assert [s["value"] for s in reply["spellings"]] == ["sown"]
    assert (reply["scope"]["rows_scanned"], reply["row_count"]) == (250, 1000)
    assert "the list is the whole domain over the rows scanned" in reply["sample_caveat"]


def test_scoped_candidate_key_match_carries_the_scope(adversarial_print: AdversarialPrint) -> None:
    matches = _dict_result(adversarial_print, "search_columns", {"candidate_key": True})["matches"]
    key = next(m for m in matches if (m["table"], m["column"]) == (SCOPED_TABLE, SCOPED_KEY_COLUMN))

    assert key["scope"]["sample"] == 0.25
    assert "candidate key over the rows scanned" in _md(adversarial_print, SCOPED_TABLE)


def test_scoped_latest_value_carries_the_clause(adversarial_print: AdversarialPrint) -> None:
    row = next(
        line
        for line in _md(adversarial_print, SCOPED_TABLE).splitlines()
        if line.startswith(f"| {SCOPED_LATEST_COLUMN} |")
    )

    assert "freshness: dormant over the rows scanned" in row


def test_a_value_is_spelled_so_it_reads_back_as_itself(
    adversarial_print: AdversarialPrint,
) -> None:
    """A stored 'NULL' or '' printed raw reads as a null or as nothing; a long one folds."""

    row = next(
        line
        for line in _md(adversarial_print, DELIMITER_TABLE).splitlines()
        if line.startswith(f"| {SPELLING_COLUMN} |")
    )
    notes = [c for c in re.split(r"(?<!\\)\|", row.strip()) if c.strip()][-1].strip()
    facts = split_outside_literals(notes, "; ")
    listed = next(f.split(": ", 1)[1] for f in facts if f.startswith("values (complete"))
    entries = split_outside_literals(listed, ", ")

    assert [yaml.safe_load(re.sub(r" \([^()]*%[^()]*\)$", "", v)) for v in entries] == list(
        SPELLING_VALUES,
    )


def test_an_extreme_statistic_is_spelled_as_the_artifact_spells_it(
    adversarial_print: AdversarialPrint,
) -> None:
    text = _md(adversarial_print, EXTREME_TABLE)

    assert EXPONENT_FORM.findall(text) == []
    assert "mean: 0.00000005" in text


def test_a_share_near_a_boundary_is_not_rounded_onto_it(
    adversarial_print: AdversarialPrint,
) -> None:
    text = _md(adversarial_print, EXTREME_TABLE)
    partial = [line for line in text.splitlines() if line.startswith(("| status |", "| sparse |"))]

    assert [PARTIAL_AS_WHOLE.findall(line) for line in partial if "whole domain" not in line] == [
        [],
        [],
    ]
    assert "nulls: 99.96%" in text
    assert (
        _statistics(adversarial_print, EXTREME_TABLE)["columns"]["sparse"]["null_rate"]
        == EXTREME_NULL_RATE
    )


def _tools_called(module: ast.Module) -> set[str]:
    """Tool names the module's tests dispatch, directly or through the helpers they call."""

    functions = {f.name: f for f in module.body if isinstance(f, ast.FunctionDef)}
    called: set[str] = set()
    seen: set[str] = set()
    pending = [name for name in functions if name.startswith("test_")]

    while pending:
        name = pending.pop()

        if name in seen:
            continue

        seen.add(name)

        for call in (n for n in ast.walk(functions[name]) if isinstance(n, ast.Call)):
            callee = call.func.id if isinstance(call.func, ast.Name) else None
            called.update(
                a.value
                for a in call.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            )

            if callee in functions:
                pending.append(callee)

    return called & set(TOOL_NAMES)


def test_the_tool_serves_the_fragment_the_context_command_prints(
    adversarial_print: AdversarialPrint,
) -> None:
    runner = CliRunner()
    old_cwd = Path.cwd()
    os.chdir(adversarial_print.conn.output.parent)

    try:
        result = runner.invoke(main, ["context", SCOPED_TABLE, "--format", "md"])
    finally:
        os.chdir(old_cwd)

    arguments = {"table": SCOPED_TABLE, "format": "md"}
    pages = _mcp_pages.pages(_state(adversarial_print), "get_table_context", arguments)

    assert result.exit_code == 0, result.output
    assert [page + "\n" for page in pages] == [result.output]


def test_an_orphan_spelling_stays_its_own_value(adversarial_print: AdversarialPrint) -> None:
    row = next(
        line
        for line in _md(adversarial_print, ORPHAN_SPELLING_TABLE).splitlines()
        if line.startswith(f"| {ORPHAN_SPELLING_COLUMN} |")
    )

    assert ORPHAN_SPELLING_VALUE in row


def test_a_grain_search_without_outcome_reads_as_not_determined(
    adversarial_print: AdversarialPrint,
) -> None:
    assert "Grain: not determined" in _md(adversarial_print, GRAIN_NO_OUTCOME_TABLE)


def test_an_unreadable_profiled_at_reads_as_dormant(adversarial_print: AdversarialPrint) -> None:
    tables = _dict_result(adversarial_print, "list_tables", {"detail": True})["tables"]
    [entry] = [t for t in tables if t["table"] == UNREADABLE_PROFILED_TABLE]

    assert (entry["freshness"], entry["age_days"]) == ("dormant", None)


def test_a_percentile_band_never_reads_as_the_range(adversarial_print: AdversarialPrint) -> None:
    row = next(
        line
        for line in _md(adversarial_print, SCOPED_TABLE).splitlines()
        if line.startswith(f"| {PERCENTILE_INSIDE_RANGE_COLUMN} |")
    )

    assert "range: '2010-03-01' -> '2014-03-01' (1461 days); P1-P99: '2010-04-01' -> " in row


def test_every_standing_edge_is_named_surest_first(adversarial_print: AdversarialPrint) -> None:
    row = next(
        line
        for line in _md(adversarial_print, SEVERAL_EDGES_TABLE).splitlines()
        if line.startswith(f"| {SEVERAL_EDGES_COLUMN} |")
    )

    assert "FK: public.cultivar.id (declared), public.wide_lookup.a (measured); " in row
    assert REJECTED_EDGE_TARGET not in row
