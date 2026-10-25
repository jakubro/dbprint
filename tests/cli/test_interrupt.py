"""Ctrl-C during a duckdb statement ends `generate`, `diff` and `check --online` with exit 1."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest


BIG = "wh.main.b_big"
LATER = "wh.main.c_small"
CONFIG = """\
connections:
  wh:
    adapter: duckdb
    assertions:
      queries:
        - name: no_duplicate_keys
          sql: SELECT b.k FROM b_big b GROUP BY b.k HAVING COUNT(DISTINCT b.h) > 1000000
          expect: empty
"""


@pytest.fixture(scope="module")
def printed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    work = tmp_path_factory.mktemp("interrupt")
    con = duckdb.connect(str(work / "wh.duckdb"))
    con.execute("CREATE TABLE a_small AS SELECT i AS id, 'n' || i AS name FROM range(100) t(i)")
    con.execute(
        "CREATE TABLE b_big AS SELECT i AS id, i % 100000 AS k, md5(i::VARCHAR) AS h, "
        "md5((i * 7)::VARCHAR) AS g FROM range(12000000) t(i)",
    )
    con.execute("CREATE TABLE c_small AS SELECT i AS id FROM range(50) t(i)")
    con.close()
    (work / ".dbprint.yaml").write_text(CONFIG, encoding="utf-8")
    env = {**os.environ, "HOME": str(work), "DBPRINT_WH_DATABASE": str(work / "wh.duckdb")}
    first = subprocess.run(
        [_dbprint(), "generate", "-q"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert first.returncode in (0, 3), first.stderr

    return work, env


@pytest.mark.parametrize("command", [["generate", "--force"], ["diff"], ["check", "--online"]])
def test_ctrl_c_mid_statement_exits_one_and_commits_nothing(
    printed: tuple[Path, dict[str, str]],
    command: list[str],
) -> None:
    work, env = printed
    before = _tree(work / "prints" / "wh")

    code, err = _interrupted(work, env, command)

    assert code == 1, err
    assert "Aborted" in err
    assert _tree(work / "prints" / "wh") == before
    assert f"\t{LATER}\tstart" not in err
    assert "Query interrupted" not in err
    assert "sql-execution-error" not in err


def _dbprint() -> str:
    return str(Path(sys.executable).parent / "dbprint")


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _interrupted(work: Path, env: dict[str, str], command: list[str]) -> tuple[int, str]:
    proc = subprocess.Popen(
        [_dbprint(), *command, "--no-tui"],
        cwd=work,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    lines: list[str] = []

    assert proc.stderr is not None

    for line in proc.stderr:
        lines.append(line)

        if line.rstrip("\n").split("\t")[1:3] == [BIG, "start"]:
            time.sleep(2)
            os.killpg(proc.pid, signal.SIGINT)
            break

    lines.extend(proc.stderr)

    return proc.wait(timeout=300), "".join(lines)
