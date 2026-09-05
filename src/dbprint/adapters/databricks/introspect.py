"""Catalog reads: `information_schema` on Unity Catalog, `DESCRIBE`/`SHOW` where it is absent.
The fallback path loses constraints, ordinals and nullability, so it states a weaker fact.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

from dbprint.config.selectors import expand
from .connection import Cursor, exec_query
from ..base import (
    ColumnMeta,
    CommentsMeta,
    FkAction,
    ForeignKeyMeta,
    IndexMeta,
    PhysicalLayout,
    PhysicalLayoutKey,
    TableMeta,
    TableType,
    UniqueKeyMeta,
)


class IdentifierRejected(ValueError):
    """Raised when a Databricks identifier fails SPEC 1.5 path-segment rules; format SPEC 1.5.5."""


class UnmappedTableType(RuntimeError):
    """Raised when `information_schema.tables.table_type` is not one of the eight values Databricks
    documents - a future ninth value surfaces loudly rather than dropping the object it names.
    """


PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*$")

# The eight documented `information_schema.tables.table_type` values - bare "TABLE" is not among
# them. Streaming, foreign and shallow-clone tables all behave as tables; only a view differs.
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

_FK_ACTIONS: dict[str, FkAction] = {
    "NO ACTION": "NO ACTION",
    "CASCADE": "CASCADE",
    "SET NULL": "SET NULL",
    "SET DEFAULT": "SET DEFAULT",
    "RESTRICT": "RESTRICT",
}

_SYSTEM_SCHEMAS = ("information_schema",)

_Candidate = tuple[TableMeta, tuple[str, str]]


def detect_unity_catalog(cursor: Cursor) -> bool:
    """Whether `information_schema` resolves on this connection - probed once, at connect time;
    it does not exist outside Unity Catalog, answering `TABLE_OR_VIEW_NOT_FOUND` there.
    """

    try:
        exec_query(cursor, "SELECT 1 FROM information_schema.tables WHERE 1 = 0").fetchall()
    except Exception:  # noqa: BLE001 - any failure means "no Unity Catalog", not one driver error
        return False
    else:
        return True


def list_tables(
    cursor: Cursor,
    include: list[str],
    exclude: list[str],
    *,
    unity_catalog: bool,
) -> list[TableMeta]:
    """Enumerate tables/views in the connected catalog, filtered by selectors."""

    candidates = _uc_list_candidates(cursor) if unity_catalog else _legacy_list_candidates(cursor)
    in_scope = set(
        expand(
            [meta.fqn for meta, _ in candidates],
            config_include=include,
            config_exclude=exclude,
        ),
    )
    selected = [entry for entry in candidates if entry[0].fqn in in_scope]
    _enforce_identifier_rules(selected)

    return [meta for meta, _ in selected]


def _uc_list_candidates(cursor: Cursor) -> list[_Candidate]:
    rows = exec_query(
        cursor,
        """
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        WHERE table_catalog = current_catalog()
        ORDER BY table_schema, table_name
        """,
    ).fetchall()
    out: list[_Candidate] = []

    for schema, name, table_type in rows:
        if _norm(schema) in _SYSTEM_SCHEMAS:
            continue

        canonical_type = _TABLE_TYPE_MAP.get(str(table_type).upper())

        if canonical_type is None:
            raise UnmappedTableType(
                f"{schema}.{name}: unrecognised table_type {table_type!r} - not one of "
                f"the eight Databricks documents ({sorted(_TABLE_TYPE_MAP)})",
            )

        path = (_norm(schema), _norm(name))
        out.append(
            (
                TableMeta(fqn=".".join(path), type=canonical_type, namespace_path=path),
                (schema, name),
            ),
        )

    return out


def _legacy_list_candidates(cursor: Cursor) -> list[_Candidate]:
    """`SHOW SCHEMAS` then, per schema, `SHOW TABLES` cross-referenced against `SHOW VIEWS` -
    `SHOW TABLES` does not discriminate a view from a table, so the view name set does.

    `SHOW VIEWS` also returns session-local temporary views regardless of the schema named in
    `IN`, so both loops filter on `isTemporary`.
    """

    out: list[_Candidate] = []
    schema_rows = exec_query(cursor, "SHOW SCHEMAS").fetchall()

    for (schema,) in schema_rows:
        if _norm(schema) in _SYSTEM_SCHEMAS:
            continue

        views = {
            _norm(name)
            for _ns, name, is_temp, *_rest in exec_query(
                cursor,
                f"SHOW VIEWS IN `{schema}`",
            ).fetchall()
            if str(is_temp).lower() not in ("true", "1")
        }
        table_rows = exec_query(cursor, f"SHOW TABLES IN `{schema}`").fetchall()

        for _ns, name, is_temp, *_rest in table_rows:
            if str(is_temp).lower() in ("true", "1"):
                continue

            path = (_norm(schema), _norm(name))
            table_type: TableType = "view" if _norm(name) in views else "table"
            out.append(
                (
                    TableMeta(fqn=".".join(path), type=table_type, namespace_path=path),
                    (schema, name),
                ),
            )

    return out


def columns(cursor: Cursor, fqn: str, *, unity_catalog: bool) -> list[ColumnMeta]:
    """Per-column structural metadata in ordinal order - on the fallback path `DESCRIBE TABLE`
    carries name/type/comment only, so nullable is reported `true` rather than guessed.
    """

    if unity_catalog:
        return _uc_columns(cursor, fqn)

    return _legacy_columns(cursor, fqn)


def _uc_columns(cursor: Cursor, fqn: str) -> list[ColumnMeta]:
    """`full_data_type` carries precision/scale/length; `data_type` is only the simple type name.

    `column_default` is documented "Always NULL" and there is no per-column collation here -
    `DESCRIBE TABLE EXTENDED ... AS JSON` is the documented source for both.
    """

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT column_name, ordinal_position, full_data_type, is_nullable
        FROM information_schema.columns
        WHERE table_catalog = current_catalog() AND table_schema = ? AND table_name = ?
        ORDER BY ordinal_position
        """,
        (schema, table),
    ).fetchall()
    extended = _uc_column_extended(cursor, schema, table)
    defaults = extended["defaults"]
    collations = extended["collations"]

    return [
        ColumnMeta(
            name=_norm(col_name),
            sql_type=str(data_type),
            nullable=(str(is_nullable).upper() in ("YES", "TRUE")),
            default=defaults.get(_norm(col_name)),
            ordinal=int(ordinal),
            physical_name=None if col_name == _norm(col_name) else col_name,
            collation=collations.get(_norm(col_name)),
        )
        for col_name, ordinal, data_type, is_nullable in rows
    ]


