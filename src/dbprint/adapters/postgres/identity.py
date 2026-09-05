"""Physical catalog identifiers for one Postgres relation.

The catalog stores a quoted name verbatim, so the artifact's lowercased path (SPEC 1.3) misses.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    """Physical `(schema, table)` as `pg_catalog` spells it."""

    parts: tuple[str, str]

    @property
    def schema(self) -> str:
        return self.parts[0]

    @property
    def table(self) -> str:
        return self.parts[1]

    def dotted(self) -> str:
        """Unquoted `schema.table`; lowercased it is the path the artifact is written under."""

        return ".".join(self.parts)

    def quoted(self) -> str:
        """Double-quoted `"schema"."table"` reference for a query's FROM clause."""

        return ".".join(quote_ident(part) for part in self.parts)


def quote_ident(name: str) -> str:
    """Double-quote an identifier, escaping embedded quotes."""

    return '"' + name.replace('"', '""') + '"'
