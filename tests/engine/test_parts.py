"""What a column holds, published under it as `parts` (SPEC 2.2.18), through every consumer."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters import MockAdapter
from dbprint.adapters.mock import MockParts
from dbprint.config import StatisticsConfig
from dbprint.config.project import ConnectionConfig, RedactRule
from dbprint.conformance import validate_print
from dbprint.docs.view import parts_view
from dbprint.engine import Engine
from dbprint.engine import diff as diff_module
from dbprint.engine.carried import CommittedPrint, redaction_mismatches
from dbprint.engine.context_assembler import AssemblyOptions, assemble
from dbprint.mcp.server import ServedConnections
from dbprint.mcp.tools import _tool_resolve_value, _tool_search_columns
from tests.engine._parts import attrs_parts, attrs_with_contact, many_parts, order_line


_SALT = "parts-test-salt"


def test_an_array_of_records_publishes_each_part_over_its_occurrences(tmp_path: Path) -> None:
    items = _columns(tmp_path, order_line())["items"]
    parts = items["parts"]

    assert items["classification"] == "composite"
    assert items["parts_found"] == 3
    assert items["size"] == {"min": 1, "max": 5, "avg": 2.5, "p95": 5.0}
    assert list(parts) == ["[*]", "[*].qty", "[*].sku"]
    assert parts["[*]"]["classification"] == "composite"
    assert parts["[*].sku"]["classification"] == "categorical"
    assert [v["value"] for v in parts["[*].sku"]["values"]] == ["SKU-A", "SKU-B", "SKU-C"]
    assert parts["[*].qty"]["classification"] == "numeric"
    assert parts["[*].qty"]["range"] == {"min": 1, "max": 60}
    assert parts["[*].qty"]["cardinality_ratio"] == 0.24  # noqa: RUF069 - the expected value is an exact literal
    assert parts["[*].qty"]["occurrences"] == 250
    assert not {"nullable", "rows_scanned", "sketch"} & set(parts["[*].sku"])


def test_a_document_stays_json_and_quotes_a_key_that_needs_it(tmp_path: Path) -> None:
    attrs = _columns(tmp_path, order_line())["attrs"]

    assert attrs["classification"] == "json"
    assert attrs["cardinality"] == 40
    assert attrs["parts"][".status"]["classification"] == "categorical"
    assert '["user-id"]' in attrs["parts"]


def test_a_map_lists_its_key_set_and_profiles_one_keys_value(tmp_path: Path) -> None:
    labels = _columns(tmp_path, order_line())["labels"]

    assert [v["value"] for v in labels["parts"]["[keys]"]["values"]] == ["unit", "origin"]
    assert labels["parts"][".unit"]["occurrences"] == 100


def test_the_cap_keeps_the_most_frequent_parts_and_counts_every_one(tmp_path: Path) -> None:
    columns = _columns(
        tmp_path,
        order_line(attrs=many_parts(30)),
        statistics=StatisticsConfig(max_parts=5),
    )

    assert list(columns["attrs"]["parts"]) == [".k00", ".k01", ".k02", ".k03", ".k04"]
    assert columns["attrs"]["parts_found"] == 30


def test_max_parts_zero_turns_descent_off(tmp_path: Path) -> None:
    columns = _columns(tmp_path, order_line(), statistics=StatisticsConfig(max_parts=0))

    assert columns["items"]["classification"] == "unsupported"
    assert "parts" not in columns["items"]
    assert "parts" not in columns["attrs"]


def test_a_failed_descent_leaves_the_column_undescended_and_the_table_profiled(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = order_line(items=MockParts(failure=RuntimeError("descent refused")))

    with caplog.at_level(logging.WARNING):
        columns = _columns(tmp_path, fixture)

    assert columns["items"]["classification"] == "unsupported"
    assert "parts" not in columns["items"]
    assert columns["attrs"]["parts"]
    assert any("descent refused" in r.getMessage() for r in caplog.records)


def test_a_rule_covering_the_column_marks_every_part_and_not_the_column(tmp_path: Path) -> None:
    columns = _columns(
        tmp_path,
        order_line(),
        redact=(RedactRule(columns=("*.order_line.attrs",), with_="mask"),),
    )
    parts = columns["attrs"]["parts"]

    assert "redacted" not in columns["attrs"]
    assert {block["redacted"] for block in parts.values()} == {"mask"}
    assert {v["value"] for v in parts[".status"]["values"]} == {"[redacted]"}


def test_a_rule_on_a_detection_reaches_the_part_that_has_it(tmp_path: Path) -> None:
    columns = _columns(
        tmp_path,
        order_line(attrs=attrs_with_contact()),
        redact=(RedactRule(looks_like=("email",), with_="hash"),),
    )
    contact = columns["attrs"]["parts"][".contact"]

    assert contact["inferred"]["looks_like"] == "email"
    assert contact["redacted"] == "hash"
    assert "redacted" not in columns["attrs"]["parts"][".status"]


def test_an_unredacted_sensitive_part_warns_naming_the_part(tmp_path: Path) -> None:
    _columns(tmp_path, order_line(attrs=attrs_with_contact()))
    issues = validate_print(tmp_path / "primary")

    assert [i for i in issues if i.severity == "error"] == []
    assert any(
        i.code == "privacy.unredacted-sensitive"
        and i.path.endswith("columns.attrs.parts[.contact]")
        for i in issues
    )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda parts: parts.update({'["status"]': parts.pop(".status")}),
            "stats.part-path-not-canonical",
        ),
        (lambda parts: parts.pop("[*]"), "stats.part-parent-unlisted"),
    ],
)
def test_a_hand_edited_parts_map_fails_conformance(
    tmp_path: Path,
    mutate: Any,
    code: str,
) -> None:
    _columns(tmp_path, order_line())
    path = next((tmp_path / "primary").rglob("statistics.yaml"))
    data = yaml.safe_load(path.read_text())
    target = "attrs" if code == "stats.part-path-not-canonical" else "items"
    mutate(data["columns"][target]["parts"])
    path.write_text(yaml.safe_dump(data, sort_keys=False))

    assert code in {i.code for i in validate_print(tmp_path / "primary")}


def test_parts_found_below_the_list_fails_conformance(tmp_path: Path) -> None:
    _columns(tmp_path, order_line())
    path = next((tmp_path / "primary").rglob("statistics.yaml"))
    data = yaml.safe_load(path.read_text())
    data["columns"]["items"]["parts_found"] = 1
    path.write_text(yaml.safe_dump(data, sort_keys=False))

    assert "stats.parts-found-below-listed" in {
        i.code for i in validate_print(tmp_path / "primary")
    }


def test_a_part_both_sides_list_diffs_with_its_path_and_a_dropped_one_does_not(
    tmp_path: Path,
) -> None:
    _columns(tmp_path, order_line())
    before = yaml.safe_load(next((tmp_path / "primary").rglob("statistics.yaml")).read_text())
    after = yaml.safe_load(yaml.safe_dump(before))
    after["columns"]["attrs"]["parts"][".status"]["cardinality"] = 3
    del after["columns"]["items"]["parts"]["[*].qty"]

    events = diff_module._diff_statistics(
        "public.order_line",
        diff_module.comparable_columns(before["columns"]),
        diff_module.comparable_columns(after["columns"]),
        _states(before["columns"]),
        _states(after["columns"]),
        scoped=False,
    )

    assert [(e["column"], e.get("part"), e["stat"]) for e in events] == [
        ("attrs", ".status", "cardinality"),
    ]


def test_context_lists_each_part_under_its_label_with_presence(tmp_path: Path) -> None:
    _columns(tmp_path, order_line())
    root = tmp_path / "primary"
    text = assemble(
        yaml.safe_load((root / "manifest.yaml").read_text()),
        root,
        ["public.order_line"],
        AssemblyOptions(),
    ).text

    assert "## Column parts" in text
    assert "| items[*].sku |" in text
    assert "| attrs.status |" in text
    assert "; present: 90% of rows |" in text
    assert "; present: 100% of items[*] |" in text


def test_search_and_resolve_reach_a_part(tmp_path: Path) -> None:
    conn = _generated(tmp_path, order_line(attrs=attrs_with_contact()))
    state = ServedConnections(served={"primary": conn}, default="primary")

    by_pattern = _tool_search_columns(state, {"pattern": "attrs.*"})["matches"]
    sensitive = _tool_search_columns(state, {"sensitivity": "*"})["matches"]
    resolved = _tool_resolve_value(
        state,
        {"table": "public.order_line", "column": "attrs", "part": ".status", "text": "shipped"},
    )

    assert {m["part"] for m in by_pattern} == {".status", ".contact"}
    assert [m["part"] for m in sensitive] == [".contact"]
    assert resolved["part"] == ".status"
    assert resolved["match"] == "stored"


def test_a_committed_part_marker_the_rules_now_contradict_is_named(tmp_path: Path) -> None:
    rule = RedactRule(columns=("*.order_line.attrs",), with_="mask")
    conn = _generated(tmp_path, order_line(attrs=attrs_parts()), redact=(rule,))
    committed = CommittedPrint.load(tmp_path / "primary")

    mismatches = redaction_mismatches(
        committed.tables["public.order_line"],
        replace(conn, redact=()),
    )

    assert {m.column for m in mismatches} == {"attrs.status", 'attrs["user-id"]'}


def _states(columns: dict[str, Any]) -> dict[str, diff_module.ColumnState]:
    return {
        name: diff_module.ColumnState(name=name, sql_type="x", nullable=True, default=None)
        for name in columns
    }


def _columns(tmp_path: Path, fixture: Any, **conn_fields: Any) -> dict[str, Any]:
    _generated(tmp_path, fixture, **conn_fields)
    path = next((tmp_path / "primary").rglob("statistics.yaml"))

    return yaml.safe_load(path.read_text())["columns"]


def _generated(tmp_path: Path, fixture: Any, **conn_fields: Any) -> ConnectionConfig:
    conn = ConnectionConfig(
        name="primary",
        adapter="postgres",
        output=tmp_path,
        redaction_salt=_SALT,
        **conn_fields,
    )
    Engine(MockAdapter(fixture), conn, tmp_path).generate()

    return conn


def test_the_docs_page_lists_a_columns_parts_under_their_labels(tmp_path: Path) -> None:
    items = _columns(tmp_path, order_line())["items"]

    assert [p["label"] for p in parts_view("items", items)] == [
        "items[*]",
        "items[*].qty",
        "items[*].sku",
    ]
