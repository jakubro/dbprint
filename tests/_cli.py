"""CLI scaffolding: a project file, credentials exported as the CLI reads them, a patched registry.

Env-var names come from `env_var_name`, so a change to the naming rule lands here too.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from dbprint.cli.main import main
from dbprint.config.connections import env_var_name


PROJECT_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
"""

AUTO_PROJECT_YAML = """\
connections:
  primary:
    adapter: postgres
    auto: true
    output: prints
"""

STUB_CREDENTIALS = {"host": "h", "port": "5432", "database": "d", "user": "u", "password": "p"}


def credential_env(
    connection: str = "primary",
    credentials: Mapping[str, Any] = STUB_CREDENTIALS,
) -> dict[str, str]:
    """The environment that supplies `credentials` to `connection`; a None value exports empty."""

    return {
        env_var_name(connection, key): "" if value is None else str(value)
        for key, value in credentials.items()
    }


def patch_registry(adapters: Mapping[str, type]) -> Any:
    """Replace the CLI's adapter registry with exactly `adapters` for the patch's duration."""

    return patch.dict("dbprint.cli.adapter_registry.ADAPTERS", dict(adapters), clear=True)


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


def run_cli(
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> Result:
    """Invoke the CLI from `project_dir` with `env` exported."""

    monkeypatch.chdir(project_dir)

    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    return CliRunner().invoke(main, list(args))


def generate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conn_name: str,
    adapter: str,
    env: Mapping[str, str],
    rules: str = "",
) -> Path:
    """Run `generate` in a fresh project with `env` exported; the connection's print directory.

    Fails unless generate exits clean (0) or with drift (3).
    """

    project_dir = tmp_path / "project"
    write_project(project_dir, conn_name, adapter, rules)
    result = run_cli(project_dir, monkeypatch, ["generate", "--no-tui"], env)
    assert result.exit_code in (0, 3), (
        f"generate failed (exit={result.exit_code}):\n{result.output}"
    )

    return project_dir / "prints" / conn_name
