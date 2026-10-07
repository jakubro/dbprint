"""Catalog reads: `information_schema` on Unity Catalog, `DESCRIBE`/`SHOW` where it is absent.
The fallback path loses constraints, ordinals and nullability, so it states a weaker fact.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from dbprint.spec.fqn import join as join_fqn
from .connection import DIALECT, Cursor, exec_query
from ..base import (
    ColumnMeta,
    CommentsMeta,
    ForeignKeyMeta,
    IndexMeta,
    PhysicalLayout,
    PhysicalLayoutKey,
    SkippedNamespace,
    TableMeta,
    TableType,
    UniqueKeyMeta,
)
from ..errors import QueryFailed
from ..identifiers import (
    Identity,
    column_meta,
    fold,
    quote,
    quote_path,
    select_tables,
    table_meta,
)


class UnmappedTableType(RuntimeError):
    """Raised when `information_schema.tables.table_type` is not one of the eight values Databricks
    documents - a future ninth value surfaces loudly rather than dropping the object it names.
    """


# The eight documented `information_schema.tables.table_type` values - bare "TABLE" is not among
# them. Streaming, foreign and shallow-clone tables are all tables; a foreign one is also marked
# external, its rows answered by another system.
_TABLE_TYPE_MAP: dict[str, TableType] = {
    "MANAGED": "table",
    "EXTERNAL": "table",
    "STREAMING_TABLE": "table",
    "FOREIGN": "table",
    "MANAGED_SHALLOW_CLONE": "table",
    "EXTERNAL_SHALLOW_CLONE": "table",
    "VIEW": "view",
    "MATERIALIZED_VIEW": "matview",
}

_SYSTEM_SCHEMAS = ("information_schema",)

_Candidate = tuple[TableMeta, tuple[str, str, str]]

# Not enumerated when no catalog is configured: it has no information_schema of its own, and a
# configured `catalog: hive_metastore` still reaches it through the fallback path.
_UNENUMERATED_CATALOGS = ("system", "hive_metastore")


def detect_unity_catalog(cursor: Cursor, catalog: str | None) -> bool:
    """Whether `information_schema` resolves - probed once, at connect time, on `system` when no
    catalog is set since the session's own may be `hive_metastore`, which lacks one.
    """

    source = (
        "system.information_schema.catalogs cat"
        if catalog is None
        else f"{quote(catalog, DIALECT)}.information_schema.tables tbl"
    )
    probe = f"""
        SELECT
          1
        FROM
          {source}
        WHERE
          1 = 0
        """

    try:
        exec_query(cursor, probe).fetchall()
    except Exception as exc:
        if getattr(exc, "timed_out", False):
            raise

        return False
    else:
        return True


def list_tables(
    cursor: Cursor,
    include: list[str],
    exclude: list[str],
    *,
    unity_catalog: bool,
    catalogs: Sequence[str],
) -> tuple[list[_Candidate], tuple[SkippedNamespace, ...]]:
    """Enumerate tables/views in `catalogs`, filtered by selectors, with every catalog whose
    own listing failed - on the fallback path `catalogs` is the session's one.
    """

    candidates: list[_Candidate] = []
    skipped: list[SkippedNamespace] = []

    for catalog in catalogs:
        try:
            candidates += (
                _uc_list_candidates(cursor, catalog)
                if unity_catalog
                else _legacy_list_candidates(cursor, catalog)
            )
        except QueryFailed as exc:
            if not unity_catalog:
                raise

            skipped.append(SkippedNamespace(name=catalog, cause=str(exc)))

    selected = select_tables(candidates, include, exclude)

    return selected, tuple(skipped)


def list_catalogs(cursor: Cursor) -> tuple[str, ...]:
    """Every catalog bound to the workspace that the principal can use, less `system` and
    `hive_metastore`. Unity Catalog only.
    """

    rows = exec_query(
        cursor,
        """
        SELECT
          cat.catalog_name
        FROM
          system.information_schema.catalogs cat
        ORDER BY
          cat.catalog_name
        """,
    ).fetchall()

    return tuple(str(name) for (name,) in rows if fold(str(name)) not in _UNENUMERATED_CATALOGS)


def session_catalog(cursor: Cursor) -> str:
    """The session's current catalog - the one the fallback path lists."""

    row = exec_query(cursor, "SELECT CURRENT_CATALOG()").fetchone()

    if not row or row[0] is None:
        raise RuntimeError("current_catalog() returned no row; cannot name the session catalog")

    return str(row[0])


