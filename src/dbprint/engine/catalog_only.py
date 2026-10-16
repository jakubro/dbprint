"""Whether an object is described from the catalog alone, with no query issued (SPEC 2.2.15)."""

from __future__ import annotations

from dbprint.config.read import ReadMode


def described_without_query(
    mode: ReadMode,
    *,
    explicit: bool,
    has_columns: bool,
    view: bool = False,
    external: bool = False,
    opt_in_only: bool = False,
) -> bool:
    """Catalog-only under `mode: catalog`; a view or a remote object whose catalog lists no
    columns; or an external or opt-in-only object no rule reads by an explicit `mode`.
    """

    special = view or external or opt_in_only

    return (
        mode == "catalog"
        or (special and not has_columns)
        or ((external or opt_in_only) and not explicit)
    )
