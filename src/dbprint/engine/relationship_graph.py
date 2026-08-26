"""Two-pass relationship graph: refers_to -> referenced_by reverse index.

Pass 1 collects each table's outgoing FKs during extract; `resolve` reverses the in-memory
graph this run re-extracted, so its output is bounded by what was re-extracted rather than
by print scope. ARCHITECTURE.md 5 covers the scope-bound merge the engine applies before
writing, and the `relationships.broken-reciprocity` conformance check.
"""

from __future__ import annotations

from dataclasses import dataclass

from dbprint.adapters.base import FkAction, ForeignKeyMeta


@dataclass(frozen=True)
class IncomingFk:
    """One incoming FK entry - the referencer's perspective on this table."""

    column: tuple[str, ...]
    referencer_table: str
    referencer_column: tuple[str, ...]
    on_delete: FkAction
    on_update: FkAction
    detection: str
    constraint_name: str | None


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
