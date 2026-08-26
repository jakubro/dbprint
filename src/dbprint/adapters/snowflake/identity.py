"""Physical catalog identifiers for one table and its columns.

Snowflake reports unquoted identifiers uppercase from INFORMATION_SCHEMA, while the on-disk
format addresses objects via lowercased path segments (SPEC 1.5). Confusing the two fails
silently: a lowercased catalog filter matches nothing, and a lowercased name inside quotes
addresses a different object. `Identity` carries the physical form the catalog reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Identity:
    """Physical `(database, schema, table)` plus lowercase-to-physical columns."""

    parts: tuple[str, str, str]
    columns: dict[str, str] = field(default_factory=dict)

    @property
    def database(self) -> str:
        return self.parts[0]

    @property
    def schema(self) -> str:
        return self.parts[1]

    @property
    def table(self) -> str:
        return self.parts[2]

    def dotted(self) -> str:
        """Unquoted `db.schema.table`, for functions that take a name string."""

        return ".".join(self.parts)

    def quoted(self) -> str:
        """Fully-qualified quoted table reference."""

        return ".".join(quote_ident(part) for part in self.parts)

    def sibling(self, table: str) -> str:
        """Quoted reference to another object in this table's own database and schema.

        `table` is passed through verbatim, not upper-cased: it is the producer's own name,
        not a catalog identifier, so quoting it makes the stored form match what every
        later statement writes.
        """

        return ".".join(quote_ident(part) for part in (self.database, self.schema, table))

    def quoted_column(self, name: str) -> str:
        """Quoted physical identifier for a lowercased column name; `KeyError` if unknown."""

        return quote_ident(self.columns[name])


def quote_ident(name: str) -> str:
    """Double-quote an identifier, escaping embedded quotes."""

    return '"' + name.replace('"', '""') + '"'