def _uc_list_candidates(cursor: Cursor, catalog: str) -> list[_Candidate]:
    rows = exec_query(
        cursor,
        f"""
        SELECT
          tbl.table_schema,
          tbl.table_name,
          tbl.table_type
        FROM
          {quote(catalog, DIALECT)}.information_schema.tables tbl
        WHERE
          tbl.table_catalog = ?
        ORDER BY
          tbl.table_schema, tbl.table_name
        """,
        (catalog,),
    ).fetchall()
    out: list[_Candidate] = []

    for schema, name, table_type in rows:
        if fold(schema) in _SYSTEM_SCHEMAS:
            continue

        canonical_type = _TABLE_TYPE_MAP.get(str(table_type).upper())

        if canonical_type is None:
            raise UnmappedTableType(
                f"{schema}.{name}: unrecognised table_type {table_type!r} - not one of "
                f"the eight Databricks documents ({sorted(_TABLE_TYPE_MAP)})",
            )

        physical = (catalog, schema, name)
        external = str(table_type).upper() == "FOREIGN"
        out.append((table_meta(physical, canonical_type, external=external), physical))

    return out


def _legacy_list_candidates(cursor: Cursor, catalog: str) -> list[_Candidate]:
    """`SHOW SCHEMAS` then, per schema, `SHOW TABLES` cross-referenced against `SHOW VIEWS` -
    `SHOW TABLES` does not discriminate a view from a table, so the view name set does.

    `SHOW VIEWS` also returns session-local temporary views regardless of the schema named in
    `IN`, so both loops filter on `isTemporary`.
    """

    out: list[_Candidate] = []
    schema_rows = exec_query(cursor, f"SHOW SCHEMAS IN {quote(catalog, DIALECT)}").fetchall()

    for (schema,) in schema_rows:
        if fold(schema) in _SYSTEM_SCHEMAS:
            continue

        views = {
            fold(name)
            for _ns, name, is_temp, *_rest in exec_query(
                cursor,
                f"SHOW VIEWS IN {quote_path((catalog, schema), DIALECT)}",
            ).fetchall()
            if str(is_temp).lower() not in ("true", "1")
        }
        table_rows = exec_query(
            cursor,
            f"SHOW TABLES IN {quote_path((catalog, schema), DIALECT)}",
        ).fetchall()

        for _ns, name, is_temp, *_rest in table_rows:
            if str(is_temp).lower() in ("true", "1"):
                continue

            physical = (catalog, schema, name)
            table_type: TableType = "view" if fold(name) in views else "table"
            out.append((table_meta(physical, table_type), physical))

    return out


def columns(cursor: Cursor, identity: Identity, *, unity_catalog: bool) -> list[ColumnMeta]:
    """Per-column structural metadata in ordinal order - on the fallback path `DESCRIBE TABLE`
    carries name/type/comment only, so nullable is reported `true` rather than guessed.
    """

    if unity_catalog:
        return _uc_columns(cursor, identity)

    return _legacy_columns(cursor, identity)


def _uc_columns(cursor: Cursor, identity: Identity) -> list[ColumnMeta]:
    """`full_data_type` carries precision/scale/length; `data_type` is only the simple type name.

    `column_default` is documented "Always NULL" and there is no per-column collation here -
    `DESCRIBE TABLE EXTENDED ... AS JSON` is the documented source for both.
    """

    catalog, schema, table = identity.parts
    rows = exec_query(
        cursor,
        f"""
        SELECT
          col.column_name,
          col.ordinal_position,
          col.full_data_type,
          col.is_nullable
        FROM
          {quote(catalog, DIALECT)}.information_schema.columns col
        WHERE
          col.table_catalog = ?
          AND col.table_schema = ?
          AND col.table_name = ?
        ORDER BY
          col.ordinal_position
        """,
        (catalog, schema, table),
    ).fetchall()
    extended = _uc_column_extended(cursor, identity)
    defaults = extended["defaults"]
    collations = extended["collations"]

    return [
        column_meta(
            col_name,
            sql_type=str(data_type),
            nullable=(str(is_nullable).upper() in ("YES", "TRUE")),
            default=defaults.get(fold(col_name)),
            ordinal=int(ordinal),
            collation=collations.get(fold(col_name)),
        )
        for col_name, ordinal, data_type, is_nullable in rows
    ]


