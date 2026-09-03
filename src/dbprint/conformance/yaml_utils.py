"""YAML loading with datetime normalization for JSON Schema validation."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: Path) -> Any:
    """Load YAML and normalize datetime / date values to ISO 8601 strings.

    PyYAML auto-parses timestamps to datetime objects; JSON Schema has no datetime type.
    """

    return _normalize(yaml.safe_load(path.read_text(encoding="utf-8")))


def _normalize(node: Any) -> Any:
    if isinstance(node, datetime):
        s = node.isoformat()

        return s.replace("+00:00", "Z")
    elif isinstance(node, date):
        return node.isoformat()
    elif isinstance(node, dict):
        return {k: _normalize(v) for k, v in node.items()}
    elif isinstance(node, list):
        return [_normalize(v) for v in node]
    else:
        return node
