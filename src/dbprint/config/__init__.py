"""dbprint configuration - project config, connection credentials, selectors.

`ProjectConfig` and its members are the typed view of `.dbprint.yaml`, which `load_project`
discovers and loads; `resolve_connection` resolves adapter-required credentials per SPEC
precedence (env > connections.yaml > .env); `match`/`expand` are fnmatch table selectors.
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
    "match",
    "resolve_connection",
]
