"""The project, fixture and generate steps every live e2e module shares.

A live module keeps its credentials, fixture driver and assertions about what the service printed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dbprint.cli.main import main
from dbprint.conformance import validate_print


def fixture_statements(fixture_dir: Path, engine: str) -> list[str]:
    """`schema.<engine>.sql` then `data.<engine>.sql`, one statement each, comment lines dropped."""

    return [
        statement
        for name in (f"schema.{engine}.sql", f"data.{engine}.sql")
        for statement in split_statements((fixture_dir / name).read_text())
    ]


def split_statements(text: str) -> list[str]:
    """One SQL script's statements, split on `;` with comment lines dropped."""

    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("--")]

    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def write_project(project_dir: Path, conn_name: str, adapter: str, rules: str = "") -> None:
    """Write a `.dbprint.yaml` with one auto-discovered connection, `rules` appended verbatim."""

    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".dbprint.yaml").write_text(
        f"""\
defaults:
  max_age_days: 7
  statistics:
    enumeration_threshold: 50
    top_n_values: 20
    percentiles: [1, 25, 50, 75, 99]

connections:
  {conn_name}:
    adapter: {adapter}
    auto: true
    output: prints
{rules}""",
    )


def generate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conn_name: str,
    adapter: str,
    env: dict[str, str],
    rules: str = "",
) -> Path:
    """Run `generate` in a fresh project with `env` exported; the connection's print directory.

    Fails unless generate exits clean (0) or with drift (3).
    """

    project_dir = tmp_path / "project"
    write_project(project_dir, conn_name, adapter, rules)
    monkeypatch.chdir(project_dir)

    for key, value in env.items():
        monkeypatch.setenv(key, value)

    result = CliRunner().invoke(main, ["generate", "--no-tui"])
    assert result.exit_code in (0, 3), (
        f"generate failed (exit={result.exit_code}):\n{result.output}"
    )

    return project_dir / "prints" / conn_name


def assert_conformant(print_dir: Path) -> None:
    """Fail on any error-severity conformance issue in the print, listing each."""

    errors = [i for i in validate_print(print_dir) if i.severity == "error"]
    assert errors == [], "Conformance violations:\n" + "\n".join(
        f"  {e.code} at {e.path}: {e.detail}" for e in errors
    )
