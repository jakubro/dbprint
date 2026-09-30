"""YAML loading with datetime normalization for JSON Schema validation."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from dbprint.spec import artifact_yaml
from dbprint.spec.artifact_yaml import ArtifactLoader


class SourcedInt(int):
    """An `int` scalar that keeps the text it was written as."""

    source: str


class SourcedFloat(float):
    """A `float` scalar that keeps its text - a wide decimal loses digits as a float."""

    source: str


class _SourceKeepingLoader(ArtifactLoader):
    pass


def _sourced(kind: type[int | float]):
    base = ArtifactLoader.construct_yaml_int if kind is int else ArtifactLoader.construct_yaml_float
    wrapper = SourcedInt if kind is int else SourcedFloat

    def construct(loader: ArtifactLoader, node: yaml.ScalarNode) -> int | float:
        value = wrapper(base(loader, node))
        value.source = node.value

        return value

    return construct


_SourceKeepingLoader.add_constructor("tag:yaml.org,2002:int", _sourced(int))
_SourceKeepingLoader.add_constructor("tag:yaml.org,2002:float", _sourced(float))


def load_yaml(path: Path) -> Any:
    """Load YAML with dates as ISO 8601 strings and numbers as `SourcedInt`/`SourcedFloat`.

    JSON Schema has no datetime type; a sourced number keeps the text the file spells it with.
    """

    text = path.read_text(encoding="utf-8")

    return _normalize(artifact_yaml.load(text, loader=_SourceKeepingLoader))


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
