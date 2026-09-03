"""Connection credential resolution: env > ~/.dbprint/connections.yaml > .env.

The first hit across the three sources wins per (connection_name, key); missing required
keys raise `ConfigError` listing every unresolved one at once.
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
        raise ConfigError(_unresolved_message(connection_name, unresolved, cfile, project_root))

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
    env_key = _env_var_name(connection_name, key)

    if env_key in env_map:
        return str(env_map[env_key])
    elif key in file_entry and file_entry[key] is not None:
        return str(file_entry[key])
    elif env_key in dotenv_map and dotenv_map[env_key] is not None:
        return str(dotenv_map[env_key])
    else:
        return None


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
) -> str:
    env_vars = ", ".join(_env_var_name(connection_name, k) for k in unresolved)

    return (
        f"Connection {connection_name!r}: missing required credentials: {unresolved}.\n"
        f"Provide them via one of:\n"
        f"  - environment variables: {env_vars}\n"
        f"  - {connections_file} under {connection_name!r}\n"
        f"  - {project_root / DOTENV_FILE} entries: {env_vars}"
    )
