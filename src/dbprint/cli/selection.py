"""Table selection against committed manifests, shared by the commands that read one table."""

from __future__ import annotations

import fnmatch
from difflib import get_close_matches
from typing import Any

from dbprint.config import ConnectionConfig
from dbprint.engine import EXIT_GENERIC, EXIT_OK, worst_exit
from dbprint.engine.baseline import (
    failed_tables,
    read_manifest,
    unprofiled_message,
    unusable_manifest_message,
)


def resolve_connections(
    connections: list[ConnectionConfig],
    target: str | None,
    select_all: bool,
) -> tuple[list[tuple[ConnectionConfig, dict[str, Any], list[str]]], int, list[str]]:
    """Every connection that can answer, with its manifest and matched tables, plus the exit code
    the rest earned and the stderr lines explaining it. All resolve before any renders.
    """

    resolved: list[tuple[ConnectionConfig, dict[str, Any], list[str]]] = []
    exit_code = EXIT_OK
    messages: list[str] = []

    for conn_config in connections:
        read = read_manifest(conn_config.print_root)
        manifest = read.manifest

        if manifest is None:
            messages.append(
                f"{conn_config.name}: {unusable_manifest_message(read, conn_config.name)}",
            )
            exit_code = worst_exit(exit_code, EXIT_GENERIC)
            continue

        if unprofiled := unprofiled_matches(manifest, target, select_all):
            messages.append(
                f"{conn_config.name}: not profiled by the last run, so not included: "
                f"{', '.join(unprofiled)} - run dbprint generate to see the cause",
            )

        try:
            resolved_tables = resolve_tables(manifest, target, select_all)
        except NoMatch as exc:
            messages.append(str(exc))
            exit_code = worst_exit(exit_code, EXIT_GENERIC)
            continue

        resolved.append((conn_config, manifest, resolved_tables))

    return resolved, exit_code, messages


class NoMatch(ValueError):
    """Raised when the selection matches no manifest table."""


def resolve_tables(manifest: dict[str, Any], target: str | None, select_all: bool) -> list[str]:
    """Pick the ordered list of table FQNs per the selection rules."""

    tables = list((manifest.get("tables") or {}).keys())

    if select_all:
        return sorted(tables)

    assert target is not None

    if any(ch in target for ch in "*?["):
        matched = sorted(t for t in tables if fnmatch.fnmatchcase(t, target))

        if not matched:
            hint_list = ", ".join(sorted(tables)) or "(none)"

            raise NoMatch(f"pattern {target!r} matched no tables. Configured tables: {hint_list}")

        return matched

    if target in tables:
        return [target]

    if target in failed_tables(manifest):
        raise NoMatch(unprofiled_message(target))

    suggestions = get_close_matches(target, tables, n=1, cutoff=0.6)
    hint = f" Did you mean: {suggestions[0]}?" if suggestions else ""

    raise NoMatch(f"no table {target!r} found in manifest.{hint}")


def unprofiled_matches(
    manifest: dict[str, Any],
    target: str | None,
    select_all: bool,
) -> list[str]:
    """The tables the selection names that the last run recorded as failed."""

    if select_all:
        return list(failed_tables(manifest))

    if target is None or not any(ch in target for ch in "*?["):
        return []

    return [fqn for fqn in failed_tables(manifest) if fnmatch.fnmatchcase(fqn, target)]
