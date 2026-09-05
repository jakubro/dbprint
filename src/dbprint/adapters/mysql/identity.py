"""Physical catalog identifiers for one MySQL/MariaDB table.

At `lower_case_table_names=0` names are stored verbatim, so the lowercased path (SPEC 1.3) misses.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    """Physical `(database, table)` as `information_schema` spells it."""

    parts: tuple[str, str]

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


def quote_ident(name: str) -> str:
    """Backtick-quote an identifier, escaping embedded backticks."""

    return "`" + name.replace("`", "``") + "`"