def _uc_column_extended(cursor: Cursor, identity: Identity) -> dict[str, dict[str, str]]:
    """`{"defaults": {col: default}, "collations": {col: collation}}` from `DESCRIBE TABLE
    EXTENDED ... AS JSON` - the sources `information_schema.columns` cannot answer.

    Best-effort: an engine refusing the statement leaves every column without an override. A
    collation is published only where it differs from the table's own default (SPEC 2.2.2).
    """

    try:
        row = exec_query(
            cursor,
            f"DESCRIBE TABLE EXTENDED {identity.quoted()} AS JSON",
        ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
    except Exception:  # noqa: BLE001 - refused on this engine/table; defaults/collations unknown
        return {"defaults": {}, "collations": {}}

    table_collation = payload.get("collation")
    defaults: dict[str, str] = {}
    collations: dict[str, str] = {}

    for col in payload.get("columns", []):
        name = fold(str(col.get("name", "")))

        if not name:
            continue

        if col.get("default") is not None:
            defaults[name] = str(col["default"])

        col_type = col.get("type")
        col_collation = col_type.get("collation") if isinstance(col_type, dict) else None

        if col_collation and col_collation != table_collation:
            collations[name] = str(col_collation)

    return {"defaults": defaults, "collations": collations}


def _legacy_columns(cursor: Cursor, identity: Identity) -> list[ColumnMeta]:
    rows = exec_query(cursor, f"DESCRIBE TABLE {identity.quoted()}").fetchall()
    out: list[ColumnMeta] = []

    for ordinal, (col_name, data_type, _comment) in enumerate(rows, start=1):
        # DESCRIBE TABLE appends blank/partition-summary rows once the column list ends.
        if not col_name or col_name.startswith("#"):
            break

        out.append(
            column_meta(
                col_name,
                sql_type=str(data_type),
                nullable=True,
                default=None,
                ordinal=ordinal,
            ),
        )

    return out


def default_collation(cursor: Cursor) -> str:
    """The session's default collation (SPEC 2.2.2): `UTF8_BINARY` unless configured otherwise.

    It governs DML only, and no catalog surface publishes a schema's declared default, so this
    connection-level value is the best available answer; `_uc_columns` carries the per-column truth.
    """

    try:
        row = exec_query(cursor, "SET spark.sql.session.collation.default").fetchone()
    except Exception:  # noqa: BLE001 - an engine with no collation setting at all
        return "UTF8_BINARY"

    if not row or len(row) < 2 or not row[1] or str(row[1]) == "<undefined>":
        return "UTF8_BINARY"

    return str(row[1])


def relationships(
    cursor: Cursor,
    identity: Identity,
    *,
    unity_catalog: bool,
) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs on Unity Catalog, informational only - `ENFORCED` is always `'NO'`.

    Paired by `position_in_unique_constraint`, not ordinal; a target key is read in its own catalog.
    """

    if not unity_catalog:
        return []

    catalog, schema, table = identity.parts
    rows = exec_query(
        cursor,
        f"""
        SELECT
          kcu.constraint_name,
          kcu.column_name,
          kcu.position_in_unique_constraint,
          rfc.unique_constraint_catalog,
          rfc.unique_constraint_schema,
          rfc.unique_constraint_name

        FROM
          {quote(catalog, DIALECT)}.information_schema.key_column_usage kcu
          JOIN {quote(catalog, DIALECT)}.information_schema.referential_constraints rfc ON
            rfc.constraint_catalog = kcu.constraint_catalog
            AND rfc.constraint_schema = kcu.constraint_schema
            AND rfc.constraint_name = kcu.constraint_name

        WHERE
          kcu.table_catalog = ?
          AND kcu.table_schema = ?
          AND kcu.table_name = ?
        ORDER BY
          kcu.constraint_name, kcu.ordinal_position
        """,
        (catalog, schema, table),
    ).fetchall()

    if not rows:
        return []

    target_ordinals = _pk_ordinals_by_constraint(
        cursor,
        {
            (ref_catalog, ref_schema, ref_name)
            for _, _, _, ref_catalog, ref_schema, ref_name in rows
        },
    )
    src_cols: dict[str, list[str]] = {}
    src_positions: dict[str, list[int]] = {}
    targets: dict[str, tuple[str, str, str]] = {}
    order: list[str] = []

    for name, column, position, ref_catalog, ref_schema, ref_constraint in rows:
        if name not in src_cols:
            src_cols[name] = []
            src_positions[name] = []
            targets[name] = (ref_catalog, ref_schema, ref_constraint)
            order.append(name)

        src_cols[name].append(fold(column))
        src_positions[name].append(int(position))

    out: list[ForeignKeyMeta] = []

    for name in order:
        ref_catalog, ref_schema, ref_constraint = targets[name]
        target = target_ordinals.get((ref_catalog, ref_schema, ref_constraint))

        if target is None:
            continue

        ref_table, ref_by_ordinal = target
        resolved = [ref_by_ordinal.get(p) for p in src_positions[name]]

        if any(r is None for r in resolved):
            continue

        target_table = join_fqn((fold(ref_catalog), fold(ref_schema), fold(ref_table)))

        out.append(
            ForeignKeyMeta(
                column=tuple(src_cols[name]),
                target_table=target_table,
                target_column=cast("tuple[str, ...]", tuple(resolved)),
                on_delete="NO ACTION",
                on_update="NO ACTION",
                constraint_name=name,
            ),
        )

    return out


def _pk_ordinals_by_constraint(
    cursor: Cursor,
    constraints: set[tuple[str, str, str]],
) -> dict[tuple[str, str, str], tuple[str, dict[int, str]]]:
    """`{(catalog, schema, constraint_name): (table_name, {ordinal: column_name})}` for the
    referenced PK/UNIQUE side - keyed by ordinal, so `position_in_unique_constraint` resolves it.
    """

    out: dict[tuple[str, str, str], tuple[str, dict[int, str]]] = {}

    for catalog, schema, constraint_name in constraints:
        # Each catalog's information_schema describes that catalog alone.
        rows = exec_query(
            cursor,
            f"""
            SELECT
              kcu.table_name,
              kcu.ordinal_position,
              kcu.column_name
            FROM
              {quote(catalog, DIALECT)}.information_schema.key_column_usage kcu
            WHERE
              kcu.table_catalog = ?
              AND kcu.table_schema = ?
              AND kcu.constraint_name = ?
            """,
            (catalog, schema, constraint_name),
        ).fetchall()

        if rows:
            table_name = str(rows[0][0])
            by_ordinal = {int(ordinal): fold(col) for _t, ordinal, col in rows}
            out[(catalog, schema, constraint_name)] = (table_name, by_ordinal)

    return out


def indexes(cursor: Cursor, identity: Identity) -> list[IndexMeta]:
    """No index concept exists on Databricks; always empty."""

    del cursor, identity

    return []


def unique_keys(cursor: Cursor, identity: Identity, *, unity_catalog: bool) -> list[UniqueKeyMeta]:
    """Declared-unique column groups; PRIMARY first, then named UNIQUE constraints - Unity
    Catalog only, the legacy path having no constraint surface at all.
    """

    if not unity_catalog:
        return []

    catalog, schema, table = identity.parts
    rows = exec_query(
        cursor,
        f"""
        SELECT
          tcn.constraint_name,
          tcn.constraint_type,
          kcu.column_name

        FROM
          {quote(catalog, DIALECT)}.information_schema.table_constraints tcn
          JOIN {quote(catalog, DIALECT)}.information_schema.key_column_usage kcu ON
            kcu.constraint_catalog = tcn.constraint_catalog
            AND kcu.constraint_schema = tcn.constraint_schema
            AND kcu.constraint_name = tcn.constraint_name

        WHERE
          tcn.table_catalog = ?
          AND tcn.table_schema = ?
          AND tcn.table_name = ?
          AND tcn.constraint_type IN ('PRIMARY KEY', 'UNIQUE')

        ORDER BY
          tcn.constraint_name, kcu.ordinal_position
        """,
        (catalog, schema, table),
    ).fetchall()

    grouped: dict[str, list[str]] = {}
    is_primary: dict[str, bool] = {}

    for name, constraint_type, column in rows:
        grouped.setdefault(name, []).append(fold(column))
        is_primary[name] = constraint_type == "PRIMARY KEY"

    ordered = sorted(grouped, key=lambda name: (not is_primary[name], name))

    return [
        UniqueKeyMeta(columns=tuple(grouped[name]), primary=is_primary[name]) for name in ordered
    ]


def physical_layout(cursor: Cursor, identity: Identity) -> PhysicalLayout | None:
    """Declared clustering or partitioning key via `DESCRIBE DETAIL`, clustering winning when both
    are declared - `DESCRIBE TABLE EXTENDED` is the fallback for a view, which it refuses.
    """

    name = identity.quoted()
    row = _describe_detail(cursor, name)

    if row is not None:
        cluster_cols = _str_list(row.get("clusteringColumns"))

        if cluster_cols:
            return PhysicalLayout(
                mechanism="cluster",
                keys=tuple(PhysicalLayoutKey(expression=c, column=fold(c)) for c in cluster_cols),
            )

        partition_cols = _str_list(row.get("partitionColumns"))
    else:
        partition_cols = _describe_extended_fallback(cursor, name)["partition_columns"]

    if partition_cols:
        return PhysicalLayout(
            mechanism="partition",
            keys=tuple(PhysicalLayoutKey(expression=c, column=fold(c)) for c in partition_cols),
        )

    return None


def view_dependencies(cursor: Cursor) -> dict[str, tuple[str, ...]] | None:
    """`None` for the whole connection: no reliable source exists - `view_table_usage` is absent,
    `view_definition` needs ownership, and lineage is silent for an unqueried view.
    """

    del cursor

    return None


def comments(cursor: Cursor, identity: Identity) -> CommentsMeta:
    """Table comment via `DESCRIBE DETAIL.description`, per-column via `DESCRIBE TABLE` - neither
    is `information_schema`-gated, and a view falls back to `DESCRIBE TABLE EXTENDED`'s Comment row.
    """

    name = identity.quoted()
    detail = _describe_detail(cursor, name)
    table_comment = (
        detail.get("description")
        if detail is not None
        else _describe_extended_fallback(cursor, name)["comment"]
    )
    rows = exec_query(cursor, f"DESCRIBE TABLE {name}").fetchall()
    col_comments: dict[str, str] = {}

    for col_name, _data_type, comment in rows:
        if not col_name or col_name.startswith("#"):
            break

        if comment:
            col_comments[fold(col_name)] = comment

    return CommentsMeta(
        table=str(table_comment) if table_comment else None,
        columns=col_comments,
    )


def estimate_row_count(cursor: Cursor, identity: Identity) -> int | None:
    """`DESCRIBE TABLE EXTENDED ... AS JSON` -> `statistics.num_rows`; `None` on any failure -
    never a `COUNT(*)` fallback, a silent scan being worse than an absent estimate.
    """

    try:
        row = exec_query(
            cursor,
            f"DESCRIBE TABLE EXTENDED {identity.quoted()} AS JSON",
        ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
        num_rows = payload.get("statistics", {}).get("num_rows")
    except Exception:  # noqa: BLE001 - refused on this engine/table; the catalog has no estimate
        return None

    return int(num_rows) if num_rows is not None else None


def _describe_detail(cursor: Cursor, name: str) -> dict[str, object] | None:
    """`DESCRIBE DETAIL` as a name-keyed dict, via the cursor's own column order."""

    try:
        result = exec_query(cursor, f"DESCRIBE DETAIL {name}")
        row = result.fetchone()
    except Exception as exc:
        if getattr(exc, "timed_out", False):
            raise

        return None

    if row is None:
        return None

    names = [d[0] for d in (getattr(result, "description", None) or [])]

    if not names or len(names) != len(row):
        return None

    return dict(zip(names, row, strict=True))


def _describe_extended_fallback(cursor: Cursor, name: str) -> dict[str, Any]:
    """`DESCRIBE TABLE EXTENDED`'s partition-columns section and detailed-info `Comment` row - the
    fallback for an object `DESCRIBE DETAIL` refuses, in a column-list-then-name-value format.
    """

    try:
        rows = exec_query(cursor, f"DESCRIBE TABLE EXTENDED {name}").fetchall()
    except Exception:  # noqa: BLE001 - genuinely could not ask either way
        return {"comment": None, "partition_columns": []}

    comment: str | None = None
    partition_columns: list[str] = []
    in_partition_section = False

    for row in rows:
        first = str(row[0]) if row and row[0] is not None else ""

        if first == "# Partition Information":
            in_partition_section = True
            continue

        if in_partition_section:
            if first.startswith("# col_name"):
                continue

            if not first or first.startswith("#"):
                in_partition_section = False
            else:
                partition_columns.append(first)

            continue

        if first == "Comment" and len(row) > 1 and row[1]:
            comment = str(row[1])

    return {"comment": comment, "partition_columns": partition_columns}


def _str_list(value: object) -> list[str]:
    """A `DESCRIBE DETAIL` array field as `list[str]`; anything else reads as absent."""

    if not isinstance(value, list):
        return []

    return [str(v) for v in value]
