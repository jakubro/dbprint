"""`engine.explain`: a table's resolved profile against the row count the print recorded."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dbprint.adapters import MockAdapter, MockTable
from dbprint.config import RuleConfig, RuleMatch, TablesConfig
from dbprint.engine import ConnectFailed, Engine
from dbprint.engine.baseline import ManifestRead
from dbprint.engine.explain import explain
from tests._curator import conn_config, curator_fixture


FQN = "public.curator"
BIG = RuleConfig(
    match=RuleMatch(min_rows=1000),
    profile={"freshness": {"max_age_days": 30}},
    label="defaults.rules[0]",
)


def _manifest(**entry: Any) -> ManifestRead:
    tables = {FQN: {"type": "table", "profiled_at": "2026-06-01T00:00:00Z", **entry}}

    return ManifestRead(Path("manifest.yaml"), "ok", {"tables": tables})


def _conn(tmp_path: Path, *rules: RuleConfig) -> Any:
    return replace(conn_config(tmp_path), rules=rules)


@pytest.mark.parametrize(
    ("count", "outcome", "days"),
    [(5000, "applied", 30), (10, "below_min_rows", 7)],
)
def test_a_size_rule_is_tested_against_the_recorded_count(
    tmp_path: Path,
    count: int,
    outcome: str,
    days: int,
) -> None:
    explained = explain(_conn(tmp_path, BIG), FQN, _manifest(row_count=count))

    assert (explained.row_count, explained.count_source) == (count, "print")
    assert explained.profile.rules[0].outcome == outcome
    assert explained.profile.values["freshness"]["max_age_days"] == days


def test_an_unprinted_table_is_matched_by_name_only(tmp_path: Path) -> None:
    explained = explain(_conn(tmp_path, BIG), "public.elsewhere", _manifest(row_count=5000))

    assert not explained.printed
    assert explained.count_source == "none"
    assert explained.note == "not in the print: matched by name only"
    assert explained.profile.rules[0].outcome == "size_unknown"


def test_a_view_is_never_sized(tmp_path: Path) -> None:
    explained = explain(_conn(tmp_path, BIG), FQN, _manifest(type="view", row_count=5000))

    assert explained.row_count is None
    assert explained.profile.rules[0].outcome == "size_unknown"


def test_a_catalog_only_entry_has_no_count(tmp_path: Path) -> None:
    explained = explain(_conn(tmp_path, BIG), FQN, _manifest())

    assert explained.note == "the print records no row count"


def test_with_no_manifest_every_table_is_unprinted(tmp_path: Path) -> None:
    explained = explain(_conn(tmp_path), FQN, ManifestRead(Path("m.yaml"), "absent"))

    assert not explained.printed
    assert "Run `dbprint generate primary` first" in (explained.note or "")


def test_a_live_estimate_replaces_the_recorded_count(tmp_path: Path) -> None:
    explained = explain(
        _conn(tmp_path, BIG),
        FQN,
        _manifest(row_count=10),
        estimate=5000,
        online=True,
    )

    assert (explained.row_count, explained.count_source) == (5000, "database")
    assert explained.profile.rules[0].outcome == "applied"


def test_a_table_outside_the_connections_scope_says_so(tmp_path: Path) -> None:
    conn = replace(_conn(tmp_path), tables=TablesConfig(include=("other.*",)))

    assert not explain(conn, FQN, _manifest()).in_scope


def test_a_ceiling_shows_the_fraction_it_resolves_to(tmp_path: Path) -> None:
    ceiling = RuleConfig(profile={"read": {"tables": {"mode": "sample", "max_rows": 100}}})
    explained = explain(_conn(tmp_path, ceiling), FQN, _manifest(row_count=1000))

    assert explained.ceiling is not None
    assert explained.ceiling[0] == 100
    assert explained.ceiling[1] is not None and explained.ceiling[1] <= 0.1


def test_a_recorded_profile_unlike_todays_is_reported(tmp_path: Path) -> None:
    manifest = _manifest(row_count=5, profile={"null_patterns": {"enabled": False}})
    assert manifest.manifest is not None
    manifest.manifest["profile"] = {"null_patterns": {"enabled": True, "top_n": 20}}

    assert explain(_conn(tmp_path), FQN, manifest).recorded_changes == (
        "null_patterns.enabled",
        "null_patterns.top_n",
    )


def test_no_table_lists_the_rules_without_a_verdict(tmp_path: Path) -> None:
    explained = explain(_conn(tmp_path, BIG), None, _manifest())

    assert explained.profile.rules == ()
    assert [label for label, _ in explained.rules] == ["defaults.rules[0]"]


class _Tracking(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.closed = 0

    def close(self) -> None:
        self.closed += 1
        super().close()


def _sized() -> dict[str, MockTable]:
    fixture = curator_fixture()
    fixture[FQN] = replace(fixture[FQN], row_count_estimate=4321)
    fixture["public.ledger"] = replace(fixture[FQN], type="view", row_count_estimate=9)

    return fixture


@pytest.mark.parametrize(
    ("fqn", "expected"),
    [(FQN, 4321), ("public.ledger", None), ("public.nope", None)],
)
def test_the_engine_reads_one_live_estimate_and_disconnects(
    tmp_path: Path,
    fqn: str,
    expected: int | None,
) -> None:
    adapter = _Tracking(_sized())

    assert Engine(adapter, conn_config(tmp_path), tmp_path).estimate_row_count(fqn) == expected
    assert adapter.closed == 1


class _Unreachable(MockAdapter):
    def connect(self) -> None:
        raise OSError("no route to host")


def test_an_unreachable_database_raises_connect_failed(tmp_path: Path) -> None:
    with pytest.raises(ConnectFailed, match="no route to host"):
        Engine(_Unreachable(curator_fixture()), conn_config(tmp_path), tmp_path).estimate_row_count(
            FQN,
        )
