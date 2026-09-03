"""Credential resolution, salt binding and Engine construction, shared by every command.

Centralizing the `with: hash` salt guard here means every command that calls `build_engine`
gets the same check, including one added later.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from dbprint.adapters import Adapter
from dbprint.config import ConnectionConfig, resolve_connection
from dbprint.config.project import REDACTION_SALT_KEY, bind_redaction_salt
from dbprint.engine import Engine
from .adapter_registry import get_adapter_class


# Credential keys the run log's per-connection record may name - an allowlist, never
# a deny-list, so a future credential key defaults to withheld rather than leaked.
_TARGET_KEYS: tuple[str, ...] = (
    "host",
    "port",
    "database",
    "account",
    "warehouse",
    "server_hostname",
    "http_path",
    "catalog",
    "project",
    "dataset",
)


@dataclass(frozen=True)
class EngineSetup:
    """One connection's Engine and the prepared adapter `check --online` runs assertions on."""

    adapter: Adapter
    engine: Engine


class ConnectionSetupError(Exception):
    """The target could not be prepared: unknown adapter, or credentials that did not resolve."""


def build_engine(conn_config: ConnectionConfig, project_root: Path) -> EngineSetup:
    """Resolve credentials, bind the redaction salt, construct the adapter and the Engine.

    Raises `ConnectionSetupError` when the target cannot be prepared and `ConfigError` when
    the config itself is refused, including the salt precondition for `with: hash`. Callers
    keep the two apart: one invites a retry, the other cannot be fixed by one.
    """

    try:
        adapter_class = get_adapter_class(conn_config.adapter)
    except KeyError as exc:
        raise ConnectionSetupError(exc.args[0] if exc.args else str(exc)) from exc

    try:
        credentials = resolve_connection(
            conn_config.name,
            list(adapter_class.REQUIRED_KEYS),
            project_root=project_root,
            optional_keys=[*adapter_class.OPTIONAL_KEYS, REDACTION_SALT_KEY],
        )
    except Exception as exc:
        raise ConnectionSetupError(str(exc)) from exc

    target = _describe_target(credentials)

    # The salt lives with the credentials, never in the committed `.dbprint.yaml`; popping
    # it keeps it out of the adapter's constructor.
    bound = bind_redaction_salt(conn_config, credentials.pop(REDACTION_SALT_KEY, None))

    # The ABC cannot type the constructor: adapters take credentials, MockAdapter a fixture.
    adapter_ctor = cast(Any, adapter_class)
    adapter = adapter_ctor(credentials)

    return EngineSetup(
        adapter=adapter,
        engine=Engine(adapter, bound, project_root, target=target),
    )


def _describe_target(credentials: dict[str, str]) -> str:
    """A non-secret summary of what the connection points at, for the run log's own record."""

    return " ".join(f"{key}={credentials[key]}" for key in _TARGET_KEYS if key in credentials)
