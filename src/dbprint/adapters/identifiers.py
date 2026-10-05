"""Every adapter's one reading of a database identifier: fold, allowlist, collisions, quoting.

Folding, quoting or splitting a name anywhere else lets two statements address different objects.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Self

from dbprint.spec.fqn import SEPARATOR
from dbprint.spec.fqn import join as join_fqn
from .base import ColumnMeta, TableMeta, TableType
from .dialect import Dialect


PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]*$")

SOURCE_ALIAS = "src"


class IdentifierRejected(ValueError):
    """Raised when an identifier fails SPEC 1.5 at table or column grain; message is SPEC 1.5.5.

    `fqn` names the refused table at table grain, where the whole run is refused.
    """

    def __init__(self, message: str, *, fqn: str | None = None) -> None:
        super().__init__(message)
        self.fqn = fqn


class UnknownTable(LookupError):
    """Raised when a table's physical identifiers were never captured by `list_tables`."""


def fold(name: str) -> str:
    """The artifact's spelling of an identifier (SPEC 1.3, 2.2.1): lowercase, nothing else."""

    return name.lower()


def quote(name: str, dialect: Dialect) -> str:
    """`name` quoted in `dialect`'s identifier quote, the quote character doubled inside."""

    mark = dialect.quote_char

    return mark + name.replace(mark, mark * 2) + mark


def qualified(quoted_name: str) -> str:
    """`quoted_name` qualified by `SOURCE_ALIAS`."""

    return f"{SOURCE_ALIAS}.{quoted_name}"


def source_column(column: ColumnMeta, dialect: Dialect) -> str:
    """`column`'s quoted physical spelling, qualified by `SOURCE_ALIAS`."""

    return qualified(quote(column.physical_name or column.name, dialect))


def quote_path(parts: Iterable[str], dialect: Dialect) -> str:
    """Each of `parts` quoted in `dialect`, dot-joined - a schema or catalog reference."""

    return ".".join(quote(part, dialect) for part in parts)


def string_literal(text: str) -> str:
    """`text` as a single-quoted SQL string literal."""

    return "'" + text.replace("'", "''") + "'"


def table_meta(
    physical: tuple[str, ...],
    type: TableType,
    *,
    external: bool = False,
    opt_in_only: bool = False,
) -> TableMeta:
    """The `TableMeta` a table whose catalog spells it `physical` is written under."""

    path = tuple(fold(part) for part in physical)

    return TableMeta(
        fqn=join_fqn(path),
        type=type,
        namespace_path=path,
        external=external,
        opt_in_only=opt_in_only,
    )


def column_meta(
    physical: str,
    *,
    sql_type: str,
    nullable: bool,
    default: str | None,
    ordinal: int,
    collation: str | None = None,
    classify_as: str | None = None,
) -> ColumnMeta:
    """The `ColumnMeta` for a column the catalog spells `physical`, keyed by its folded name."""

    key = fold(physical)

    return ColumnMeta(
        name=key,
        sql_type=sql_type,
        nullable=nullable,
        default=default,
        ordinal=ordinal,
        physical_name=None if physical == key else physical,
        collation=collation,
        classify_as=classify_as,
    )


def enforce_table_identifiers(selected: Iterable[tuple[TableMeta, tuple[str, ...]]]) -> None:
    """Reject SPEC 1.5 violations before any artifact is written.

    Two tables folding to one path would overwrite each other, so the run stops.
    """

    seen: dict[str, tuple[str, ...]] = {}

    for meta, physical in selected:
        for segment in meta.namespace_path:
            if SEPARATOR in segment:
                raise IdentifierRejected(
                    _table_message(meta.fqn, "contains-period", segment),
                    fqn=meta.fqn,
                )

            if not PATH_SEGMENT_RE.match(segment):
                raise IdentifierRejected(
                    _table_message(meta.fqn, "contains-unsafe-character", segment),
                    fqn=meta.fqn,
                )

        previous = seen.get(meta.fqn)

        if previous is not None and previous != physical:
            raise IdentifierRejected(
                _table_message(
                    meta.fqn,
                    f"case-collides-with-{join_fqn(previous)}",
                    join_fqn(physical),
                ),
                fqn=meta.fqn,
            )

        seen[meta.fqn] = physical


