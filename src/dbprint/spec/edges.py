"""An edge's detection and address, and which human verdicts bind (SPEC 2.3, 2.7.2). Pure."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def edge_detection(entry: Mapping[str, Any]) -> str:
    """An edge's `detection`; absent reads `inferred`, the weaker claim (SPEC 2.3.2)."""

    return entry.get("detection") or "inferred"


def edge_key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    """The (column, target_table, target_column) triplet a `refers_to` edge is addressed by."""

    return (
        tuple(entry.get("column") or ()),
        entry.get("target_table"),
        tuple(entry.get("target_column") or ()),
    )


def verdict_binds(edge: Mapping[str, Any] | None) -> bool:
    """Whether a `verdict` on `edge` takes effect: never on a declared edge; an absent one binds."""

    return edge is None or edge_detection(edge) != "declared"


def rejected_edges(
    annotated: Iterable[Any] | None,
    edges: Mapping[tuple[Any, ...], Mapping[str, Any]],
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """The `verdict: rejected` entries of a `refers_to` annotation list that bind, by `edge_key`.

    `edges` maps each address to the edge it names, so a verdict on a declared edge is dropped.
    """

    return {
        key: entry
        for entry in annotated or ()
        if isinstance(entry, dict)
        and entry.get("verdict") == "rejected"
        and verdict_binds(edges.get(key := edge_key(entry)))
    }
