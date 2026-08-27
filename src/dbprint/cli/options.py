"""The `--project` locator shared by every command that loads a project, and its remote helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import rich_click as click

from dbprint.config import ConfigError, ProjectConfig, load_project, load_project_at
from dbprint.config.remote import materialize, parse_address, watch_for_refresh


_HELP = (
    "Exact project locator: a directory whose direct child is .dbprint.yaml, that "
    ".dbprint.yaml file itself, or a git address (a forge URL, an SSH remote, or "
    "`<git-url>#<ref>:<subpath>`). No upward walk, no downward scan. Omit it to walk up "
    "from the working directory instead."
)


def project_option[F: Callable[..., Any]](f: F) -> F:
    """One `--project` declaration, applied identically to every loader.

    A plain string, not `click.Path`: a remote locator is not a filesystem path at all.
    """

    return click.option("--project", "project", type=str, default=None, help=_HELP)(f)


def resolve_project(project: str | None) -> ProjectConfig:
    """Load the project a `--project`-decorated command was invoked with.

    Omitted, it walks up from the working directory; any locator resolves exactly instead.
    """

    if project is None:
        return load_project()

    address = parse_address(project)

    if address is None:
        return load_project_at(project)

    return load_project_at(materialize(address))


def keep_fresh(project: str | None) -> None:
    """Start background TTL refresh for a remote `--project`; a no-op for a local one.

    Call once after `resolve_project`; only a long-lived server outlives the first TTL.
    """

    address = parse_address(project) if project is not None else None

    if address is not None:
        watch_for_refresh(address)


def refuse_if_remote(project: str | None, command: str) -> None:
    """Raise before any clone when `--project` names a git address.

    A remote print is read-only, and the address alone is grounds to refuse - no clone needed.
    """

    if project is not None and parse_address(project) is not None:
        raise ConfigError(
            f"--project {project!r} names a remote repository. `dbprint {command}` needs a "
            f"local one to write to (or query live) - clone it yourself first, or point "
            f"{command.split()[0]!r} at a local checkout instead.",
        )