def reject_column_collisions(fqn: str, columns: Sequence[ColumnMeta]) -> None:
    """Refuse a table two of whose columns fold to one map key (SPEC 1.5.2), first pair by ordinal."""

    seen: dict[str, str] = {}

    for column in sorted(columns, key=lambda c: c.ordinal):
        spelling = column.physical_name or column.name
        previous = seen.get(column.name)

        if previous is not None and previous != spelling:
            raise IdentifierRejected(_column_message(fqn, column.name, previous, spelling))

        seen[column.name] = spelling


@dataclass(frozen=True)
class Identity:
    """One table's physical spelling, and its columns', as the catalog reported them."""

    fqn: str
    parts: tuple[str, ...]
    dialect: Dialect
    columns: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        physical: tuple[str, ...],
        dialect: Dialect,
        columns: Mapping[str, str] | None = None,
    ) -> Self:
        """The identity of the table the catalog spells `physical`, keyed by its folded path."""

        fqn = join_fqn([fold(part) for part in physical])

        return cls(fqn=fqn, parts=physical, dialect=dialect, columns=columns or {})

    @property
    def table(self) -> str:
        """The table's own physical name."""

        return self.parts[-1]

    @property
    def addressed(self) -> tuple[str, ...]:
        """The parts a statement names - a session reaches only its own database on some engines."""

        count = self.dialect.addressed_parts

        return self.parts if count is None else self.parts[-count:]

    def quoted(self) -> str:
        """The fully quoted reference every statement addresses the table by."""

        return quote_path(self.addressed, self.dialect)

    def name_string(self) -> str:
        """The quoted reference as a string literal, for a function taking a name as text."""

        return string_literal(self.quoted())

    def sibling(self, table: str) -> str:
        """Quoted reference to `table` beside this one; the producer's own name, taken verbatim."""

        return quote_path((*self.addressed[:-1], table), self.dialect)

    def quoted_column(self, key: str) -> str:
        """The quoted physical spelling of the column keyed `key`; `KeyError` when unknown."""

        return quote(self.columns[key], self.dialect)

    def source_column(self, key: str) -> str:
        """`quoted_column(key)` qualified by `SOURCE_ALIAS`."""

        return qualified(self.quoted_column(key))

    def with_columns(self, columns: Sequence[ColumnMeta]) -> Self:
        """This identity carrying `columns`' physical spellings, refused on a key collision."""

        reject_column_collisions(self.fqn, columns)

        return replace(self, columns={c.name: c.physical_name or c.name for c in columns})


class IdentityRegistry:
    """The identities `list_tables` captured, shared by every session of one adapter.

    Sessions extract tables on parallel threads, so attaching a table's columns is locked.
    """

    def __init__(self, dialect: Dialect) -> None:
        self._dialect = dialect
        self._lock = threading.Lock()
        self._identities: dict[str, Identity] = {}

    def register(self, selected: Iterable[tuple[TableMeta, tuple[str, ...]]]) -> None:
        """Replace the captured tables with `selected`, forgetting every attached column."""

        with self._lock:
            self._identities = {
                meta.fqn: Identity.of(physical, self._dialect) for meta, physical in selected
            }

    def attach(self, fqn: str, columns: Sequence[ColumnMeta]) -> Identity:
        """Record `columns` on the table `fqn` and return its identity carrying them."""

        with self._lock:
            identity = self._get(fqn).with_columns(columns)
            self._identities[fqn] = identity

        return identity

    def __getitem__(self, fqn: str) -> Identity:
        with self._lock:
            return self._get(fqn)

    def _get(self, fqn: str) -> Identity:
        try:
            return self._identities[fqn]
        except KeyError:
            raise UnknownTable(
                f"physical identifiers for {fqn!r} are unknown; "
                "call list_tables() before per-table extraction",
            ) from None


def _table_message(fqn: str, reason: str, detail: str) -> str:
    return (
        f"ERROR: Table identifier rejected: {fqn}\n"
        f"  Reason: {reason}\n"
        f"  Detail: {detail!r}\n"
        f"  Resolution: Either rename the identifier in the database, OR "
        f"exclude it via .dbprint.yaml selectors:\n"
        f"    exclude:\n"
        f'      - "{fqn}"'
    )


def _column_message(fqn: str, key: str, previous: str, spelling: str) -> str:
    return (
        f"ERROR: Column identifier rejected: {fqn}.{key}\n"
        f"  Reason: case-collides-with-{previous}\n"
        f"  Detail: {spelling!r}\n"
        f"  Resolution: Either rename one of the columns in the database, OR "
        f"exclude the table via .dbprint.yaml selectors:\n"
        f"    exclude:\n"
        f'      - "{fqn}"'
    )
