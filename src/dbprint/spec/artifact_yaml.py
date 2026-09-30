"""The one YAML parser every print artifact is read through: libyaml's, when PyYAML has it.

A PyYAML build without libyaml falls back to the pure-Python parser, which is slower, not different.
"""

from __future__ import annotations

from typing import Any

import yaml


_BASE: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class ArtifactLoader(_BASE):
    """PyYAML's safe constructors over the fastest parser available; custom loaders subclass it."""


def load(text: str, *, loader: type[ArtifactLoader] = ArtifactLoader) -> Any:
    """Parse one artifact's text; raises `yaml.YAMLError` exactly as `yaml.safe_load` does."""

    return yaml.load(text, Loader=loader)
