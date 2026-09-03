"""Physical catalog identifiers for one BigQuery table and its columns.

BigQuery is case-sensitive while the format addresses objects by lowercased path segments
(SPEC 1.3, 2.2.1): a lowercased table or column name resolves to nothing, so `Identity` carries
the physical form the catalog reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Identity:
    """Physical `(dataset, table)` plus lowercase-to-physical columns."""

    parts: tuple[str, str]
    columns: dict[str, str] = field(default_factory=dict)

    @property
    def dataset(self) -> str:
        return self.parts[0]

    @property
    def table(self) -> str:
        return self.parts[1]

    def dotted(self) -> str:
        """Unquoted `dataset.table`, for a materialized-scope name or a catalog filter value."""

        return ".".join(self.parts)

    def quoted(self) -> str:
        """Backtick-quoted `` `dataset`.`table` `` reference for a query's FROM clause."""

        return ".".join(quote_ident(part) for part in self.parts)

    def sibling(self, table: str) -> str:
        """Quoted reference to another table in this table's own dataset.

        `table` is passed through verbatim, not case-folded - it is the producer's own name, not
        a catalog identifier.
        """

        return ".".join(quote_ident(part) for part in (self.dataset, table))

    def quoted_column(self, name: str) -> str:
        """Quoted physical identifier for a lowercased column name; `KeyError` if unknown."""

        return quote_ident(self.columns[name])


def quote_ident(name: str) -> str:
    """Backtick-quote an identifier, escaping embedded backticks."""

    return "`" + name.replace("`", "``") + "`"
