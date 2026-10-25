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
from dbprint.config import PartsProfile, ProfileConfig
from dbprint.config.project import ConnectionConfig, RedactRule
from dbprint.conformance import validate_print
from dbprint.docs.view import parts_view
from dbprint.engine import Engine
from dbprint.engine import diff as diff_module
from dbprint.engine.carried import CommittedPrint, redaction_mismatches
from dbprint.engine.context_assembler import AssemblyOptions, assemble
from dbprint.mcp.server import ServedConnections
from dbprint.mcp.tools import _tool_resolve_value, _tool_search_columns
from tests.engine._parts import (
    attrs_parts,
    attrs_with_contact,
    attrs_with_mostly_contact,
    attrs_with_vault_address,
    fieldwork,
    items_with_home_address,
    labels_keyed_by_email,
    many_parts,
)


_SALT = "parts-test-salt"


def test_an_array_of_records_publishes_each_part_over_its_occurrences(tmp_path: Path) -> None:
    items = _columns(tmp_path, fieldwork())["items"]
    parts = items["parts"]

    assert items["classification"] == "composite"
    assert items["parts_found"] == 3
    assert items["size"] == {"min": 1, "max": 5, "avg": 2.5, "p95": 5.0}
    assert list(parts) == ["[*]", "[*].lot", "[*].seeds"]
    assert parts["[*]"]["classification"] == "composite"
    assert parts["[*].lot"]["classification"] == "categorical"
    assert [v["value"] for v in parts["[*].lot"]["values"]] == ["LOT-A", "LOT-B", "LOT-C"]
    assert parts["[*].seeds"]["classification"] == "numeric"
    assert parts["[*].seeds"]["range"] == {"min": 1, "max": 60}
    assert parts["[*].seeds"]["cardinality_ratio"] == 0.24  # noqa: RUF069 - the expected value is an exact literal
    assert parts["[*].seeds"]["occurrences"] == 250
    assert not {"nullable", "rows_scanned", "sketch"} & set(parts["[*].lot"])


def test_a_document_stays_json_and_quotes_a_key_that_needs_it(tmp_path: Path) -> None:
    attrs = _columns(tmp_path, fieldwork())["attrs"]

    assert attrs["classification"] == "json"
    assert attrs["cardinality"] == 40
    assert attrs["parts"][".status"]["classification"] == "categorical"
    assert '["user-id"]' in attrs["parts"]


def test_a_map_lists_its_key_set_and_profiles_one_keys_value(tmp_path: Path) -> None:
    labels = _columns(tmp_path, fieldwork())["labels"]

    assert [v["value"] for v in labels["parts"]["[keys]"]["values"]] == ["unit", "origin"]
    assert labels["parts"][".unit"]["occurrences"] == 100


def test_a_withheld_key_set_drops_its_members_before_they_are_read(tmp_path: Path) -> None:
    labels = _columns(
        tmp_path,
        fieldwork(labels=labels_keyed_by_email()),
        redact=(RedactRule(looks_like=("email",), with_="hash"),),
    )["labels"]

    assert list(labels["parts"]) == ["[keys]"]
    assert labels["parts"]["[keys]"]["redacted"] == "hash"
    assert labels["parts_found"] == 3


def test_the_cap_keeps_the_most_frequent_parts_and_counts_every_one(tmp_path: Path) -> None:
    columns = _columns(
        tmp_path,
        fieldwork(attrs=many_parts(30)),
        parts=PartsProfile(max=5),
    )

    assert list(columns["attrs"]["parts"]) == [".k00", ".k01", ".k02", ".k03", ".k04"]
    assert columns["attrs"]["parts_found"] == 30


def test_max_parts_zero_turns_descent_off(tmp_path: Path) -> None:
    columns = _columns(tmp_path, fieldwork(), parts=PartsProfile(enabled=False))

    assert columns["items"]["classification"] == "unsupported"
    assert "parts" not in columns["items"]
    assert "parts" not in columns["attrs"]


