"""`dbprint init` - scaffold project-level config + connections template."""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

import rich_click as click

from dbprint.engine import EXIT_OK


CONNECTIONS_DIR = Path("~/.dbprint").expanduser()
CONNECTIONS_FILE = CONNECTIONS_DIR / "connections.yaml"

PROJECT_TEMPLATE = """\
defaults:
  max_age_days: 7
  statistics:
    enumeration_threshold: 50
    top_n_values: 20
    percentiles: [1, 25, 50, 75, 99]
  diff:
    stat_change_threshold:
      default: 0.01

connections:
  primary:
    adapter: postgres
    auto: true
    include:
      - "public.*"
"""

CONNECTIONS_TEMPLATE = """\
primary:
  host: localhost
  port: 5432
  database: my_db
  user: dbprint_ro
  password: change_me
"""


@click.command(name="init")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing .dbprint.yaml. Never touches the creds stub - "
    "~/.dbprint/connections.yaml is shared by every project on the host and is "
    "written only when absent.",
)
@click.pass_context
def init_command(ctx: click.Context, force: bool) -> None:
    """Scaffold a new dbprint project in the current directory.

    Writes `.dbprint.yaml` (project config), creates the `prints/` output root,
    and writes a `~/.dbprint/connections.yaml` credentials template when one
    does not already exist. Idempotent: `.dbprint.yaml` is kept unless `--force`
    is given; the credentials stub is always kept once it exists, `--force` or
    not, since it is machine-wide rather than project-local. Prints one outcome
    line per file (wrote / kept / created).

    **Exit codes:**

    - `0`: always (init has no failure path)

    **Examples:**

    - `dbprint init`: scaffold; keep anything that already exists
    - `dbprint init --force`: overwrite `.dbprint.yaml`; credentials untouched
    """

    cwd = Path.cwd()
    outcomes = _scaffold(cwd, force=force)
    out: TextIO = click.get_text_stream("stdout")

    out.writelines(f"{status}\t{kind}\t{path}\n" for kind, path, status in outcomes)

    ctx.exit(EXIT_OK)


def _scaffold(cwd: Path, *, force: bool) -> list[tuple[str, Path, str]]:
    outcomes: list[tuple[str, Path, str]] = []
    outcomes.append(
        _write_template(
            cwd / ".dbprint.yaml",
            PROJECT_TEMPLATE,
            force=force,
            kind="project_config",
        ),
    )
    outcomes.append(_ensure_dir(cwd / "prints", kind="prints_dir"))
    outcomes.append(
        _write_template(
            CONNECTIONS_FILE,
            CONNECTIONS_TEMPLATE,
            force=False,
            kind="connections_file",
        ),
    )

    return outcomes


def _write_template(path: Path, content: str, *, force: bool, kind: str) -> tuple[str, Path, str]:
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not force:
        return (kind, path, "kept")

    path.write_text(content)

    return (kind, path, "wrote")


def _ensure_dir(path: Path, *, kind: str) -> tuple[str, Path, str]:
    if path.is_dir():
        return (kind, path, "kept")

    path.mkdir(parents=True, exist_ok=True)

    return (kind, path, "created")
