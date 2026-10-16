"""How `profile.read` blocks resolve: one mode per object type, each parameter bound to its mode."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal


ReadMode = Literal["full", "sample", "filter", "catalog"]
ObjectKind = Literal["tables", "views"]

MODE_PARAMETERS: Mapping[str, frozenset[str]] = {
    "full": frozenset(),
    "sample": frozenset({"fraction", "max_rows", "materialize"}),
    "filter": frozenset({"filter"}),
    "catalog": frozenset(),
}

BUILTIN_READ: Mapping[ObjectKind, Mapping[str, Any]] = {
    "tables": {"mode": "full"},
    "views": {"mode": "catalog"},
}

# A sample is one size or the other, so writing either one replaces its alternative.
_ALTERNATIVES = {"fraction": "max_rows", "max_rows": "fraction"}


def fold_read(
    resolved: Mapping[str, Any],
    layer: Mapping[str, Any],
    label: str,
    origins: dict[str, str],
) -> dict[str, Any]:
    """`layer` written over `resolved`; a new mode drops the parameters it does not take."""

    out = dict(resolved)

    if "mode" in layer and layer["mode"] != out.get("mode"):
        allowed = MODE_PARAMETERS.get(layer["mode"], frozenset())
        out = {key: value for key, value in out.items() if key == "mode" or key in allowed}

    for key, value in layer.items():
        if (alternative := _ALTERNATIVES.get(key)) is not None:
            out.pop(alternative, None)
            origins.pop(alternative, None)

        out[key] = value
        origins[key] = label

    for key in set(origins) - set(out):
        del origins[key]

    return out
