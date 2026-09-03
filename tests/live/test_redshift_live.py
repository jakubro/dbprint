"""Live e2e against a real Redshift workgroup: extract -> classify -> write, plus every
behavior the Postgres-backed contract substrate fabricates rather than proves.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from dbprint.cli.main import main
from dbprint.conformance import validate_print


# Every variable `_live_creds` reads without a default, so exporting exactly what the skip
# reason names produces a run rather than a KeyError inside the first test.
_REQUIRED_ENV = ("REDSHIFT_HOST", "REDSHIFT_DATABASE", "REDSHIFT_USER", "REDSHIFT_PASSWORD")

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in _REQUIRED_ENV),
    reason=(
        f"set {', '.join(_REQUIRED_ENV)} to run the live Redshift e2e "
        "(REDSHIFT_PORT defaults to 5439). Set base capacity to 4 RPU on a Serverless "
        "workgroup before running this - the default (128 RPU) bills at 32x the floor."
    ),
)

CONN_NAME = "redshift_live"
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "redshift"


def _live_creds() -> dict[str, str]:
    return {
        "host": os.environ["REDSHIFT_HOST"],
        "port": os.environ.get("REDSHIFT_PORT", "5439"),
        "database": os.environ["REDSHIFT_DATABASE"],
        "user": os.environ["REDSHIFT_USER"],
        "password": os.environ["REDSHIFT_PASSWORD"],
    }


def _apply_fixtures(creds: dict[str, str]) -> None:
    import redshift_connector

    conn = redshift_connector.connect(
        host=creds["host"],
        port=int(creds["port"]),
        database=creds["database"],
        user=creds["user"],
        password=creds["password"],
    )
    conn.autocommit = True

    try:
        cursor = conn.cursor()

        for path in ("schema.redshift.sql", "data.redshift.sql"):
            for statement in _split_sql((_FIXTURE_DIR / path).read_text()):
                cursor.execute(statement)

        cursor.close()
    finally:
        conn.close()


def _write_project(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".dbprint.yaml").write_text(
        f"""\
defaults:
  max_age_days: 7
  statistics:
    enumeration_threshold: 50
    top_n_values: 20
    percentiles: [1, 25, 50, 75, 99]

connections:
  {CONN_NAME}:
    adapter: redshift
    auto: true
    output: prints
""",
    )


def _credential_env(creds: dict[str, str]) -> dict[str, str]:
    upper = CONN_NAME.upper()

    return {
        f"DBPRINT_{upper}_HOST": creds["host"],
        f"DBPRINT_{upper}_PORT": str(creds["port"]),
        f"DBPRINT_{upper}_DATABASE": creds["database"],
        f"DBPRINT_{upper}_USER": creds["user"],
        f"DBPRINT_{upper}_PASSWORD": creds["password"],
    }


def test_redshift_live_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full pipeline against real Redshift: generate, conformance-clean, SORTKEY, empty table."""

    creds = _live_creds()
    _apply_fixtures(creds)

    project_dir = tmp_path / "project"
    _write_project(project_dir)
    monkeypatch.chdir(project_dir)

    for key, value in _credential_env(creds).items():
        monkeypatch.setenv(key, value)

    result = CliRunner().invoke(main, ["generate", "--no-tui"])
    assert result.exit_code in (0, 3), (
        f"generate failed (exit={result.exit_code}):\n{result.output}"
    )

    print_dir = project_dir / "prints" / CONN_NAME
    assert (print_dir / "manifest.yaml").is_file()

    issues = validate_print(print_dir)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], "Conformance violations:\n" + "\n".join(
        f"  {e.code} at {e.path}: {e.detail}" for e in errors
    )

    manifest = yaml.safe_load((print_dir / "manifest.yaml").read_text())
    assert "public.loan_request" in manifest["tables"], (
        "the never-populated table is missing from the manifest - SVV_TABLE_INFO's silence "
        "on an empty table was read as absent, not as row_count 0"
    )
    assert manifest["tables"]["public.loan_request"]["row_count"] == 0

    sheet = yaml.safe_load(
        (print_dir / "public" / "herbarium_sheet" / "statistics.yaml").read_text(),
    )
    layout = sheet["physical_layout"]
    assert layout["mechanism"] == "sort"
    assert [k["column"] for k in layout["keys"]] == ["herbarium_id", "logged_at"]

    columns = sheet["columns"]
    assert columns["herbarium_id"]["physical_layout_key"] is True
    # DISTKEY stays unexpressed even though this table declares no explicit DISTKEY -
    # the absence of any DISTKEY-shaped field in the emitted file is the assertion.
    assert "distkey" not in str(layout).lower()

    relationships = yaml.safe_load(
        (print_dir / "public" / "herbarium_sheet" / "relationships.yaml").read_text(),
    )
    herbarium_edge = next(
        (r for r in relationships["refers_to"] if r["target_table"] == "public.herbarium"),
        None,
    )
    assert herbarium_edge is not None, "the informational FK to herbarium did not surface"
    assert herbarium_edge["detection"] == "declared"

    ddl = (print_dir / "public" / "herbarium_sheet" / "ddl.sql").read_text()
    assert "SORTKEY" in ddl.upper()


