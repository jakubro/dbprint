"""An exact number is published exactly by every adapter: one cell rule, one rounding rule.

A structural guard over every `stats.py`, and exact decimals past binary64 read back from every substrate.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
from decimal import Decimal
from pathlib import Path
from typing import Any, LiteralString, cast

import duckdb
import pytest
import yaml

from dbprint.adapters import DuckdbAdapter, StatisticsConfig
from dbprint.adapters import base as adapter_base
from dbprint.adapters import statements as adapter_statements
from dbprint.cli.adapter_registry import ADAPTERS
from dbprint.config.project import ConnectionConfig
from dbprint.engine import Engine, GenerateRequest
from dbprint.engine.yaml_dumper import dump_yaml
from dbprint.spec import rounding
from tests.adapters.conftest import SQL_PARAMS, _adapter_factory_for, _mysql_exec_many
from tests.conftest import pg_connect


_VENDORS_WITH_STATS = sorted(
    name
    for name in ADAPTERS
    if importlib.util.find_spec(f"dbprint.adapters.{name}.stats") is not None
)

# Catalog estimates, never cells: Postgres's `pg_stats.n_distinct` and a scaled row estimate.
_FLOAT_ALLOWLIST = {("postgres", "_approximate_cardinality"), ("shared", "scoped_estimate")}

_CELL_RULES = frozenset({"measured_value", "round_statistic"})
_TEXT_BOUNDS = frozenset(
    {"probe_timeline", "compute_populated_windows", "timeline", "windows_from_row"},
)
# The shared statement readers apply `measured_text` themselves, so delegating to one is ruled.
_SHARED_TEXT_READERS = frozenset({"timeline", "populated_windows", "windows_from_row"})

_ROWS = 60
_ID_BASE = 2**64
_STOCK_BASE = Decimal("12345678901234.567891")
_MASS_STEP = Decimal("1E-18")


@pytest.mark.parametrize("vendor", _VENDORS_WITH_STATS)
def test_every_adapter_publishes_cells_through_the_shared_rule(vendor: str) -> None:
    module = importlib.import_module(f"dbprint.adapters.{vendor}.stats")
    tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))

    assert not hasattr(module, "_measured_value")
    assert not hasattr(module, "_iso_or_value")
    assert not hasattr(module, "_round_numeric")
    assert getattr(module, "measured_value", rounding.measured_value) is rounding.measured_value
    assert getattr(module, "round_statistic", rounding.round_statistic) is rounding.round_statistic
    assert (
        module.render_text
        is importlib.import_module(
            f"dbprint.adapters.{vendor}.rendering",
        ).render_text
    )
    assert _cell_violations(tree, vendor) == []


@pytest.mark.parametrize("module", [adapter_base, adapter_statements], ids=["base", "statements"])
def test_the_shared_assembly_publishes_cells_through_the_shared_rule(module: Any) -> None:
    """Module-level functions only: the `Adapter` declarations share names with statement builders."""

    tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
    functions = ast.Module(
        body=[node for node in tree.body if isinstance(node, ast.FunctionDef)],
        type_ignores=[],
    )

    assert _cell_violations(functions, "shared") == []


def test_the_structural_guard_flags_what_it_exists_to_catch() -> None:
    snippet = ast.parse(
        "def f(rows, v):\n"
        "    x = float(v)\n"
        "    return [ValueCount(value=r, count=1) for r in rows]\n"
        "def g(c, s):\n"
        "    return _approximate_distribution_via_top_n(c, s, 'x', 'x', 1, None, str)\n"
        "def h(row, keys):\n"
        "    percentiles = {k: v for k, v in zip(keys, row)}\n"
        "    return Range(min=row[0], max=None)\n"
        "def probe_timeline(rows):\n"
        "    return tuple((r[0], r[1]) for r in rows)\n"
        "def _fetch_value_list(cn, source):\n"
        "    return f'SELECT {cn} AS rendered FROM {source}'\n",
    )

    assert {what for _, what in _cell_violations(snippet, "none")} == {
        "float() on a cell in f",
        "ValueCount value bypasses measured_value in f",
        "top-N transform bypasses measured_value in g",
        "percentile bypasses measured_value in h",
        "Range min bypasses measured_value in h",
        "unmeasured text bound in probe_timeline",
        "native value-list select in _fetch_value_list",
    }


class TestTheCellRule:
    def test_a_38_digit_fraction_keeps_every_digit(self) -> None:
        value = Decimal("12345678901234567890.123456789012345678")

        assert rounding.measured_value(value) == value

    def test_an_integral_decimal_becomes_an_integer(self) -> None:
        assert rounding.measured_value(Decimal("-20.0")) == -20
        assert type(rounding.measured_value(Decimal("-20.0"))) is int

    def test_a_bool_is_unchanged(self) -> None:
        assert rounding.measured_value(True) is True

    def test_a_small_decimal_is_spelled_without_an_exponent(self) -> None:
        assert rounding.number_text(Decimal("1E-7")) == "0.0000001"

    def test_a_decimal_statistic_rounds_in_decimal_arithmetic(self) -> None:
        rounded = rounding.round_statistic(Decimal("12345678901234.5678915"))

        assert rounded == Decimal("12345678901234.567892")

    def test_the_floor_applies_to_a_decimal(self) -> None:
        assert rounding.round_statistic(Decimal("4E-7")) == Decimal("4E-7")


@pytest.mark.parametrize("vendor", SQL_PARAMS)
def test_exact_decimals_survive_every_substrate(
    vendor: str,
    request: pytest.FixtureRequest,
) -> None:
    _SEEDERS[vendor](request, _TYPES[vendor])
    adapter = _adapter_factory_for(request, vendor)()
    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if t.fqn.endswith(".lot")
    )
    columns = adapter.introspect_columns(fqn)
    _, stats = adapter.compute_statistics(fqn, columns, StatisticsConfig(), frozenset())
    adapter.close()

    ids = {_ID_BASE + i for i in range(_ROWS)}
    grades = {Decimal(-20) + Decimal("0.5") * (i % 10) for i in range(_ROWS)}
    listed = {name: [v.value for v in stats[name].values or ()] for name in stats}

    assert {type(v) for v in listed["n"]} == {int}
    assert all(not isinstance(v, str) for v in listed["grade"])

    if vendor == "bigquery":
        return  # the emulator decodes NUMERIC and BIGNUMERIC as float (measured)

    masses = {1 + _MASS_STEP * i for i in range(_ROWS)}
    stock = stats["stock"]

    assert set(listed["accession_id"]) <= ids and len(set(listed["accession_id"])) == len(
        listed["accession_id"],
    )
    assert {type(v) for v in listed["accession_id"]} == {int}
    assert set(listed["mass"]) <= masses and len(set(listed["mass"])) == len(listed["mass"])
    assert set(listed["grade"]) == grades
    assert stock.range is not None
    assert (stock.range.min, stock.range.max) == (_STOCK_BASE, _STOCK_BASE + _ROWS - 1)
    assert stock.sum == sum(_STOCK_BASE + i for i in range(_ROWS))

    written = dump_yaml({name: listed[name] for name in ("accession_id", "mass", "grade")})
    assert yaml.load(written, Loader=_DigitLoader) == {
        name: listed[name] for name in ("accession_id", "mass", "grade")
    }


class _DigitLoader(yaml.SafeLoader):
    pass


_DigitLoader.add_constructor(
    "tag:yaml.org,2002:float",
    lambda loader, node: Decimal(loader.construct_scalar(node)),
)


_TYPES: dict[str, dict[str, str]] = {
    "postgres": {
        "id": "numeric(20,0)",
        "mass": "numeric(38,18)",
        "stock": "numeric(38,6)",
        "grade": "numeric(4,1)",
        "n": "integer",
    },
    "redshift": {
        "id": "numeric(20,0)",
        "mass": "numeric(38,18)",
        "stock": "numeric(38,6)",
        "grade": "numeric(4,1)",
        "n": "integer",
    },
    "mysql": {
        "id": "decimal(20,0)",
        "mass": "decimal(38,18)",
        "stock": "decimal(38,6)",
        "grade": "decimal(4,1)",
        "n": "int",
    },
    "duckdb": {
        "id": "DECIMAL(20,0)",
        "mass": "DECIMAL(38,18)",
        "stock": "DECIMAL(38,6)",
        "grade": "DECIMAL(4,1)",
        "n": "INTEGER",
    },
    "snowflake": {
        "id": "DECIMAL(20,0)",
        "mass": "DECIMAL(38,18)",
        "stock": "DECIMAL(38,6)",
        "grade": "DECIMAL(4,1)",
        "n": "INTEGER",
    },
    "clickhouse": {
        "id": "Decimal(20, 0)",
        "mass": "Decimal(38, 18)",
        "stock": "Decimal(38, 6)",
        "grade": "Decimal(4, 1)",
        "n": "Int32",
    },
    "databricks": {
        "id": "DECIMAL(20,0)",
        "mass": "DECIMAL(38,18)",
        "stock": "DECIMAL(38,6)",
        "grade": "DECIMAL(4,1)",
        "n": "INT",
    },
    "bigquery": {
        "id": "BIGNUMERIC",
        "mass": "BIGNUMERIC",
        "stock": "NUMERIC",
        "grade": "NUMERIC",
        "n": "INT64",
    },
}


def _create(types: dict[str, str], table: str, suffix: str = "") -> str:
    return (
        f"CREATE TABLE {table} (accession_id {types['id']}, mass {types['mass']}, "
        f"stock {types['stock']}, grade {types['grade']}, n {types['n']}){suffix}"
    )


def _insert(types: dict[str, str], table: str) -> str:
    def cast(text: object, kind: str) -> str:
        return f"CAST('{text}' AS {types[kind]})"

    rows = ", ".join(
        f"({cast(_ID_BASE + i, 'id')}, {cast(1 + _MASS_STEP * i, 'mass')}, "
        f"{cast(_STOCK_BASE + i, 'stock')}, {cast(Decimal(-20) + Decimal('0.5') * (i % 10), 'grade')}, "
        f"{i % 5})"
        for i in range(_ROWS)
    )

    return f"INSERT INTO {table} VALUES {rows}"


def _seed_duckdb(request: pytest.FixtureRequest, types: dict[str, str]) -> None:
    con = request.getfixturevalue("duckdb_native_connection")
    con.execute(_create(types, "seedbank.lot"))
    con.execute(_insert(types, "seedbank.lot"))


def _seed_snowflake(request: pytest.FixtureRequest, types: dict[str, str]) -> None:
    shim = request.getfixturevalue("snowflake_duckdb_connection")
    shim.execute(_create(types, "seedbank.lot"))
    shim.execute(_insert(types, "seedbank.lot"))


def _seed_postgres(request: pytest.FixtureRequest, types: dict[str, str]) -> None:

    creds = request.getfixturevalue("postgres_test_db")

    with pg_connect(creds) as conn:
        conn.execute(cast(LiteralString, _create(types, "seedbank.lot")))
        conn.execute(cast(LiteralString, _insert(types, "seedbank.lot")))
        conn.execute("ANALYZE")


def _seed_redshift(request: pytest.FixtureRequest, types: dict[str, str]) -> None:
    shim = request.getfixturevalue("redshift_postgres_connection")
    shim.execute(_create(types, "seedbank.lot"))
    shim.execute(_insert(types, "seedbank.lot"))
    shim.execute("ANALYZE")


def _seed_mysql(request: pytest.FixtureRequest, types: dict[str, str]) -> None:
    creds = request.getfixturevalue("mysql_test_db")
    statements = [_create(types, "lot"), _insert(types, "lot"), "ANALYZE TABLE lot"]
    _mysql_exec_many(int(creds["port"]), creds["database"], statements)


def _seed_clickhouse(request: pytest.FixtureRequest, types: dict[str, str]) -> None:
    cursor = request.getfixturevalue("clickhouse_native_connection")
    cursor.execute(_create(types, "seedbank.lot", " ENGINE = Memory"))
    cursor.execute(_insert(types, "seedbank.lot"))


def _seed_databricks(request: pytest.FixtureRequest, types: dict[str, str]) -> None:
    cursor = request.getfixturevalue("databricks_test_schema")
    cursor.execute(_create(types, "lot", " USING DELTA"))
    cursor.execute(_insert(types, "lot"))


def _seed_bigquery(request: pytest.FixtureRequest, types: dict[str, str]) -> None:
    cursor, dataset = request.getfixturevalue("bigquery_test_dataset")
    table = f"`dbprint-test`.`{dataset}`.lot"
    cursor.execute(_create(types, table))
    cursor.execute(_insert(types, table))


_SEEDERS: dict[str, Any] = {
    "bigquery": _seed_bigquery,
    "clickhouse": _seed_clickhouse,
    "databricks": _seed_databricks,
    "duckdb": _seed_duckdb,
    "mysql": _seed_mysql,
    "postgres": _seed_postgres,
    "redshift": _seed_redshift,
    "snowflake": _seed_snowflake,
}


def _cell_violations(tree: ast.AST, vendor: str) -> list[tuple[int, str]]:
    out = []

    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue

        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue

            callee = _callee(node)

            if callee == "float" and (vendor, function.name) not in _FLOAT_ALLOWLIST:
                out.append((node.lineno, f"float() on a cell in {function.name}"))

            if callee == "ValueCount" and not _passes_through_rule(_keyword(node, "value")):
                out.append(
                    (node.lineno, f"ValueCount value bypasses measured_value in {function.name}"),
                )

            if callee == "_approximate_distribution_via_top_n" and not _is_cell_transform(
                _transform_argument(node),
            ):
                out.append(
                    (node.lineno, f"top-N transform bypasses measured_value in {function.name}"),
                )

            if callee == "Range":
                out += [
                    (node.lineno, f"Range {bound} bypasses measured_value in {function.name}")
                    for bound in ("min", "max")
                    if not _is_ruled(_keyword(node, bound))
                ]

        out += [
            (node.lineno, f"percentile bypasses measured_value in {function.name}")
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "percentiles" for t in node.targets)
            and isinstance(node.value, ast.DictComp)
            and not _is_ruled(node.value.value)
        ]

        readers = {"measured_text", *_SHARED_TEXT_READERS} - {function.name}

        if function.name in _TEXT_BOUNDS and not readers & _callees(function):
            out.append((function.lineno, f"unmeasured text bound in {function.name}"))

        if function.name == "_fetch_value_list" and _selects_native_rendered(function):
            out.append((function.lineno, f"native value-list select in {function.name}"))

    return out


def _is_ruled(value: ast.expr | None) -> bool:
    if value is None or isinstance(value, ast.Constant) and value.value is None:
        return True

    return isinstance(value, ast.Call) and _callee(value) in _CELL_RULES


def _callees(function: ast.FunctionDef) -> set[str | None]:
    return {_callee(node) for node in ast.walk(function) if isinstance(node, ast.Call)}


def _selects_native_rendered(function: ast.FunctionDef) -> bool:
    for node in ast.walk(function):
        if not isinstance(node, ast.JoinedStr):
            continue

        parts = node.values

        for i, part in enumerate(parts[:-1]):
            following = parts[i + 1]

            if (
                isinstance(part, ast.FormattedValue)
                and isinstance(part.value, ast.Name)
                and part.value.id == "cn"
                and isinstance(following, ast.Constant)
                and str(following.value).startswith(" AS rendered")
            ):
                return True

    return False


def _passes_through_rule(value: ast.expr | None) -> bool:
    if isinstance(value, ast.Call):
        return _callee(value) in {"measured_value", "value_transform"}

    return False


def _is_cell_transform(transform: ast.expr | None) -> bool:
    if isinstance(transform, ast.Name):
        return transform.id == "measured_value"

    if isinstance(transform, ast.Lambda):
        body = transform.body
        identity = isinstance(body, ast.Name) and body.id == transform.args.args[0].arg

        return identity or (isinstance(body, ast.Call) and _callee(body) == "measured_value")

    return False


def _transform_argument(call: ast.Call) -> ast.expr | None:
    keyword = _keyword(call, "value_transform")

    if keyword is not None:
        return keyword

    candidates = [arg for arg in call.args if isinstance(arg, (ast.Name, ast.Lambda))]

    return candidates[-1] if candidates else None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id

    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def test_a_rerun_over_unchanged_decimals_reports_no_change_and_keeps_every_digit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(_create(_TYPES["duckdb"], "lot"))
    con.execute(_insert(_TYPES["duckdb"], "lot"))
    con.close()
    conn = ConnectionConfig(
        name="garden",
        adapter="duckdb",
        output=tmp_path / "prints",
        sketch_all_columns=True,
    )

    for _ in range(2):
        Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate(
            GenerateRequest(force=True),
        )

    table_dir = tmp_path / "prints" / "garden" / "garden" / "main" / "lot"
    text = (table_dir / "statistics.yaml").read_text(encoding="utf-8")
    changes = yaml.safe_load((tmp_path / "prints" / "garden" / "diff.yaml").read_text())["changes"]

    assert "sketch:" in text
    assert str(1 + _MASS_STEP * 19) in text
    assert [c for c in changes if c["kind"] == "statistic_changed"] == []
    assert "lot" not in {
        c.get("table", "").rsplit(".", 1)[-1] for c in changes if c["kind"] == "table_added"
    }