def test_a_failed_descent_leaves_the_column_undescended_and_the_table_profiled(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = fieldwork(items=MockParts(failure=RuntimeError("descent refused")))

    with caplog.at_level(logging.WARNING):
        columns = _columns(tmp_path, fixture)

    assert columns["items"]["classification"] == "unsupported"
    assert "parts" not in columns["items"]
    assert columns["attrs"]["parts"]
    assert any("descent refused" in r.getMessage() for r in caplog.records)


def test_a_rule_covering_the_column_marks_every_part_and_not_the_column(tmp_path: Path) -> None:
    columns = _columns(
        tmp_path,
        fieldwork(),
        redact=(RedactRule(columns=("*.fieldwork.attrs",), with_="mask"),),
    )
    parts = columns["attrs"]["parts"]

    assert "redacted" not in columns["attrs"]
    assert {block["redacted"] for block in parts.values()} == {"mask"}
    assert {v["value"] for v in parts[".status"]["values"]} == {"[redacted]"}


def test_a_rule_on_a_detection_reaches_the_part_that_has_it(tmp_path: Path) -> None:
    columns = _columns(
        tmp_path,
        fieldwork(attrs=attrs_with_contact()),
        redact=(RedactRule(looks_like=("email",), with_="hash"),),
    )
    contact = columns["attrs"]["parts"][".contact"]

    assert contact["inferred"]["looks_like"] == "email"
    assert contact["redacted"] == "hash"
    assert "redacted" not in columns["attrs"]["parts"][".status"]


def test_a_part_mostly_of_addresses_is_contact_and_masked(tmp_path: Path) -> None:
    columns = _columns(
        tmp_path,
        fieldwork(attrs=attrs_with_mostly_contact()),
        redact=(RedactRule(sensitivity=("contact",), with_="mask"),),
    )
    relay = columns["attrs"]["parts"][".relay"]

    assert relay["inferred"]["sensitivity"] == "contact"
    assert relay["redacted"] == "mask"
    assert "@" not in yaml.safe_dump(relay)


_POSTAL = (RedactRule(sensitivity=("postal_address",), with_="mask"),)


def test_a_rule_on_an_enclosing_parts_detection_withholds_a_documents_members(
    tmp_path: Path,
) -> None:
    columns = _columns(
        tmp_path,
        fieldwork(attrs=attrs_with_vault_address()),
        redact=_POSTAL,
    )
    attrs = columns["attrs"]
    parts = attrs["parts"]

    assert parts[".vault_address"]["inferred"]["sensitivity"] == "postal_address"
    assert parts[".vault_address[keys]"]["redacted"] == "mask"
    assert not [path for path in parts if path.startswith(".vault_address.")]
    assert attrs["parts_found"] == 6
    assert "redacted" not in parts[".status"]
    assert {v["value"] for v in parts[".status"]["values"]} == {"planted", "pending"}


def test_a_rule_on_an_enclosing_parts_detection_masks_a_records_fields(tmp_path: Path) -> None:
    columns = _columns(tmp_path, fieldwork(items=items_with_home_address()), redact=_POSTAL)
    parts = columns["items"]["parts"]

    assert parts["[*].home_address"]["inferred"]["sensitivity"] == "postal_address"
    assert parts["[*].home_address.line1"]["redacted"] == "mask"
    assert parts["[*].home_address.town"]["redacted"] == "mask"
    assert "redacted" not in parts["[*]"]


def test_a_print_written_before_the_rule_names_each_nested_part_stale(tmp_path: Path) -> None:
    conn = _generated(tmp_path, fieldwork(items=items_with_home_address()))
    ruled = replace(conn, profile=replace(conn.profile, redact=_POSTAL))
    committed = CommittedPrint.load(tmp_path / "primary").tables["public.fieldwork"]

    assert {m.column for m in redaction_mismatches(committed, ruled)} == {
        "items[*].home_address.line1",
        "items[*].home_address.town",
    }


def test_a_document_print_written_before_the_rule_names_each_member_stale(
    tmp_path: Path,
) -> None:
    conn = _generated(tmp_path, fieldwork(attrs=attrs_with_vault_address()))
    ruled = replace(conn, profile=replace(conn.profile, redact=_POSTAL))
    committed = CommittedPrint.load(tmp_path / "primary").tables["public.fieldwork"]

    assert {m.column for m in redaction_mismatches(committed, ruled)} >= {
        "attrs.vault_address[keys]",
        "attrs.vault_address.line1",
        "attrs.vault_address.city",
    }


def test_the_stale_guard_reads_a_part_path_it_cannot_parse_as_its_own_reading(
    tmp_path: Path,
) -> None:
    conn = _generated(tmp_path, fieldwork(items=items_with_home_address()))
    path = next((tmp_path / "primary").rglob("statistics.yaml"))
    data = yaml.safe_load(path.read_text())
    parts = data["columns"]["items"]["parts"]
    parts["not a path"] = parts.pop("[*].home_address.town")
    parts[7] = parts.pop("[*].home_address.line1")
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    ruled = replace(conn, profile=replace(conn.profile, redact=_POSTAL))
    committed = CommittedPrint.load(tmp_path / "primary").tables["public.fieldwork"]

    assert redaction_mismatches(committed, ruled) == ()


def test_a_rule_listing_any_enclosing_detection_covers_the_part_and_the_last_one_wins(
    tmp_path: Path,
) -> None:
    conn = ConnectionConfig(
        name="primary",
        adapter="postgres",
        output=tmp_path,
        profile=ProfileConfig(
            redact=(
                RedactRule(sensitivity=("postal_address",), with_="mask"),
                RedactRule(looks_like=("email",), with_="hash"),
            ),
        ),
    )
    chain = ((None, None), ("postal_address", None), (None, "email"))

    assert conn.redaction_for_part("db.t.c", None, None, chain) == "hash"
    assert conn.redaction_for_part("db.t.c", None, None, chain[:2]) == "mask"
    assert conn.redaction_for_part("db.t.c", None, None, chain[:1]) is None


def test_a_part_inside_a_sensitive_part_with_no_rule_warns(tmp_path: Path) -> None:
    _columns(tmp_path, fieldwork(items=items_with_home_address()))
    issues = validate_print(tmp_path / "primary")
    warned = {
        i.path.rsplit("parts", 1)[-1]
        for i in issues
        if i.code == "privacy.unredacted-sensitive" and "columns.items.parts" in i.path
    }

    assert [i for i in issues if i.severity == "error"] == []
    assert warned == {"[[*].home_address.line1]", "[[*].home_address.town]"}


def test_an_unredacted_sensitive_part_warns_naming_the_part(tmp_path: Path) -> None:
    _columns(tmp_path, fieldwork(attrs=attrs_with_contact()))
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
    _columns(tmp_path, fieldwork())
    path = next((tmp_path / "primary").rglob("statistics.yaml"))
    data = yaml.safe_load(path.read_text())
    target = "attrs" if code == "stats.part-path-not-canonical" else "items"
    mutate(data["columns"][target]["parts"])
    path.write_text(yaml.safe_dump(data, sort_keys=False))

    assert code in {i.code for i in validate_print(tmp_path / "primary")}


def test_parts_found_below_the_list_fails_conformance(tmp_path: Path) -> None:
    _columns(tmp_path, fieldwork())
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
    _columns(tmp_path, fieldwork())
    before = yaml.safe_load(next((tmp_path / "primary").rglob("statistics.yaml")).read_text())
    after = yaml.safe_load(yaml.safe_dump(before))
    after["columns"]["attrs"]["parts"][".status"]["cardinality"] = 3
    del after["columns"]["items"]["parts"]["[*].seeds"]

    events = diff_module._diff_statistics(
        "public.fieldwork",
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
    _columns(tmp_path, fieldwork())
    root = tmp_path / "primary"
    text = assemble(
        yaml.safe_load((root / "manifest.yaml").read_text()),
        root,
        ["public.fieldwork"],
        AssemblyOptions(),
    ).text

    assert "## Column parts" in text
    assert "| items[*].lot |" in text
    assert "| attrs.status |" in text
    assert "; present: 90% of rows |" in text
    assert "; present: 100% of items[*] |" in text


def test_search_and_resolve_reach_a_part(tmp_path: Path) -> None:
    conn = _generated(tmp_path, fieldwork(attrs=attrs_with_contact()))
    state = ServedConnections(served={"primary": conn}, default="primary")

    by_pattern = _tool_search_columns(state, {"pattern": "attrs.*"})["matches"]
    sensitive = _tool_search_columns(state, {"sensitivity": "*"})["matches"]
    resolved = _tool_resolve_value(
        state,
        {"table": "public.fieldwork", "column": "attrs", "part": ".status", "text": "planted"},
    )

    assert {m["part"] for m in by_pattern} == {".status", ".contact"}
    assert [m["part"] for m in sensitive] == [".contact"]
    assert resolved["part"] == ".status"
    assert resolved["match"] == "stored"


def test_a_committed_part_marker_the_rules_now_contradict_is_named(tmp_path: Path) -> None:
    rule = RedactRule(columns=("*.fieldwork.attrs",), with_="mask")
    conn = _generated(tmp_path, fieldwork(attrs=attrs_parts()), redact=(rule,))
    committed = CommittedPrint.load(tmp_path / "primary")

    mismatches = redaction_mismatches(
        committed.tables["public.fieldwork"],
        replace(conn, profile=replace(conn.profile, redact=())),
    )

    assert {m.column for m in mismatches} == {"attrs.status", 'attrs["user-id"]'}


def _states(columns: dict[str, Any]) -> dict[str, diff_module.ColumnState]:
    return {
        name: diff_module.ColumnState(name=name, sql_type="x", nullable=True, default=None)
        for name in columns
    }


def _columns(tmp_path: Path, fixture: Any, **profile_fields: Any) -> dict[str, Any]:
    _generated(tmp_path, fixture, **profile_fields)
    path = next((tmp_path / "primary").rglob("statistics.yaml"))

    return yaml.safe_load(path.read_text())["columns"]


def _generated(tmp_path: Path, fixture: Any, **profile_fields: Any) -> ConnectionConfig:
    conn = ConnectionConfig(
        name="primary",
        adapter="postgres",
        output=tmp_path,
        redaction_salt=_SALT,
        profile=ProfileConfig(**profile_fields),
    )
    Engine(MockAdapter(fixture), conn, tmp_path).generate()

    return conn


def test_the_docs_page_lists_a_columns_parts_under_their_labels(tmp_path: Path) -> None:
    items = _columns(tmp_path, fieldwork())["items"]

    assert [p["label"] for p in parts_view("items", items, None)] == [
        "items[*]",
        "items[*].lot",
        "items[*].seeds",
    ]
