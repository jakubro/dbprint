"""A key or pattern that stops holding fails `check` offline and online, exit 6 (SPEC 7.2)."""

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
        plots.main.bed:
          columns:
            bed_id: {candidate_key: true}
            keeper: {looks_like: email}
"""


@pytest.mark.parametrize("mode", [(), ("--online",)], ids=["offline", "online"])
def test_a_broken_key_and_pattern_fail_the_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: tuple[str, ...],
) -> None:
    database = tmp_path / "plots.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE TABLE bed (bed_id INTEGER, keeper VARCHAR)")
    con.execute("INSERT INTO bed SELECT i % 10, 'no keeper ' || (i % 4) FROM range(300) r(i)")
    con.close()
    (tmp_path / ".dbprint.yaml").write_text(_PROJECT)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(env_var_name("garden", "database"), str(database))

    generated = CliRunner().invoke(main, ["generate", "-q"])
    result = CliRunner().invoke(main, ["check", "-q", "--format", "json", *mode])
    codes = {issue["code"] for issue in _issues(json.loads(result.output))}

    assert generated.exit_code in (0, 3), generated.output
    assert result.exit_code == 6
    assert {"assertion.candidate-key-mismatch", "assertion.looks-like-mismatch"} <= codes


def _issues(node: object) -> list[dict[str, str]]:
    if isinstance(node, dict):
        own = [node] if "code" in node and "path" in node else []

        return own + [i for value in node.values() for i in _issues(value)]

    if isinstance(node, list):
        return [i for value in node for i in _issues(value)]

    return []
