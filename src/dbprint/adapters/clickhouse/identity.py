"""Physical catalog identifiers for one ClickHouse table and its columns.

`system.*` compares case-sensitively, so the artifact's lowercased path (SPEC 1.3) filters nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Identity:
    """Physical `(database, table)` plus lowercase-to-physical columns."""

    parts: tuple[str, str]
    columns: dict[str, str] = field(default_factory=dict)

    @property
    def database(self) -> str:
        return self.parts[0]

    @property
    def table(self) -> str:
        return self.parts[1]

    def dotted(self) -> str:
        """Unquoted `database.table`; lowercased it is the path the artifact is written under."""

        return ".".join(self.parts)

    def quoted(self) -> str:
        """Backtick-quoted `` `database`.`table` `` reference for a query's FROM clause."""

        return ".".join(quote_ident(part) for part in self.parts)

    def quoted_column(self, name: str) -> str:
        """Quoted physical identifier for a lowercased column name; `KeyError` if unknown."""

        return quote_ident(self.columns[name])


def quote_ident(name: str) -> str:
    """Backtick-quote an identifier, escaping embedded backticks."""

    return "`" + name.replace("`", "``") + "`"
