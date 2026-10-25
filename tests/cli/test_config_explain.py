"""`dbprint config explain`: a table's resolved profile, every value commented with its origin."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters import MockAdapter
from tests._cli import credential_env, patch_registry, run_cli, write_project
from tests._curator import curator_fixture


FQN = "public.curator"
RULES = """\
    rules:
    - match: {tables: [public.curator]}
      profile: {null_patterns: {enabled: false}}
    - match: {min_rows: 1000}
      profile: {freshness: {max_age_days: 30}}
"""


def _project(tmp_path: Path, row_count: int | None = 5000) -> Path:
    write_project(tmp_path, "primary", "postgres", RULES)
    prints = tmp_path / "prints" / "primary"
    prints.mkdir(parents=True)
    entry: dict[str, Any] = {
        "type": "table",
        "path": "public/curator",
        "profiled_at": "2026-06-01T00:00:00Z",
    }

    if row_count is not None:
        entry["row_count"] = row_count

    (prints / "manifest.yaml").write_text(
        yaml.safe_dump({"tables": {FQN: entry}}),
        encoding="utf-8",
    )

    return tmp_path


def test_a_printed_table_prints_its_profile_with_every_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_cli(_project(tmp_path), monkeypatch, ["config", "explain", "primary", FQN])
    lines = result.output.splitlines()

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.output)["profile"]["null_patterns"]["enabled"] is False
    assert (
        "# row count: 5000, as the print last recorded it; size rules evaluated against it" in lines
    )
    assert any(
        "connections.primary.rules[1]" in line and "applied (5000 >= 1000)" in line
        for line in lines
    )
    assert any(
        "max_age_days: 30" in line and "# connections.primary.rules[1]" in line for line in lines
    )
    assert any("top_n_values: 20" in line and "# defaults.profile" in line for line in lines)
    assert any(
        "enabled: false" in line and "# connections.primary.rules[0]" in line for line in lines
    )


def test_one_argument_naming_no_connection_is_the_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_cli(_project(tmp_path), monkeypatch, ["config", "explain", "public.elsewhere"])

    assert result.exit_code == 0, result.output
    assert "# table: public.elsewhere (not in the print)" in result.output
    assert "not matched (name)" in result.output
    assert "not applied (no row count)" in result.output


def test_a_broken_config_exits_one_with_the_loaders_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project(tmp_path, "primary", "postgres", "    profile: {statistcs: {}}\n")
    result = run_cli(tmp_path, monkeypatch, ["config", "explain"])

    assert result.exit_code == 1
    assert "did you mean 'statistics'?" in result.output


def test_an_unknown_connection_with_a_table_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_cli(_project(tmp_path), monkeypatch, ["config", "explain", "nope", FQN])

    assert result.exit_code == 1
    assert "unknown connection 'nope'" in result.output


class _Sized(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str], **_options: object) -> None:
        fixture = curator_fixture()
        fixture[FQN] = replace(fixture[FQN], row_count_estimate=10)
        super().__init__(fixture)


def test_online_tests_size_rules_against_the_live_estimate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with patch_registry({"postgres": _Sized}):
        result = run_cli(
            _project(tmp_path),
            monkeypatch,
            ["config", "explain", "primary", FQN, "--online"],
            credential_env("primary"),
        )

    assert result.exit_code == 0, result.output
    assert "# row count: 10, the database's current estimate" in result.output
    assert "not applied (10 < 1000)" in result.output


def test_online_without_credentials_exits_four(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    with patch_registry({"postgres": _Sized}):
        result = run_cli(_project(tmp_path), monkeypatch, ["config", "explain", FQN, "--online"])

    assert result.exit_code == 4
    assert "missing required credentials" in result.output


def test_online_refuses_a_remote_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(
        tmp_path,
        monkeypatch,
        ["config", "explain", FQN, "--online", "--project", "git@github.com:acme/prints.git"],
    )

    assert result.exit_code == 1
    assert "names a remote repository" in result.output


def test_a_value_yaml_would_retype_survives_the_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_project(
        tmp_path,
        "primary",
        "postgres",
        "    rules:\n    - profile: {read: {tables: {mode: filter, filter: '1e3 # x: y'}}}\n",
    )
    result = run_cli(tmp_path, monkeypatch, ["config", "explain", "primary", FQN])

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.output)["profile"]["read"]["tables"]["filter"] == "1e3 # x: y"


@pytest.mark.parametrize("online", [False, True])
def test_an_upper_case_table_resolves_as_its_lowercase_fqn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    online: bool,
) -> None:
    args = ["config", "explain", "primary", "PUBLIC.Curator", *(["--online"] if online else [])]

    with patch_registry({"postgres": _Sized}):
        result = run_cli(_project(tmp_path), monkeypatch, args, credential_env("primary"))

    assert result.exit_code == 0, result.output
    assert f"# table: {FQN} (in the print" in result.output
    assert yaml.safe_load(result.output)["profile"]["null_patterns"]["enabled"] is False


def _external(tmp_path: Path, statistics: str | bytes | None) -> Path:
    root = _project(tmp_path)
    manifest_path = root / "prints" / "primary" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["tables"][FQN]["artifacts"] = {"statistics": "statistics.yaml"}
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    table_dir = root / "prints" / "primary" / "public" / "curator"
    table_dir.mkdir(parents=True)

    if isinstance(statistics, bytes):
        (table_dir / "statistics.yaml").write_bytes(statistics)
    elif statistics is not None:
        (table_dir / "statistics.yaml").write_text(statistics, encoding="utf-8")

    return root


EXTERNAL_STATISTICS = "format_version: 1\ntable: public.curator\ntype: table\nexternal: true\n"
CATALOG_ALONE = (
    "# row count: none - described from the catalog alone, so generate takes no row-count "
    "estimate for it; size rules not applied"
)


def test_a_printed_external_table_is_not_sized_from_its_recorded_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _external(tmp_path, EXTERNAL_STATISTICS)
    result = run_cli(root, monkeypatch, ["config", "explain", "primary", FQN])

    assert result.exit_code == 0, result.output
    assert CATALOG_ALONE in result.output.splitlines()
    assert "not applied (no row count)" in result.output
    assert yaml.safe_load(result.output)["profile"]["freshness"]["max_age_days"] == 7


def test_online_a_printed_external_table_is_not_sized_from_the_estimate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _external(tmp_path, EXTERNAL_STATISTICS)

    with patch_registry({"postgres": _Sized}):
        result = run_cli(
            root,
            monkeypatch,
            ["config", "explain", "primary", FQN, "--online"],
            credential_env("primary"),
        )

    assert result.exit_code == 0, result.output
    assert CATALOG_ALONE in result.output.splitlines()
    assert yaml.safe_load(result.output)["profile"]["freshness"]["max_age_days"] == 7


@pytest.mark.parametrize(
    "statistics",
    [
        "format_version: 1\ntable: public.curator\ntype: table\n",
        "external: [true\n",
        "- true\n",
        b"external: \xff\n",
        "format_version: 1\ntable: public.curator\ntype: table\ndisabled: 5\n",
        None,
    ],
    ids=[
        "no-external-key",
        "unparseable",
        "not-a-mapping",
        "not-utf-8",
        "disabled-not-a-list",
        "absent",
    ],
)
def test_statistics_without_a_readable_external_flag_size_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statistics: str | bytes | None,
) -> None:
    root = _external(tmp_path, statistics)
    result = run_cli(root, monkeypatch, ["config", "explain", "primary", FQN])

    assert result.exit_code == 0, result.output
    assert "# row count: 5000, as the print last recorded it" in result.output