def test_redshift_live_composite_grain_search_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`germination_reading` has no declared key and no near-unique single column, so `probe_grain`
    runs its composite-distinct search - the row-constructor spelling Redshift rejects.
    """

    creds = _live_creds()
    _apply_fixtures(creds)

    project_dir = tmp_path / "project"
    _write_project(project_dir)
    monkeypatch.chdir(project_dir)

    for key, value in _credential_env(creds).items():
        monkeypatch.setenv(key, value)

    result = CliRunner().invoke(main, ["generate", "--no-tui"])
    assert result.exit_code in (0, 3), (
        f"generate failed (exit={result.exit_code}):\n{result.output}"
    )

    print_dir = project_dir / "prints" / CONN_NAME
    stats_path = print_dir / "public" / "germination_reading" / "statistics.yaml"
    assert stats_path.is_file(), (
        "germination_reading is missing statistics.yaml - the table was lost, not just the probe"
    )

    stats = yaml.safe_load(stats_path.read_text())
    measured = {
        frozenset(key["columns"])
        for key in stats["grain"]["keys"]
        if key["detection"] == "measured"
    }
    assert frozenset({"trial_id", "reading_no"}) in measured, (
        f"the one fully unique pair the seed guarantees was not found: {measured}"
    )


def test_redshift_live_late_binding_view_omits_depends_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`herbarium_late_view` is created `WITH NO SCHEMA BINDING` - the one case the Postgres-backed
    shim cannot host, Postgres having no late-binding view concept at all.
    """

    creds = _live_creds()
    _apply_fixtures(creds)

    project_dir = tmp_path / "project"
    _write_project(project_dir)
    monkeypatch.chdir(project_dir)

    for key, value in _credential_env(creds).items():
        monkeypatch.setenv(key, value)

    result = CliRunner().invoke(main, ["generate", "--no-tui"])
    assert result.exit_code in (0, 3), (
        f"generate failed (exit={result.exit_code}):\n{result.output}"
    )

    stats_path = (
        project_dir / "prints" / CONN_NAME / "public" / "herbarium_late_view" / "statistics.yaml"
    )
    assert stats_path.is_file(), "herbarium_late_view is missing statistics.yaml"

    stats = yaml.safe_load(stats_path.read_text())
    assert "depends_on" not in stats, (
        f"a late-binding view published depends_on: {stats.get('depends_on')!r}"
    )


