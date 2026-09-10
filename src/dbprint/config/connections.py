"""Connection credential resolution: env > ~/.dbprint/connections.yaml > .env.

The first source carrying a value wins per (connection_name, key); missing required keys
raise `ConfigError` listing every unresolved one at once.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from .project import ConfigError


CONNECTIONS_FILE_DEFAULT = Path("~/.dbprint/connections.yaml")
DOTENV_FILE = ".env"

# The keys whose empty value is itself the credential: a trust-authenticated cluster is reached
# with a password of none, and a variable set to empty is how a runner supplies exactly that.
_EMPTY_IS_A_VALUE = frozenset({"password"})


def resolve(
    connection_name: str,
    required_keys: list[str],
    project_root: Path,
    connections_file: Path | None = None,
    env: dict[str, str] | None = None,
    optional_keys: list[str] | None = None,
) -> dict[str, str]:
    """Resolve credential keys for connection_name; precedence per SPEC + ARCHITECTURE.md 7.

    Missing `required_keys` raise `ConfigError` listing every unresolved one; `optional_keys`
    follow the same precedence but are silently omitted when absent. `env` defaults to `os.environ`.
    """

    env_map = env if env is not None else dict(os.environ)
    cfile = (connections_file or CONNECTIONS_FILE_DEFAULT).expanduser()

    file_entry = _load_connections_file(cfile).get(connection_name, {})
    dotenv_map = _load_dotenv(project_root / DOTENV_FILE)

    resolved: dict[str, str] = {}
    unresolved: list[str] = []

    for key in required_keys:
        value = _resolve_one(connection_name, key, env_map, file_entry, dotenv_map)

        if value is None:
            unresolved.append(key)
        else:
            resolved[key] = value

    if unresolved:
        blank = [
            _env_var_name(connection_name, key)
            for key in unresolved
            if _env_var_name(connection_name, key) in env_map
            or _env_var_name(connection_name, key) in dotenv_map
        ]

        raise ConfigError(
            _unresolved_message(connection_name, unresolved, cfile, project_root, blank),
        )

    for key in optional_keys or []:
        value = _resolve_one(connection_name, key, env_map, file_entry, dotenv_map)

        if value is not None:
            resolved[key] = value

    return resolved


def _resolve_one(
    connection_name: str,
    key: str,
    env_map: dict[str, str],
    file_entry: dict[str, Any],
    dotenv_map: dict[str, str | None],
) -> str | None:
    """The first source carrying a value, or None.

    An empty value in the connections file is deliberate and kept; `_carries_value` rules on
    an empty variable.
    """

    env_key = _env_var_name(connection_name, key)
    exported = env_map.get(env_key)
    carried = dotenv_map.get(env_key)

    if _carries_value(exported, key):
        return str(exported)
    elif key in file_entry and file_entry[key] is not None:
        return str(file_entry[key])
    elif _carries_value(carried, key):
        return str(carried)
    else:
        return None


def _carries_value(value: str | None, key: str) -> bool:
    """Whether a variable supplies this key.

    Blank is what a shell leaves when a secret did not resolve, so it falls through outside
    `_EMPTY_IS_A_VALUE`.
    """

    if value is None:
        return False

    return key in _EMPTY_IS_A_VALUE or bool(value.strip())


def _env_var_name(connection_name: str, key: str) -> str:
    return f"DBPRINT_{connection_name.upper()}_{key.upper()}"


def _load_connections_file(path: Path) -> dict[str, dict[str, Any]]:
    """Load ~/.dbprint/connections.yaml or return empty dict if absent."""

    if not path.is_file():
        return {}

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML — {exc}") from exc

    if data is None:
        return {}

    if not isinstance(data, dict):
        raise ConfigError(
            f"{path}: top-level YAML must be a mapping of connection names to credentials.",
        )

    return data


def _load_dotenv(path: Path) -> dict[str, str | None]:
    """Parse .env via python-dotenv; return empty if absent."""

    if not path.is_file():
        return {}

    return dict(dotenv_values(path))


def _unresolved_message(
    connection_name: str,
    unresolved: list[str],
    connections_file: Path,
    project_root: Path,
    blank: list[str] | None = None,
) -> str:
    """Name the sources, and any variable that was skipped for carrying no value."""

    env_vars = ", ".join(_env_var_name(connection_name, k) for k in unresolved)
    skipped = (
        f"\nSet but empty, so skipped: {', '.join(blank)}. An empty value is not a credential."
        if blank
        else ""
    )

    return (
        f"Connection {connection_name!r}: missing required credentials: {unresolved}.\n"
        f"Provide them via one of:\n"
        f"  - environment variables: {env_vars}\n"
        f"  - {connections_file} under {connection_name!r}\n"
        f"  - {project_root / DOTENV_FILE} entries: {env_vars}"
        f"{skipped}"
    )