def _uc_column_extended(cursor: Cursor, schema: str, table: str) -> dict[str, dict[str, str]]:
    """`{"defaults": {col: default}, "collations": {col: collation}}` from `DESCRIBE TABLE
    EXTENDED ... AS JSON` - the sources `information_schema.columns` cannot answer.

    Best-effort: an engine refusing the statement leaves every column without an override. A
    collation is published only where it differs from the table's own default (SPEC 2.2.2).
    """

    try:
        row = exec_query(
            cursor,
            f"DESCRIBE TABLE EXTENDED `{schema}`.`{table}` AS JSON",
        ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
    except Exception:  # noqa: BLE001 - refused on this engine/table; defaults/collations unknown
        return {"defaults": {}, "collations": {}}

    table_collation = payload.get("collation")
    defaults: dict[str, str] = {}
    collations: dict[str, str] = {}

    for col in payload.get("columns", []):
        name = _norm(str(col.get("name", "")))

        if not name:
            continue

        if col.get("default") is not None:
            defaults[name] = str(col["default"])

        col_type = col.get("type")
        col_collation = col_type.get("collation") if isinstance(col_type, dict) else None

        if col_collation and col_collation != table_collation:
            collations[name] = str(col_collation)

    return {"defaults": defaults, "collations": collations}


def _legacy_columns(cursor: Cursor, fqn: str) -> list[ColumnMeta]:
    schema, table = _split_fqn(fqn)
    rows = exec_query(cursor, f"DESCRIBE TABLE `{schema}`.`{table}`").fetchall()
    out: list[ColumnMeta] = []

    for ordinal, (col_name, data_type, _comment) in enumerate(rows, start=1):
        # DESCRIBE TABLE appends blank/partition-summary rows once the column list ends.
        if not col_name or col_name.startswith("#"):
            break

        out.append(
            ColumnMeta(
                name=_norm(col_name),
                sql_type=str(data_type),
                nullable=True,
                default=None,
                ordinal=ordinal,
                physical_name=None if col_name == _norm(col_name) else col_name,
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


def relationships(cursor: Cursor, fqn: str, *, unity_catalog: bool) -> list[ForeignKeyMeta]:
    """Declared outgoing FKs, informational only - `TABLE_CONSTRAINTS.ENFORCED` is documented
    always `'NO'`. Unity Catalog only: the legacy path has no constraint surface at all.

    Paired via `key_column_usage.position_in_unique_constraint`, never by zipping the two sides'
    `ordinal_position`; resolved by catalog as well as schema, so a cross-catalog FK survives.
    """

    if not unity_catalog:
        return []

    schema, table = _split_fqn(fqn)
    current_catalog = _current_catalog(cursor)
    rows = exec_query(
        cursor,
        """
        SELECT
            kcu.constraint_name, kcu.column_name, kcu.position_in_unique_constraint,
            rc.unique_constraint_catalog, rc.unique_constraint_schema, rc.unique_constraint_name
        FROM information_schema.key_column_usage kcu
        JOIN information_schema.referential_constraints rc
          ON rc.constraint_catalog = kcu.constraint_catalog
         AND rc.constraint_schema = kcu.constraint_schema
         AND rc.constraint_name = kcu.constraint_name
        WHERE kcu.table_catalog = current_catalog()
          AND kcu.table_schema = ? AND kcu.table_name = ?
        ORDER BY kcu.constraint_name, kcu.ordinal_position
        """,
        (schema, table),
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

        src_cols[name].append(_norm(column))
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

        # A target in another catalog is not in this print (`list_tables` enumerates
        # `current_catalog()` alone), so it keeps its catalog segment instead of naming a local one.
        target_table = f"{_norm(ref_schema)}.{_norm(ref_table)}"

        if _norm(ref_catalog) != _norm(current_catalog):
            target_table = f"{_norm(ref_catalog)}.{target_table}"

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


def _current_catalog(cursor: Cursor) -> str:
    """The catalog `list_tables` enumerates - the one an FK target may fall outside of."""

    row = exec_query(cursor, "SELECT current_catalog()").fetchone()

    if not row or row[0] is None:
        # Failing open to "" would read as "no catalog matches", prefixing EVERY target - including
        # same-catalog ones, which then resolve against nothing. Raise, as every other failure here.
        raise RuntimeError("current_catalog() returned no row; cannot place a foreign-key target")

    return str(row[0])


def _pk_ordinals_by_constraint(
    cursor: Cursor,
    constraints: set[tuple[str, str, str]],
) -> dict[tuple[str, str, str], tuple[str, dict[int, str]]]:
    """`{(catalog, schema, constraint_name): (table_name, {ordinal: column_name})}` for the
    referenced PK/UNIQUE side - keyed by ordinal, so `position_in_unique_constraint` resolves it.
    """

    out: dict[tuple[str, str, str], tuple[str, dict[int, str]]] = {}

    for catalog, schema, constraint_name in constraints:
        rows = exec_query(
            cursor,
            """
            SELECT table_name, ordinal_position, column_name
            FROM information_schema.key_column_usage
            WHERE table_catalog = ? AND table_schema = ? AND constraint_name = ?
            """,
            (catalog, schema, constraint_name),
        ).fetchall()

        if rows:
            table_name = str(rows[0][0])
            by_ordinal = {int(ordinal): _norm(col) for _t, ordinal, col in rows}
            out[(catalog, schema, constraint_name)] = (table_name, by_ordinal)

    return out


def indexes(cursor: Cursor, fqn: str) -> list[IndexMeta]:
    """No index concept exists on Databricks; always empty."""

    del cursor, fqn

    return []


def unique_keys(cursor: Cursor, fqn: str, *, unity_catalog: bool) -> list[UniqueKeyMeta]:
    """Declared-unique column groups; PRIMARY first, then named UNIQUE constraints - Unity
    Catalog only, the legacy path having no constraint surface at all.
    """

    if not unity_catalog:
        return []

    schema, table = _split_fqn(fqn)
    rows = exec_query(
        cursor,
        """
        SELECT tc.constraint_name, tc.constraint_type, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_catalog = tc.constraint_catalog
         AND kcu.constraint_schema = tc.constraint_schema
         AND kcu.constraint_name = tc.constraint_name
        WHERE tc.table_catalog = current_catalog()
          AND tc.table_schema = ? AND tc.table_name = ?
          AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
        ORDER BY tc.constraint_name, kcu.ordinal_position
        """,
        (schema, table),
    ).fetchall()

    grouped: dict[str, list[str]] = {}
    is_primary: dict[str, bool] = {}

    for name, constraint_type, column in rows:
        grouped.setdefault(name, []).append(_norm(column))
        is_primary[name] = constraint_type == "PRIMARY KEY"

    ordered = sorted(grouped, key=lambda name: (not is_primary[name], name))

    return [
        UniqueKeyMeta(columns=tuple(grouped[name]), primary=is_primary[name]) for name in ordered
    ]


def physical_layout(cursor: Cursor, fqn: str) -> PhysicalLayout | None:
    """Declared clustering or partitioning key via `DESCRIBE DETAIL`, clustering winning when both
    are declared - `DESCRIBE TABLE EXTENDED` is the fallback for a view, which it refuses.
    """

    schema, table = _split_fqn(fqn)
    row = _describe_detail(cursor, schema, table)

    if row is not None:
        cluster_cols = _str_list(row.get("clusteringColumns"))

        if cluster_cols:
            return PhysicalLayout(
                mechanism="cluster",
                keys=tuple(PhysicalLayoutKey(expression=c, column=_norm(c)) for c in cluster_cols),
            )

        partition_cols = _str_list(row.get("partitionColumns"))
    else:
        partition_cols = _describe_extended_fallback(cursor, schema, table)["partition_columns"]

    if partition_cols:
        return PhysicalLayout(
            mechanism="partition",
            keys=tuple(PhysicalLayoutKey(expression=c, column=_norm(c)) for c in partition_cols),
        )

    return None


def view_dependencies(cursor: Cursor) -> dict[str, tuple[str, ...]] | None:
    """`None` for the whole connection: no reliable source exists - `view_table_usage` is absent,
    `view_definition` needs ownership, and lineage is silent for an unqueried view.
    """

    del cursor

    return None


def comments(cursor: Cursor, fqn: str) -> CommentsMeta:
    """Table comment via `DESCRIBE DETAIL.description`, per-column via `DESCRIBE TABLE` - neither
    is `information_schema`-gated, and a view falls back to `DESCRIBE TABLE EXTENDED`'s Comment row.
    """

    schema, table = _split_fqn(fqn)
    detail = _describe_detail(cursor, schema, table)
    table_comment = (
        detail.get("description")
        if detail is not None
        else _describe_extended_fallback(cursor, schema, table)["comment"]
    )
    rows = exec_query(cursor, f"DESCRIBE TABLE `{schema}`.`{table}`").fetchall()
    col_comments: dict[str, str] = {}

    for col_name, _data_type, comment in rows:
        if not col_name or col_name.startswith("#"):
            break

        if comment:
            col_comments[_norm(col_name)] = comment

    return CommentsMeta(
        table=str(table_comment) if table_comment else None,
        columns=col_comments,
    )


def estimate_row_count(cursor: Cursor, fqn: str) -> int | None:
    """`DESCRIBE TABLE EXTENDED ... AS JSON` -> `statistics.num_rows`; `None` on any failure -
    never a `COUNT(*)` fallback, a silent scan being worse than an absent estimate.
    """

    schema, table = _split_fqn(fqn)

    try:
        row = exec_query(
            cursor,
            f"DESCRIBE TABLE EXTENDED `{schema}`.`{table}` AS JSON",
        ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
        num_rows = payload.get("statistics", {}).get("num_rows")
    except Exception:  # noqa: BLE001 - refused on this engine/table; the catalog has no estimate
        return None

    return int(num_rows) if num_rows is not None else None


def _describe_detail(cursor: Cursor, schema: str, table: str) -> dict[str, object] | None:
    """`DESCRIBE DETAIL` as a name-keyed dict, via the cursor's own column order."""

    try:
        result = exec_query(cursor, f"DESCRIBE DETAIL `{schema}`.`{table}`")
        row = result.fetchone()
    except Exception:  # noqa: BLE001 - not every object DESCRIBE DETAIL accepts (e.g. a view)
        return None

    if row is None:
        return None

    names = [d[0] for d in (getattr(result, "description", None) or [])]

    if not names or len(names) != len(row):
        return None

    return dict(zip(names, row))


def _describe_extended_fallback(cursor: Cursor, schema: str, table: str) -> dict[str, Any]:
    """`DESCRIBE TABLE EXTENDED`'s partition-columns section and detailed-info `Comment` row - the
    fallback for an object `DESCRIBE DETAIL` refuses, in a column-list-then-name-value format.
    """

    try:
        rows = exec_query(cursor, f"DESCRIBE TABLE EXTENDED `{schema}`.`{table}`").fetchall()
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


def _enforce_identifier_rules(selected: list[_Candidate]) -> None:
    """Reject identifiers that violate SPEC 1.5 before any artifact is written.

    No physical spelling is carried afterwards: both metastores store securable names lowercase.
    """

    seen: dict[str, tuple[str, str]] = {}

    for meta, parts in selected:
        for seg in meta.namespace_path:
            if seg.startswith("."):
                raise IdentifierRejected(_reject_message(meta.fqn, "leading-period", seg))

            if not PATH_SEGMENT_RE.match(seg):
                raise IdentifierRejected(
                    _reject_message(meta.fqn, "contains-unsafe-character", seg),
                )

        previous = seen.get(meta.fqn)

        if previous is not None and previous != parts:
            raise IdentifierRejected(
                _reject_message(
                    meta.fqn,
                    f"case-collides-with-{'.'.join(previous)}",
                    ".".join(parts),
                ),
            )

        seen[meta.fqn] = parts


def _reject_message(fqn: str, reason: str, detail: str) -> str:
    """SPEC 1.5.5 error format - verbatim."""

    return (
        f"ERROR: Table identifier rejected: {fqn}\n"
        f"  Reason: {reason}\n"
        f"  Detail: {detail!r}\n"
        f"  Resolution: Either rename the identifier in the database, OR "
        f"exclude it via .dbprint.yaml selectors:\n"
        f"    exclude:\n"
        f'      - "{fqn}"'
    )


def _split_fqn(fqn: str) -> tuple[str, str]:
    if "." not in fqn:
        raise ValueError(f"Databricks FQN must be 'schema.table', got {fqn!r}")

    schema, _, table = fqn.partition(".")

    return schema, table


def _norm(name: str) -> str:
    """Lowercase an identifier - the `columns` map key (SPEC 2.2.1)."""

    return name.lower()
