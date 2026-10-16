"""The one merge rule `.dbprint.yaml` cascades by: maps merge key by key, a later value wins, a
list replaces the earlier list whole.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


Origins = dict[tuple[str, ...], str]


def merge(layers: Sequence[tuple[str, Mapping[str, Any]]]) -> tuple[dict[str, Any], Origins]:
    """Fold `(label, tree)` layers in order, with the label of the layer that set each leaf."""

    resolved: dict[str, Any] = {}
    origins: Origins = {}

    for label, tree in layers:
        merge_into(resolved, origins, tree, label)

    return resolved, origins


def merge_into(
    resolved: dict[str, Any],
    origins: Origins,
    tree: Mapping[str, Any],
    label: str,
) -> None:
    """Fold one more `(label, tree)` layer over an already merged tree and its origins."""

    _fold(resolved, tree, (), label, origins)


def _fold(
    into: dict[str, Any],
    tree: Mapping[str, Any],
    path: tuple[str, ...],
    label: str,
    origins: Origins,
) -> None:
    for key, value in tree.items():
        here = (*path, key)

        if isinstance(value, Mapping) and isinstance(into.get(key), dict):
            _fold(into[key], value, here, label, origins)
        elif isinstance(value, Mapping):
            into[key] = {}
            _fold(into[key], value, here, label, origins)
        else:
            into[key] = value
            origins[here] = label
