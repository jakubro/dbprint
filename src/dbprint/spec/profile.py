"""The manifest's recorded `profile` (SPEC 2.5) and the measurements it switches off (SPEC 2.2.21)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# Sorted, as `statistics.yaml`'s `disabled` lists them.
DISABLEABLE: tuple[str, ...] = (
    "looks_like",
    "null_patterns",
    "parts",
    "relationships.infer",
    "sketches",
    "timeline",
)

# What each switched-off measurement leaves absent, per column (and part) and per file (SPEC 2.2.21).
COLUMN_FIELDS_OF: Mapping[str, tuple[str, ...]] = {
    "looks_like": (
        "inferred.looks_like",
        "inferred.sampled",
        "inferred.matched",
        "inferred.looks_like_candidate",
        "inferred.looks_like_candidate_share",
        "inferred.epoch_unit",
    ),
    "parts": ("parts", "parts_found", "size"),
    "sketches": ("sketch",),
    "timeline": ("populated",),
}
BLOCKS_OF: Mapping[str, tuple[str, ...]] = {
    "null_patterns": ("null_patterns",),
    "timeline": ("timeline",),
}


def effective_profile(base: Mapping[str, Any], delta: Any) -> dict[str, Any]:
    """`base` with an entry's `profile` delta overlaid block by block (SPEC 2.5).

    A block replaces its namesake whole; under `read`, each object type's block does.
    """

    out = {
        name: dict(block) if isinstance(block, Mapping) else block for name, block in base.items()
    }

    if not isinstance(delta, Mapping):
        return out

    for name, block in delta.items():
        if name == "read" and isinstance(block, Mapping):
            read = out.get("read")
            out["read"] = {**(read if isinstance(read, Mapping) else {}), **block}
        else:
            out[name] = block

    return out


def switched_off(profile: Mapping[str, Any]) -> tuple[str, ...]:
    """The `DISABLEABLE` names a recorded profile switches off, sorted."""

    def off(block: str, key: str) -> bool:
        value = profile.get(block)

        return isinstance(value, Mapping) and value.get(key) is False

    flags = {
        "looks_like": off("looks_like", "enabled"),
        "null_patterns": off("null_patterns", "enabled"),
        "parts": off("parts", "enabled"),
        "relationships.infer": off("relationships", "infer"),
        "sketches": off("sketches", "enabled"),
        "timeline": off("timeline", "enabled"),
    }

    return tuple(name for name in DISABLEABLE if flags[name])
