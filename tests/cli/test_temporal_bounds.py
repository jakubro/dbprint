"""A violated timestamp bound fails `check` offline and online, whatever its spelling."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from dbprint.cli.main import main
from dbprint.config.connections import env_var_name


_PROJECT = """\
version: 1
connections:
  garden:
    adapter: duckdb
    assertions:
      tables:
        plots.main.visit:
          columns:
            seen_at:
              range.min: {min: '2024-03-01 05:00:00'}
            logged_at:
              range.max: {max: '2024-03-05T05:00:00+02:00'}
"""


@pytest.mark.parametrize("mode", [(), ("--online",)], ids=["offline", "online"])
def test_a_violated_bound_fails_the_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: tuple[str, ...],
) -> None:
    database = tmp_path / "plots.duckdb"
    con = duckdb.connect(str(database))
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE TABLE visit (seen_at TIMESTAMP, logged_at TIMESTAMPTZ)")
    con.execute(
        "INSERT INTO visit SELECT TIMESTAMP '2024-03-01 01:00:00' + to_hours(i), "
        "TIMESTAMPTZ '2024-03-01 01:00:00+00' + to_hours(i) FROM range(100) r(i)",
    )
    con.close()
    (tmp_path / ".dbprint.yaml").write_text(_PROJECT)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(env_var_name("garden", "database"), str(database))

    generated = CliRunner().invoke(main, ["generate", "-q"])
    result = CliRunner().invoke(main, ["check", "-q", "--format", "json", *mode])
    failed = {
        issue["path"].rsplit(".columns.", 1)[-1]
        for issue in _issues(json.loads(result.output))
        if issue["code"] == "assertion.range-out-of-bounds"
    }

    assert generated.exit_code in (0, 3), generated.output
    assert result.exit_code == 6
    assert failed == {"seen_at.range.min", "logged_at.range.max"}


def _issues(node: object) -> list[dict[str, str]]:
    if isinstance(node, dict):
        own = [node] if "code" in node and "path" in node else []

        return own + [i for value in node.values() for i in _issues(value)]

    if isinstance(node, list):
        return [i for value in node for i in _issues(value)]

    return []
