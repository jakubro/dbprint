"""Whether an object is described from the catalog alone, with no query issued (SPEC 2.2.15)."""

from __future__ import annotations


def described_without_query(
    table_type: str | None,
    *,
    read_rows: bool,
    has_columns: bool,
    external: bool = False,
    opt_in_only: bool = False,
) -> bool:
    """A plain view, external or opt-in-only object is, unless `read_rows` opts it in with columns."""

    return (table_type == "view" or external or opt_in_only) and not (read_rows and has_columns)
