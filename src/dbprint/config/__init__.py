"""dbprint configuration - project config, connection credentials, selectors.

`load_project` walks up from a directory; `load_project_at` resolves a `--project` locator with
no walk. `resolve_connection` follows SPEC credential precedence (env > connections.yaml > .env).
"""

from __future__ import annotations

from .connections import resolve as resolve_connection
from .project import (
    ConfigError,
    ConnectionConfig,
    DiffConfig,
    ProjectConfig,
    RuleConfig,
    StatisticsConfig,
    TableSettings,
    load_project,
    load_project_at,
)
from .selectors import expand, match


__all__ = [
    "ConfigError",
    "ConnectionConfig",
    "DiffConfig",
    "ProjectConfig",
    "RuleConfig",
    "StatisticsConfig",
    "TableSettings",
    "expand",
    "load_project",
    "load_project_at",
    "match",
    "resolve_connection",
]