def test_redshift_live_composite_foreign_key_and_unique_constraint() -> None:
    """`relationships`/`unique_keys` read `pg_constraint` directly, issuing neither `SHOW
    CONSTRAINTS` form. Self-contained: builds and drops its own tables.
    """

    import redshift_connector

    from dbprint.adapters import RedshiftAdapter

    creds = _live_creds()
    conn = redshift_connector.connect(
        host=creds["host"],
        port=int(creds["port"]),
        database=creds["database"],
        user=creds["user"],
        password=creds["password"],
    )
    conn.autocommit = True
    cursor = conn.cursor()

    try:
        cursor.execute("DROP TABLE IF EXISTS dbprint_composite_fk_src")
        cursor.execute("DROP TABLE IF EXISTS dbprint_composite_fk_ref")
        cursor.execute(
            "CREATE TABLE dbprint_composite_fk_ref (x INTEGER NOT NULL, y VARCHAR(8) NOT NULL, "
            "code VARCHAR(8) UNIQUE, PRIMARY KEY (x, y))",
        )
        cursor.execute(
            "CREATE TABLE dbprint_composite_fk_src (a INTEGER, b VARCHAR(8), "
            "FOREIGN KEY (a, b) REFERENCES dbprint_composite_fk_ref (x, y))",
        )

        adapter = RedshiftAdapter(creds)
        adapter.connect()

        try:
            edges = adapter.introspect_relationships("public.dbprint_composite_fk_src")
            keys = adapter.introspect_unique_keys("public.dbprint_composite_fk_ref")
        finally:
            adapter.close()
    finally:
        cursor.execute("DROP TABLE IF EXISTS dbprint_composite_fk_src")
        cursor.execute("DROP TABLE IF EXISTS dbprint_composite_fk_ref")
        cursor.close()
        conn.close()

    assert len(edges) == 1, f"expected exactly one composite FK, got {len(edges)}: {edges}"
    edge = edges[0]
    assert edge.column == ("a", "b"), f"source columns duplicated or reordered: {edge.column}"
    assert edge.target_column == ("x", "y"), f"target columns wrong: {edge.target_column}"

    by_columns = {k.columns: k.primary for k in keys}
    assert by_columns.get(("x", "y")) is True, keys
    assert by_columns.get(("code",)) is False, keys


def test_redshift_live_sampled_table_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The derived table in `CREATE TEMPORARY TABLE ... AS SELECT * FROM (...)` carries no alias.
    Postgres accepts that unaliased, so only Redshift's older parser can confirm it either way.
    """

    creds = _live_creds()
    _apply_fixtures(creds)

    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".dbprint.yaml").write_text(
        f"""\
defaults:
  max_age_days: 7
  statistics:
    enumeration_threshold: 50
    top_n_values: 20
    percentiles: [1, 25, 50, 75, 99]

connections:
  {CONN_NAME}:
    adapter: redshift
    auto: true
    output: prints
    rules:
      - include: ["public.herbarium_sheet"]
        sample: 0.99
""",
    )
    monkeypatch.chdir(project_dir)

    for key, value in _credential_env(creds).items():
        monkeypatch.setenv(key, value)

    result = CliRunner().invoke(main, ["generate", "--no-tui"])
    assert result.exit_code in (0, 3), (
        f"generate failed (exit={result.exit_code}):\n{result.output}"
    )

    stats_path = (
        project_dir / "prints" / CONN_NAME / "public" / "herbarium_sheet" / "statistics.yaml"
    )
    assert stats_path.is_file(), "a sampled table's statistics.yaml is missing - materialize failed"


def test_redshift_live_materialized_view_gets_real_statistics() -> None:
    """`SVV_REDSHIFT_TABLES.table_type` documents only 'views' and 'tables', so `STV_MV_INFO`
    supplies the matview distinction - without it the engine profiles the object as catalog-only.
    """

    import redshift_connector

    from dbprint.adapters import RedshiftAdapter

    creds = _live_creds()
    conn = redshift_connector.connect(
        host=creds["host"],
        port=int(creds["port"]),
        database=creds["database"],
        user=creds["user"],
        password=creds["password"],
    )
    conn.autocommit = True
    cursor = conn.cursor()

    try:
        cursor.execute("DROP MATERIALIZED VIEW IF EXISTS dbprint_mv_test")
        cursor.execute("DROP TABLE IF EXISTS dbprint_mv_base")
        cursor.execute("CREATE TABLE dbprint_mv_base (id INTEGER)")
        cursor.execute("INSERT INTO dbprint_mv_base (id) VALUES (1), (2), (3)")
        cursor.execute(
            "CREATE MATERIALIZED VIEW dbprint_mv_test AS SELECT id FROM dbprint_mv_base",
        )

        adapter = RedshiftAdapter(creds)
        adapter.connect()

        try:
            tables = {t.fqn: t.type for t in adapter.list_tables(include=["*"], exclude=[])}
        finally:
            adapter.close()
    finally:
        cursor.execute("DROP MATERIALIZED VIEW IF EXISTS dbprint_mv_test")
        cursor.execute("DROP TABLE IF EXISTS dbprint_mv_base")
        cursor.close()
        conn.close()

    assert tables.get("public.dbprint_mv_test") == "matview"
    assert tables.get("public.dbprint_mv_base") == "table"


def _split_sql(text: str) -> list[str]:
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("--")]

    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]
