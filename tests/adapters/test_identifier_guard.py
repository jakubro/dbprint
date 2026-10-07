"""Every adapter folds and quotes an identifier only through `adapters/identifiers.py`.

A structural sweep, plus behavioural cases on engines that can hold the spellings.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, LiteralString, cast

import duckdb
import pytest

import dbprint.adapters as adapters_package
from dbprint.adapters import ClickhouseAdapter, DuckdbAdapter, SnowflakeAdapter, identifiers
from dbprint.adapters.base import materialized_name, seed_from_fqn
from dbprint.adapters.identifiers import IdentifierRejected
from dbprint.cli.adapter_registry import ADAPTERS
from dbprint.config.project import ConnectionConfig
from dbprint.conformance.layout import PATH_SEGMENT_RE as VALIDATOR_PATH_SEGMENT_RE
from dbprint.engine import EXIT_PARTIAL, Engine
from tests.adapters.conftest import SnowflakeDialectShim, _adapter_factory_for
from tests.conftest import pg_connect


_PACKAGE = Path(adapters_package.__file__).parent

_OWNERS = frozenset({"identifiers.py", "base.py", "mock.py"})

_SHARED_NAMES = frozenset(
    {
        "IdentifierRejected",
        "Identity",
        "IdentityRegistry",
        "PATH_SEGMENT_RE",
        "UnknownTable",
        "_norm",
        "_quote_ident",
        "_quote_qualified",
        "_split_fqn",
        "column_meta",
        "enforce_table_identifiers",
        "fold",
        "quote",
        "quote_path",
        "reject_column_collisions",
        "string_literal",
        "table_meta",
    },
)


def test_no_adapter_module_folds_quotes_or_splits_a_name_by_hand() -> None:
    hits = [
        f"{path.relative_to(_PACKAGE)}:{line} {what}"
        for path in sorted(_PACKAGE.rglob("*.py"))
        if path.relative_to(_PACKAGE).as_posix() not in _OWNERS
        for line, what in _violations(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert hits == []


_KEYWORD_FOLDS = {
    ("bigquery/introspect.py", "list_tables"): "a table-type keyword",
    ("credentials.py", "masked"): "a table-option keyword",
    ("bigquery/introspect.py", "columns"): "an IS_NULLABLE keyword",
    ("bigquery/introspect.py", "physical_layout"): "an is-partitioning keyword",
    ("bigquery/introspect.py", "_layout_key"): "an is-hidden keyword",
    ("databricks/introspect.py", "_uc_list_candidates"): "a table-type keyword",
    ("databricks/introspect.py", "_legacy_list_candidates"): "an is-temporary flag",
    ("databricks/introspect.py", "_uc_columns"): "an IS_NULLABLE keyword",
    ("duckdb/connection.py", "from_credentials"): "a read-only credential flag",
    ("mysql/introspect.py", "relationships"): "a referential-action keyword",
    ("mysql/introspect.py", "indexes"): "an index-type keyword",
    ("mysql/introspect.py", "comments"): "a storage-engine keyword",
    ("mysql/connection.py", "_opened"): "a server-flavour keyword",
    ("mysql/introspect.py", "list_tables"): "a storage-engine keyword",
    ("redshift/adapter.py", "_databases"): "matches a configured name to the stored spelling",
    ("redshift/introspect.py", "list_tables"): "a table-type keyword",
    ("redshift/introspect.py", "_nullable"): "an is-nullable keyword",
    ("snowflake/adapter.py", "_databases"): "matches a configured name to the stored spelling",
    ("snowflake/ddl.py", "extract_ddl"): "an object-kind keyword",
    ("snowflake/introspect.py", "list_databases"): "the system database's name",
    ("snowflake/introspect.py", "relationships"): "a referential-action keyword",
    ("snowflake/introspect.py", "_is_true"): "a boolean keyword",
}


def test_no_adapter_module_folds_a_name_inline() -> None:
    sites = {
        (path.relative_to(_PACKAGE).as_posix(), function)
        for path in sorted(_PACKAGE.rglob("*.py"))
        if path.relative_to(_PACKAGE).as_posix() not in _OWNERS
        for function in _inline_folds(ast.parse(path.read_text(encoding="utf-8")))
    }

    assert sites - set(_KEYWORD_FOLDS) == set()
    assert set(_KEYWORD_FOLDS) - sites == set(), "stale allowlist entries"


def test_the_fold_sweep_sees_a_planted_fold() -> None:
    tree = ast.parse(
        "def f(rows):\n    return {name.lower(): value for name, value in rows}\n"
        "async def g(name):\n    return name.casefold()\n"
        "NAME = 'Seedbank'.upper()\n",
    )

    assert _inline_folds(tree) == {"f", "g", "<module>"}


def test_the_sweep_flags_what_it_exists_to_catch() -> None:
    snippet = ast.parse(
        "def _quote_ident(name):\n"
        "    return f'\"{name}\"'\n"
        "def f(name):\n"
        "    return ColumnMeta(name=name.lower())\n",
    )

    assert {what for _, what in _violations(snippet)} == {
        "defines _quote_ident",
        "hand-quoted interpolation",
        "constructs ColumnMeta",
    }


_SEED_PINS = [
    (
        "snowflake",
        ("GARDEN", "SEEDBANK", "Accession"),
        226508143,
        "dbprint_sample_1f3326310d803d6f",
    ),
    (
        "postgres",
        ("garden", "Seedbank", "Accession"),
        226508143,
        "dbprint_sample_1f3326310d803d6f",
    ),
    ("mysql", ("Seedbank", "Accession"), 991638664, "dbprint_sample_ed8a3f483b1b3488"),
    ("bigquery", ("Seedbank", "Accession"), 991638664, "dbprint_sample_ed8a3f483b1b3488"),
    ("clickhouse", ("Seedbank", "Accession"), 991638664, "dbprint_sample_ed8a3f483b1b3488"),
    (
        "redshift",
        ("garden", "Seedbank", "Accession"),
        226508143,
        "dbprint_sample_1f3326310d803d6f",
    ),
    (
        "duckdb",
        ("Garden", "Seedbank", "Accession"),
        226508143,
        "dbprint_sample_1f3326310d803d6f",
    ),
    (
        "databricks",
        ("Garden", "Seedbank", "Accession"),
        226508143,
        "dbprint_sample_1f3326310d803d6f",
    ),
]


def test_every_registered_adapter_has_a_seed_pin() -> None:
    assert {vendor for vendor, *_ in _SEED_PINS} == set(ADAPTERS)


def test_the_validator_and_the_producer_share_one_allowlist() -> None:
    assert VALIDATOR_PATH_SEGMENT_RE.pattern == identifiers.PATH_SEGMENT_RE.pattern


@pytest.mark.parametrize(("vendor", "physical", "seed", "sample"), _SEED_PINS)
def test_seeds_and_sample_names_read_the_folded_path(
    vendor: str,
    physical: tuple[str, ...],
    seed: int,
    sample: str,
) -> None:
    module = __import__(ADAPTERS[vendor].__module__.rsplit(".", 1)[0], fromlist=["DIALECT"])
    identity = identifiers.Identity.of(physical, module.DIALECT)

    assert (seed_from_fqn(identity.fqn, 2**31), materialized_name(identity.fqn)) == (seed, sample)


def test_a_case_colliding_column_pair_refuses_its_table_on_a_case_sensitive_engine(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.plot (id Int32, `Status` String, `status` Nullable(String)) "
        "ENGINE = MergeTree ORDER BY id",
    )
    cursor.execute("INSERT INTO seedbank.plot VALUES (1, 'open', NULL), (2, 'open', 'kept')")
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    conn = ConnectionConfig(name="garden", adapter="clickhouse", output=tmp_path / "prints")

    result = Engine(adapter, conn, tmp_path).generate()

    failure = next(t for t in result.tables if t.fqn == "seedbank.plot")
    assert failure.status == "failed"
    assert "Column identifier rejected: seedbank.plot.status" in (failure.error or "")
    assert any(t.status == "ok" for t in result.tables)
    assert result.exit_code == EXIT_PARTIAL


# Engines that cannot hold two column names differing only by case: duckdb (and the Snowflake
# stand-in on it) measured, the rest per the vendors' documentation.
_CANNOT_HOLD_A_CASE_PAIR = {
    "bigquery": "column names are case-insensitive; the pair is a duplicate column",
    "databricks": "Delta rejects two columns differing only by case",
    "duckdb": "identifiers are case-insensitive; the pair is a duplicate column",
    "mysql": "column names are case-insensitive under every collation",
    "snowflake": "the stand-in is duckdb, which cannot hold the pair",
}
_HOLDS_A_CASE_PAIR = ("clickhouse", "postgres", "redshift")


def test_every_adapter_proves_or_explains_the_column_collision() -> None:
    assert set(_HOLDS_A_CASE_PAIR) | set(_CANNOT_HOLD_A_CASE_PAIR) == set(ADAPTERS)


@pytest.mark.parametrize("vendor", ["postgres", "redshift"])
def test_a_case_colliding_column_pair_is_refused_through_the_real_adapter(
    vendor: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    statements = [
        'CREATE TABLE seedbank.plot (id int, "Status" text, status text)',
        "INSERT INTO seedbank.plot VALUES (1, 'open', NULL), (2, 'open', 'kept')",
    ]

    if vendor == "postgres":
        creds = request.getfixturevalue("postgres_test_db")
        with pg_connect(creds) as conn:
            for statement in statements:
                conn.execute(cast(LiteralString, statement))
    else:
        shim = request.getfixturevalue("redshift_postgres_connection")
        for statement in statements:
            shim.execute(statement)

    adapter = _adapter_factory_for(request, vendor)()
    conn = ConnectionConfig(
        name="garden",
        adapter=cast(Any, vendor),
        output=tmp_path / "prints",
        include=("*.seedbank.plot", "*.seedbank.herbarium"),
    )

    result = Engine(adapter, conn, tmp_path).generate()

    statuses = {t.fqn.rsplit(".", 1)[-1]: t for t in result.tables}
    assert statuses["plot"].status == "failed"
    assert "Column identifier rejected" in (statuses["plot"].error or "")
    assert statuses["herbarium"].status == "ok"
    assert result.exit_code == EXIT_PARTIAL


_ARITY = {
    "bigquery": 2,
    "clickhouse": 2,
    "databricks": 3,
    "duckdb": 3,
    "mysql": 2,
    "postgres": 3,
    "redshift": 3,
    "snowflake": 3,
}


def test_every_registered_adapter_has_a_dotted_segment_case() -> None:
    assert set(_ARITY) == set(ADAPTERS)


@pytest.mark.parametrize(
    ("vendor", "level"),
    [(vendor, level) for vendor, arity in sorted(_ARITY.items()) for level in range(arity)],
)
def test_a_period_in_any_segment_is_refused_by_its_own_reason(vendor: str, level: int) -> None:
    module = __import__(ADAPTERS[vendor].__module__.rsplit(".", 1)[0], fromlist=["DIALECT"])
    physical = ["garden", "seedbank", "beds"][-_ARITY[vendor] :]
    physical[level] = f"{physical[level]}.v2"
    identity = identifiers.Identity.of(tuple(physical), module.DIALECT)
    meta = identifiers.table_meta(tuple(physical), "table")

    with pytest.raises(IdentifierRejected) as rejected:
        identifiers.enforce_table_identifiers([(meta, identity.parts)])

    assert "Reason: contains-period" in str(rejected.value)
    assert f"Detail: '{physical[level]}'" in str(rejected.value)


@pytest.mark.parametrize("dotted", ['"beds.v2"', '"plots.old".rows'], ids=["table", "schema"])
def test_a_dotted_duckdb_name_refuses_the_run_before_anything_is_written(
    tmp_path: Path,
    dotted: str,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE TABLE beds (id INTEGER)")
    con.execute('CREATE SCHEMA "plots.old"')
    con.execute(f"CREATE TABLE {dotted} (id INTEGER)")
    con.close()
    conn = ConnectionConfig(name="garden", adapter="duckdb", output=tmp_path / "prints")

    result = Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate()

    assert result.exit_code == 1
    assert "Reason: contains-period" in (result.error or "")
    assert not (tmp_path / "prints").exists() or not any((tmp_path / "prints").rglob("*.yaml"))


def test_a_dotted_clickhouse_table_is_refused_on_the_real_catalog(
    clickhouse_native_connection: Any,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute("CREATE TABLE seedbank.`beds.v2` (id Int32) ENGINE = MergeTree ORDER BY id")
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    adapter.connect()

    with pytest.raises(IdentifierRejected, match="Detail: 'beds.v2'"):
        adapter.list_tables(include=["*"], exclude=[])


@pytest.mark.parametrize(
    ("schema", "create", "fqn", "name_string"),
    [
        (
            "seedbank",
            "CREATE TABLE seedbank.accession (id INTEGER)",
            "memory.seedbank.accession",
            '''"memory"."seedbank"."accession"''',
        ),
        (
            "seedbank",
            'CREATE TABLE seedbank."Accession" (id INTEGER)',
            "memory.seedbank.accession",
            '''"memory"."seedbank"."Accession"''',
        ),
        (
            "seedbank",
            'CREATE TABLE seedbank."seed-lot" (id INTEGER)',
            "memory.seedbank.seed-lot",
            '''"memory"."seedbank"."seed-lot"''',
        ),
        (
            '"Seedbank"',
            'CREATE TABLE "Seedbank".accession (id INTEGER)',
            "memory.seedbank.accession",
            '''"memory"."Seedbank"."accession"''',
        ),
        (
            "seedbank",
            'CREATE VIEW seedbank."lot_view" AS SELECT 1 AS id',
            "memory.seedbank.lot_view",
            '''"memory"."seedbank"."lot_view"''',
        ),
    ],
    ids=["lowercase", "mixed-case-table", "hyphenated-table", "mixed-case-schema", "quoted-view"],
)
def test_snowflake_reads_ddl_by_a_quoted_name_string(
    schema: str,
    create: str,
    fqn: str,
    name_string: str,
) -> None:
    con = duckdb.connect(":memory:")
    con.execute(f"CREATE SCHEMA {schema}")
    con.execute(create)
    recorder = _Recorder(SnowflakeDialectShim(con))
    adapter = SnowflakeAdapter(
        {"account": "a", "user": "u", "password": "p", "warehouse": "w", "role": "r"},
        cursor_factory=lambda _params: recorder,
    )
    adapter.connect()
    adapter.list_tables(include=["*"], exclude=[])

    ddl = adapter.extract_ddl(fqn)

    assert ddl.startswith("CREATE")
    assert any(f"'{name_string}'" in sql for sql in recorder.statements)


class _Recorder:
    def __init__(self, real: Any) -> None:
        self._real = real
        self.statements: list[str] = []

    def execute(self, sql: object, params: object = None) -> _Recorder:
        self.statements.append(str(sql))
        self._real.execute(sql, params)

        return self

    def fetchall(self) -> Any:
        return self._real.fetchall()

    def fetchone(self) -> Any:
        return self._real.fetchone()

    def close(self) -> None:
        self._real.close()


def _violations(tree: ast.AST) -> list[tuple[int, str]]:
    out = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in _SHARED_NAMES:
            out.append((node.lineno, f"defines {node.name}"))

        if isinstance(node, ast.Assign):
            out.extend(
                (node.lineno, f"defines {target.id}")
                for target in node.targets
                if isinstance(target, ast.Name) and target.id in _SHARED_NAMES
            )

        if isinstance(node, ast.Call):
            out.extend(_call_violations(node))

        if isinstance(node, ast.JoinedStr) and _hand_quotes(node):
            out.append((node.lineno, "hand-quoted interpolation"))

    return out


def _call_violations(node: ast.Call) -> list[tuple[int, str]]:
    if isinstance(node.func, ast.Name) and node.func.id in ("ColumnMeta", "TableMeta"):
        return [(node.lineno, f"constructs {node.func.id}")]

    return []


def _hand_quotes(node: ast.JoinedStr) -> bool:
    parts = node.values

    for index, part in enumerate(parts):
        if not isinstance(part, ast.FormattedValue) or not 0 < index < len(parts) - 1:
            continue

        before, after = parts[index - 1], parts[index + 1]

        if (
            isinstance(before, ast.Constant)
            and isinstance(after, ast.Constant)
            and str(before.value)[-1:] in ('"', "`")
            and str(after.value)[:1] == str(before.value)[-1:]
        ):
            return True

    return False


def _inline_folds(tree: ast.AST) -> set[str]:
    functions = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]

    def enclosing(line: int) -> str:
        around = [f for f in functions if f.lineno <= line <= (f.end_lineno or f.lineno)]

        return max(around, key=lambda f: f.lineno).name if around else "<module>"

    return {
        enclosing(node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("lower", "upper", "casefold")
    }
