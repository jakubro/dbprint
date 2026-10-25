"""`config explain` tests a size rule exactly where `generate` takes a row-count estimate."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dbprint.config import RuleConfig, RuleMatch
from dbprint.config.project import ReadConfig, ReadSettings
from dbprint.config.read import ReadMode
from dbprint.engine.baseline import ManifestRead
from dbprint.engine.catalog_only import sizes_by_estimate
from dbprint.engine.explain import explain
from tests._curator import conn_config


FQN = "public.curator"
_SIZED_FRESHNESS = RuleConfig(
    match=RuleMatch(min_rows=1000),
    profile={"freshness": {"max_age_days": 30}},
)
_SIZED_MODE = RuleConfig(
    match=RuleMatch(min_rows=1000),
    profile={"read": {"tables": {"mode": "sample", "fraction": 0.5}}},
)


@pytest.mark.parametrize(
    ("mode", "object_type", "rule", "count", "outcome"),
    [
        ("full", "table", _SIZED_FRESHNESS, 5000, "applied"),
        ("full", "table", _SIZED_FRESHNESS, 10, "below_min_rows"),
        ("full", "table", _SIZED_MODE, 5000, "applied"),
        ("catalog", "table", _SIZED_FRESHNESS, 5000, "size_unknown"),
        ("catalog", "table", _SIZED_FRESHNESS, 10, "size_unknown"),
        ("catalog", "table", _SIZED_MODE, 5000, "applied"),
        ("catalog", "table", _SIZED_MODE, 10, "below_min_rows"),
        ("full", "view", _SIZED_FRESHNESS, 5000, "size_unknown"),
        ("full", "view", _SIZED_MODE, 5000, "size_unknown"),
    ],
)
def test_explain_and_generate_size_the_same_tables(
    tmp_path: Path,
    mode: ReadMode,
    object_type: str,
    rule: RuleConfig,
    count: int,
    outcome: str,
) -> None:
    base = conn_config(tmp_path)
    read = ReadSettings(tables=ReadConfig(mode=mode))
    conn = replace(base, rules=(rule,), profile=replace(base.profile, read=read))
    entry = {"type": object_type, "profiled_at": "2026-06-01T00:00:00Z", "row_count": count}
    manifest = ManifestRead(Path("manifest.yaml"), "ok", {"tables": {FQN: entry}})
    generate_sizes = sizes_by_estimate(conn, FQN, object_type)

    assert explain(conn, FQN, manifest).profile.rules[0].outcome == outcome
    assert generate_sizes is (outcome != "size_unknown")
