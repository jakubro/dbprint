"""Whether an object is described from the catalog alone, with no query issued (SPEC 2.2.15)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dbprint.config.read import ReadMode


if TYPE_CHECKING:
    from dbprint.config.project import ConnectionConfig


def described_without_query(
    mode: ReadMode,
    *,
    explicit: bool,
    has_columns: bool,
    view: bool = False,
    external: bool = False,
    opt_in_only: bool = False,
    requires_filter: bool = False,
) -> bool:
    """Catalog-only under `mode: catalog`; a view or a remote object whose catalog lists no
    columns; an external or opt-in-only object no rule reads by an explicit `mode`; or an
    object the engine refuses to read unfiltered, under any mode but `filter`.
    """

    special = view or external or opt_in_only

    return (
        mode == "catalog"
        or (special and not has_columns)
        or ((external or opt_in_only) and not explicit)
        or (requires_filter and mode != "filter")
    )


def sizes_by_estimate(
    conn: ConnectionConfig,
    fqn: str,
    table_type: str | None,
    *,
    external: bool = False,
    opt_in_only: bool = False,
    requires_filter: bool = False,
) -> bool:
    """Whether generate takes a row-count estimate for this object: never a view, nor one described
    from the catalog alone unless a `min_rows` rule writing its mode selects it by name.
    """

    if table_type == "view":
        return False

    settings = conn.settings_for(fqn, "table")
    queried = not described_without_query(
        settings.mode,
        explicit=settings.explicit,
        has_columns=True,
        external=external,
        opt_in_only=opt_in_only,
        requires_filter=requires_filter,
    )

    return queried or conn.min_rows_decides_mode(fqn, "tables")
