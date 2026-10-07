"""Two-pass relationship graph: refers_to -> referenced_by reverse index.

Pass 1 collects each table's outgoing FKs during extract; `resolve` reverses the in-memory
graph this run re-extracted, so its output is bounded by what was re-extracted rather than
by print scope. ARCHITECTURE.md 5 covers the scope-bound merge the engine applies before
writing, and the `relationships.broken-reciprocity` conformance check.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from dbprint.adapters.base import FkAction, ForeignKeyMeta


@dataclass(frozen=True)
class IncomingFk:
    """One incoming FK entry - the referencer's perspective on this table."""

    column: tuple[str, ...]
    referencer_table: str
    referencer_column: tuple[str, ...]
    # None on a baseline-hydrated edge that recorded neither (SPEC 2.3.8) - never defaulted to
    # a real action, which a live-extracted edge (this dataclass's other producer) always has.
    on_delete: FkAction | None
    on_update: FkAction | None
    detection: str
    constraint_name: str | None


def edge_detection(entry: Mapping[str, Any]) -> str:
    """An edge's `detection`; absent reads `inferred`, the weaker claim (SPEC 2.3.2)."""

    return entry.get("detection") or "inferred"


def rejected_edges(annotated: Iterable[Any] | None) -> dict[tuple[Any, ...], dict[str, Any]]:
    """The `verdict: rejected` entries of a `relationships.annotations.yaml` `refers_to` list,
    keyed by `edge_key` (SPEC 2.7.2).
    """

    return {
        edge_key(entry): entry
        for entry in annotated or ()
        if isinstance(entry, dict) and entry.get("verdict") == "rejected"
    }


def withhold_rejected(
    relationships: dict[str, Any] | None,
    rejected: Mapping[tuple[Any, ...], Any],
    incoming: Mapping[tuple[Any, ...], Any],
    table: str,
) -> dict[str, Any] | None:
    """`relationships` less every edge a human rejected (SPEC 2.7.2); the same object if none is.

    `incoming` is keyed `(referencer_table, *incoming_key)`; a declared edge is never withheld.
    """

    if not relationships:
        return relationships

    def kept(entries: Any, key: Any, verdicts: Mapping[tuple[Any, ...], Any]) -> list[Any]:
        return [
            e
            for e in entries or []
            if not (isinstance(e, dict) and edge_detection(e) != "declared" and key(e) in verdicts)
        ]

    refers_to = relationships.get("refers_to")
    referenced_by = relationships.get("referenced_by")
    own = kept(refers_to, edge_key, rejected)
    others = kept(
        referenced_by,
        lambda e: (e.get("referencer_table"), *incoming_key(e, table)),
        incoming,
    )

    if len(own) == len(refers_to or []) and len(others) == len(referenced_by or []):
        return relationships

    out = dict(relationships)

    if refers_to is not None:
        out["refers_to"] = own

    if referenced_by is not None:
        out["referenced_by"] = others

    return out


def edge_key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    """The (column, target_table, target_column) triplet a `refers_to` edge is addressed by."""

    return (
        tuple(entry.get("column") or ()),
        entry.get("target_table"),
        tuple(entry.get("target_column") or ()),
    )


def incoming_key(entry: Mapping[str, Any], table: str) -> tuple[Any, ...]:
    """`edge_key` of the referencer's own `refers_to` entry behind `table`'s `referenced_by` row."""

    return (
        tuple(entry.get("referencer_column") or ()),
        table,
        tuple(entry.get("column") or ()),
    )


def resolve(
    per_table_refers_to: dict[str, list[ForeignKeyMeta]],
) -> dict[str, list[IncomingFk]]:
    """Build a fqn -> list[IncomingFk] reverse index from the per-table graph.

    Entries sort by (referencer_table, columns) for diff stability. A table with no incoming
    FKs gets an empty list; a target outside the input keyset is included if an FK names it.
    """

    out: dict[str, list[IncomingFk]] = {fqn: [] for fqn in per_table_refers_to}

    for src_fqn, fks in per_table_refers_to.items():
        for fk in fks:
            entry = IncomingFk(
                column=fk.target_column,
                referencer_table=src_fqn,
                referencer_column=fk.column,
                on_delete=fk.on_delete,
                on_update=fk.on_update,
                detection=fk.detection,
                constraint_name=fk.constraint_name,
            )
            out.setdefault(fk.target_table, []).append(entry)

    for values in out.values():
        values.sort(key=lambda e: (e.referencer_table, e.referencer_column))

    return out
