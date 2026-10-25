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
from dbprint.spec.edges import edge_key


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


def withhold_rejected(
    relationships: dict[str, Any] | None,
    rejected: Mapping[tuple[Any, ...], Any],
    incoming: Mapping[tuple[Any, ...], Any],
    table: str,
) -> dict[str, Any] | None:
    """`relationships` less every edge a human rejected (SPEC 2.7.2); the same object if none is.

    `incoming` is keyed `(referencer_table, *incoming_key)`; both maps come from `rejected_edges`.
    """

    if not relationships:
        return relationships

    def kept(entries: Any, key: Any, verdicts: Mapping[tuple[Any, ...], Any]) -> list[Any]:
        return [e for e in entries or [] if not (isinstance(e, dict) and key(e) in verdicts)]

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


def with_human_edges(
    relationships: dict[str, Any] | None,
    authored: Mapping[str, Iterable[Any] | None],
    table: str,
) -> dict[str, Any] | None:
    """`relationships` plus the edges humans added in `relationships.annotations.yaml` (SPEC 2.7.2).

    `authored` maps every table to its file's `refers_to`; an emitted edge gains only the note.
    """

    if relationships is None:
        return None

    refers_to = [
        dict(e) if isinstance(e, dict) else e for e in relationships.get("refers_to") or []
    ]
    referenced_by = [
        dict(e) if isinstance(e, dict) else e for e in relationships.get("referenced_by") or []
    ]
    outgoing = {edge_key(e): e for e in refers_to if isinstance(e, dict)}
    incoming = {
        (e.get("referencer_table"), *incoming_key(e, table)): e
        for e in referenced_by
        if isinstance(e, dict)
    }
    changed = False

    for referencer in sorted(authored):
        for entry in _authored_edges(authored[referencer]):
            key = edge_key(entry)
            note = entry.get("note")
            note = note if isinstance(note, str) and note.strip() else None

            if referencer == table:
                if key in outgoing:
                    changed |= _attach_note(outgoing[key], note)
                else:
                    refers_to.append(_annotated(entry, _OUTGOING_FIELDS, {}, note))
                    changed = True

            if key[1] != table:
                continue

            if (referencer, *key) in incoming:
                changed |= _attach_note(incoming[(referencer, *key)], note)
            else:
                referenced_by.append(
                    _annotated(entry, _INCOMING_FIELDS, {"referencer_table": referencer}, note),
                )
                changed = True

    if not changed:
        return relationships

    return {**relationships, "refers_to": refers_to, "referenced_by": referenced_by}


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


# The triplet an entry addresses an edge by (SPEC 2.7.2); an entry missing one addresses nothing.
_ADDRESS = ("column", "target_table", "target_column")
# Annotation field -> the edge field it fills on each side of a human-authored edge.
_OUTGOING_FIELDS = {
    "column": "column",
    "path": "path",
    "target_table": "target_table",
    "target_column": "target_column",
    "target_path": "target_path",
}
_INCOMING_FIELDS = {
    "target_column": "column",
    "target_path": "path",
    "column": "referencer_column",
    "path": "referencer_path",
}


def _authored_edges(entries: Iterable[Any] | None) -> list[dict[str, Any]]:
    return [
        e
        for e in entries or ()
        if isinstance(e, dict) and "verdict" not in e and all(e.get(k) for k in _ADDRESS)
    ]


def _attach_note(edge: dict[str, Any], note: str | None) -> bool:
    if note is None:
        return False

    edge["note"] = note

    return True


def _annotated(
    entry: Mapping[str, Any],
    fields: Mapping[str, str],
    extra: Mapping[str, Any],
    note: str | None,
) -> dict[str, Any]:
    edge = {**extra, **{to: entry[source] for source, to in fields.items() if source in entry}}
    edge["detection"] = "annotated"

    if note is not None:
        edge["note"] = note

    return edge
