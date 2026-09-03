"""Foreign-key inference (SPEC 2.3): from column naming, and from measured value containment.
Neither issues a query - naming is pure, and containment reads statistics already on disk.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from dbprint.adapters.base import ColumnMeta, ForeignKeyMeta, TableType, UniqueKeyMeta
from dbprint.spec.sketch import (
    K as SKETCH_K,
)
from dbprint.spec.sketch import (
    answerable_count,
    answerable_subset_containment,
    estimate_intersection,
    sketch_kind,
)


# The one naming convention strong enough to act on: `<name>_id` refers to table `<name>`.
_FK_SUFFIX = "_id"

# SPEC 2.2.14's own margin, `1/sqrt(answerable_count)`, falls to 0.05 at 400 - the slack a
# 0.95 containment floor leaves once both sketches are truncated.
_VALUE_DERIVED_MIN_ANSWERABLE = 400
_VALUE_DERIVED_MIN_CONTAINMENT = 0.95


@dataclass(frozen=True)
class TableInventory:
    """The catalog facts inference needs about one table.

    `primary_key`/`unique_columns` are single-column declared keys only, since a composite
    key cannot target the single-column rule. A view stays in the inventory because it
    originates edges of its own. `keys_known` is False only when the catalog read raised.
    """

    fqn: str
    type: TableType
    columns: tuple[ColumnMeta, ...]
    primary_key: str | None
    unique_columns: tuple[str, ...]
    keys_known: bool = True

    @classmethod
    def from_catalog(
        cls,
        fqn: str,
        table_type: TableType,
        columns: Sequence[ColumnMeta],
        unique_keys: Sequence[UniqueKeyMeta],
        keys_known: bool = True,
    ) -> TableInventory:
        """Build one table's entry from its declared-key groups.

        Composite groups drop out here, so a composite-only primary key falls through to
        the sole-unique rule.
        """

        primary = next(
            (
                group.columns[0]
                for group in unique_keys
                if group.primary and len(group.columns) == 1
            ),
            None,
        )
        # Deduplicated in order; a set would reorder a column's two constraints per run.
        unique = dict.fromkeys(
            group.columns[0]
            for group in unique_keys
            if not group.primary and len(group.columns) == 1
        )

        return cls(
            fqn=fqn,
            type=table_type,
            columns=tuple(columns),
            primary_key=primary,
            unique_columns=tuple(unique),
            keys_known=keys_known,
        )


@dataclass(frozen=True)
class SketchCandidate:
    """One column eligible for the value-derived comparison, per SPEC 2.3's floors 1/2 - the
    caller decides eligibility; this module compares whichever sketches it is handed.
    """

    fqn: str
    column: str
    sql_type: str
    cardinality: int
    sketch: tuple[int, ...]


def infer_value_derived_edges(
    children: Sequence[SketchCandidate],
    parents: Sequence[SketchCandidate],
    existing: frozenset[tuple[str, str, str, str]],
) -> dict[str, list[ForeignKeyMeta]]:
    """Propose an edge from measured value containment alone (SPEC 2.3's `measured` detection) -
    every qualifying child column of a table contributes, and the table's whole list is ordered
    once at the end for diff stability across runs.
    """

    proposals: dict[str, list[tuple[str, str, str, ForeignKeyMeta]]] = {}

    for child in children:
        child_kind = sketch_kind(child.sql_type)

        if child_kind is None:
            continue

        for parent in parents:
            if parent.fqn == child.fqn and parent.column == child.column:
                continue

            if sketch_kind(parent.sql_type) != child_kind:
                continue

            if (child.fqn, child.column, parent.fqn, parent.column) in existing:
                continue

            if _measure_containment(child, parent) is None:
                continue

            proposals.setdefault(child.fqn, []).append(
                (
                    child.column,
                    parent.fqn,
                    parent.column,
                    ForeignKeyMeta(
                        column=(child.column,),
                        target_table=parent.fqn,
                        target_column=(parent.column,),
                        on_delete="NO ACTION",
                        on_update="NO ACTION",
                        constraint_name=None,
                        detection="measured",
                    ),
                ),
            )

    return {
        fqn: [fk for _, _, _, fk in sorted(entries, key=lambda p: (p[1], p[2], p[0]))]
        for fqn, entries in proposals.items()
    }


def _measure_containment(child: SketchCandidate, parent: SketchCandidate) -> float | None:
    """SPEC 2.3's floor 3, or None where the measurement cannot carry the claim."""

    # SPEC 2.3.11: the parent's cardinality MUST be the larger - equal makes neither larger and
    # licenses the edge in both directions, each end naming the other as parent.
    if child.cardinality >= parent.cardinality:
        return None

    both_exhaustive = len(child.sketch) < SKETCH_K and len(parent.sketch) < SKETCH_K

    if both_exhaustive:
        result = answerable_subset_containment(child.sketch, parent.sketch)

        if result is None:
            return None

        ratio, _count = result
        containment = min(1.0, round(ratio, 6))

        return containment if containment == 1.0 else None

    if not child.cardinality:
        return None

    intersection = estimate_intersection(child.sketch, parent.sketch)
    containment = min(1.0, round(intersection / child.cardinality, 6))
    count = answerable_count(child.sketch, parent.sketch)

    if count >= _VALUE_DERIVED_MIN_ANSWERABLE and containment >= _VALUE_DERIVED_MIN_CONTAINMENT:
        return containment

    return None


def infer_foreign_keys(
    table: TableInventory,
    inventory: dict[str, TableInventory],
    declared: list[ForeignKeyMeta],
) -> list[ForeignKeyMeta]:
    """Return the inferred outgoing edges for one table, in column order.

    A column named `<name>_id` infers an edge when `<name>` or its regular plural resolves
    to an in-scope table, that table declares a single-column key to target, the two types
    are compatible, and no declared foreign key already covers the column. SPEC 2.3.8
    refuses a column pointing at itself, unlike a self-reference between two columns;
    composite keys are never inferred, naming evidence for one being too weak.
    """

    covered = {c for fk in declared for c in fk.column}
    out: list[ForeignKeyMeta] = []

    for column in table.columns:
        if column.name in covered or not column.name.endswith(_FK_SUFFIX):
            continue

        stem = column.name[: -len(_FK_SUFFIX)]

        if not stem:
            continue

        target = _resolve_target(stem, table.fqn, inventory)

        if target is None:
            continue

        target_column = _unique_target_column(target)

        if target_column is None or not _types_compatible(column, target, target_column):
            continue

        if target.fqn == table.fqn and target_column == column.name:
            continue  # self-pointing column; see docstring's SPEC 2.3.8 bullet

        out.append(
            ForeignKeyMeta(
                column=(column.name,),
                target_table=target.fqn,
                target_column=(target_column,),
                # An inferred edge claims no referential action the database never declared.
                on_delete="NO ACTION",
                on_update="NO ACTION",
                constraint_name=None,
            ),
        )

    return out


def can_be_target(candidate: TableInventory) -> bool:
    """Whether an inferred edge may point at this object, per SPEC 2.3.8.

    A view or matview is excluded outright regardless of its own keys. `keys_known=False`
    stays eligible but supplies no target column, so a catalog-read failure can only
    suppress an edge, never redirect one to a different table. Public: it is also the
    fact `relationships.yaml`'s `eligible_target` reports.
    """

    if candidate.type != "table":
        return False

    if not candidate.keys_known:
        return True

    return _unique_target_column(candidate) is not None


def _resolve_target(
    stem: str,
    source_fqn: str,
    inventory: dict[str, TableInventory],
) -> TableInventory | None:
    """Find the in-scope table a `<stem>_id` column names.

    Matches on the object name, not the whole FQN. The source's own namespace wins when
    the local name is eligible; otherwise a stem resolving in more than one namespace is
    ambiguous and infers nothing.
    """

    namespace = source_fqn.rsplit(".", 1)[0] if "." in source_fqn else ""

    for candidate in _name_candidates(stem):
        qualified = f"{namespace}.{candidate}" if namespace else candidate
        entry = inventory.get(qualified)

        if entry is not None and can_be_target(entry):
            return entry

    for candidate in _name_candidates(stem):
        matches = [
            inv
            for fqn, inv in inventory.items()
            if fqn.rsplit(".", 1)[-1] == candidate and can_be_target(inv)
        ]

        if len(matches) == 1:
            return matches[0]

        if matches:
            return None

    return None


def _name_candidates(stem: str) -> tuple[str, ...]:
    """The stem first, then its regular plural, so `person` beats `persons` for `person_id`."""

    plurals = [f"{stem}s"]

    if stem.endswith(("s", "x", "z", "ch", "sh")):
        plurals = [f"{stem}es"]
    elif stem.endswith("y") and len(stem) > 1 and stem[-2] not in "aeiou":
        plurals = [f"{stem[:-1]}ies"]

    return (stem, *plurals)


def _unique_target_column(target: TableInventory) -> str | None:
    """The column an edge targets, in the order SPEC 2.3.8 fixes.

    The primary key leads; a sole UNIQUE constraint is the fallback, and several of them
    with no primary key to break the tie is an ambiguity that infers nothing.
    """

    if target.primary_key is not None:
        return target.primary_key

    return target.unique_columns[0] if len(target.unique_columns) == 1 else None


def _types_compatible(source: ColumnMeta, target: TableInventory, target_column: str) -> bool:
    """True when the two columns could hold the same values.

    Compared on the base type with parameterization stripped, so `varchar(64)` matches
    `varchar(128)`; integer widths are treated as one family.
    """

    other = next((c for c in target.columns if c.name == target_column), None)

    if other is None:
        return False

    left, right = _base_type(source.sql_type), _base_type(other.sql_type)

    if left == right:
        return True

    return left in _INTEGER_TYPES and right in _INTEGER_TYPES


def _base_type(sql_type: str) -> str:
    return sql_type.lower().split("(", 1)[0].strip()


_INTEGER_TYPES = frozenset(
    {"smallint", "integer", "int", "int2", "int4", "int8", "bigint", "mediumint", "tinyint"},
)
