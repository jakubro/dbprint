"""Live e2e against a real BigQuery project: extract -> classify -> write, plus every behavior
the emulator substrate cannot prove.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from dbprint.adapters import BigqueryAdapter
from dbprint.cli.main import main
from dbprint.conformance import validate_print
from dbprint.spec.sketch import low64_md5


# Every variable `_live_creds` reads without a default, so exporting what the skip reason names
# produces a run rather than a KeyError. Credentials resolve through ADC, not a dbprint var.
_REQUIRED_ENV = ("BIGQUERY_PROJECT",)

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in _REQUIRED_ENV),
    reason=(
        f"set {', '.join(_REQUIRED_ENV)} to run the live BigQuery e2e (BIGQUERY_DATASET "
        "defaults to dbprint_live). Runs real queries against a real project - each carries "
        "the documented 10 MB-per-table billing minimum."
    ),
)

CONN_NAME = "bigquery_live"
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "bigquery"


def _live_creds() -> dict[str, str]:
    return {
        "project": os.environ["BIGQUERY_PROJECT"],
        "dataset": os.environ.get("BIGQUERY_DATASET", "dbprint_live"),
    }


def _apply_fixtures(creds: dict[str, str]) -> None:
    from google.cloud import bigquery

    client = bigquery.Client(project=creds["project"])
    client.query(f"CREATE SCHEMA IF NOT EXISTS `{creds['project']}`.`{creds['dataset']}`").result()

    job_config = bigquery.QueryJobConfig(default_dataset=f"{creds['project']}.{creds['dataset']}")

    for path in ("schema.bigquery.sql", "data.bigquery.sql"):
        for statement in _split_sql((_FIXTURE_DIR / path).read_text()):
            client.query(statement, job_config=job_config).result()


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
    adapter: bigquery
    auto: true
    output: prints
""",
    )


def _credential_env(creds: dict[str, str]) -> dict[str, str]:
    upper = CONN_NAME.upper()

    return {
        f"DBPRINT_{upper}_PROJECT": creds["project"],
        f"DBPRINT_{upper}_DATASET": creds["dataset"],
    }


def test_bigquery_live_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full pipeline against a real project: generate, conformance-clean, CLUSTER BY, FK."""

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
    dataset = creds["dataset"]
    assert f"{dataset}.loan_request" in manifest["tables"], (
        "the never-populated table is missing from the manifest - list_tables must not drop "
        "a real table just because it carries no write-collected statistics"
    )

    sheet = yaml.safe_load(
        (print_dir / dataset / "herbarium_sheet" / "statistics.yaml").read_text(),
    )
    layout = sheet["physical_layout"]
    assert layout["mechanism"] == "cluster"
    assert [k["column"] for k in layout["keys"]] == ["herbarium_id", "logged_at"]

    columns = sheet["columns"]
    assert columns["herbarium_id"]["physical_layout_key"] is True

    relationships = yaml.safe_load(
        (print_dir / dataset / "herbarium_sheet" / "relationships.yaml").read_text(),
    )
    herbarium_edge = next(
        (r for r in relationships["refers_to"] if r["target_table"] == f"{dataset}.herbarium"),
        None,
    )
    assert herbarium_edge is not None, "the declared FK to herbarium did not surface"
    assert herbarium_edge["detection"] == "declared"

    ddl = (print_dir / dataset / "herbarium_sheet" / "ddl.sql").read_text()
    assert "CLUSTER BY" in ddl.upper()


def test_key_sketch_matches_the_spec_test_vectors_on_a_real_project(
    tmp_path: Path,
) -> None:
    """The emulator's NUMERIC multiply loses precision above 2**53 (measured); this is the one
    place real BigQuery's own NUMERIC/bitwise arithmetic can be asked whether it agrees.
    """

    from google.cloud import bigquery

    creds = _live_creds()
    client = bigquery.Client(project=creds["project"])
    client.query(f"CREATE SCHEMA IF NOT EXISTS `{creds['project']}`.`{creds['dataset']}`").result()

    table = f"{creds['project']}.{creds['dataset']}.dbprint_sketch_vectors"
    values = ["42", "-7", "4.00", "hello", "true", "2026-05-17T22:48:01Z"]
    client.query(f"CREATE OR REPLACE TABLE `{table}` (v STRING)").result()
    rows = ", ".join(f"('{v}')" for v in values)
    client.query(f"INSERT INTO `{table}` (v) VALUES {rows}").result()

    adapter = BigqueryAdapter(creds)
    adapter.connect()

    try:
        fqn = f"{creds['dataset']}.dbprint_sketch_vectors"
        sketch = set(adapter.compute_key_sketch(fqn, "v", "string", "text", k=1000))
    finally:
        adapter.close()
        client.query(f"DROP TABLE `{table}`").result()

    for v in values:
        expected = low64_md5(v)
        assert expected in sketch, f"{v!r}: expected hash {expected} not in {sorted(sketch)}"


def test_a_composite_foreign_key_publishes_its_real_arity(tmp_path: Path) -> None:
    """`KEY_COLUMN_USAGE` alone carries every fact a composite key needs - joining
    `CONSTRAINT_COLUMN_USAGE` on `constraint_name` alone cross-products a composite key's rows.
    """

    from google.cloud import bigquery

    creds = _live_creds()
    client = bigquery.Client(project=creds["project"])
    client.query(f"CREATE SCHEMA IF NOT EXISTS `{creds['project']}`.`{creds['dataset']}`").result()

    ref_table = f"{creds['project']}.{creds['dataset']}.dbprint_composite_fk_ref"
    fk_table = f"{creds['project']}.{creds['dataset']}.dbprint_composite_fk_src"
    client.query(f"DROP TABLE IF EXISTS `{fk_table}`").result()
    client.query(f"DROP TABLE IF EXISTS `{ref_table}`").result()
    client.query(
        f"CREATE TABLE `{ref_table}` (x INT64 NOT NULL, y STRING NOT NULL, "
        "PRIMARY KEY (x, y) NOT ENFORCED)",
    ).result()
    client.query(
        f"CREATE TABLE `{fk_table}` (a INT64, b STRING, "
        f"FOREIGN KEY (a, b) REFERENCES `{ref_table}`(x, y) NOT ENFORCED)",
    ).result()

    adapter = BigqueryAdapter(creds)
    adapter.connect()

    try:
        fqn = f"{creds['dataset']}.dbprint_composite_fk_src"
        adapter.list_tables(include=["*"], exclude=[])
        edges = adapter.introspect_relationships(fqn)
    finally:
        adapter.close()
        client.query(f"DROP TABLE `{fk_table}`").result()
        client.query(f"DROP TABLE `{ref_table}`").result()

    assert len(edges) == 1, f"expected exactly one composite FK, got {len(edges)}: {edges}"
    edge = edges[0]
    assert edge.column == ("a", "b"), f"source columns duplicated or reordered: {edge.column}"
    assert edge.target_column == ("x", "y"), f"target columns wrong: {edge.target_column}"


def test_default_collation_and_a_columns_own_override(tmp_path: Path) -> None:
    """`default_collation()` is BigQuery's own documented constant (`binary`) - no dataset-level
    default exists to query. A column's own `COLLATE` clause is read by `columns()` instead.
    """

    from google.cloud import bigquery

    creds = _live_creds()
    client = bigquery.Client(project=creds["project"])
    client.query(f"CREATE SCHEMA IF NOT EXISTS `{creds['project']}`.`{creds['dataset']}`").result()

    table = f"{creds['project']}.{creds['dataset']}.dbprint_collation"
    client.query(f"DROP TABLE IF EXISTS `{table}`").result()
    client.query(
        f"CREATE TABLE `{table}` (plain STRING, ci STRING COLLATE 'und:ci')",
    ).result()

    adapter = BigqueryAdapter(creds)
    adapter.connect()

    try:
        fqn = f"{creds['dataset']}.dbprint_collation"
        default = adapter.default_collation()
        adapter.list_tables(include=["*"], exclude=[])
        columns = {c.name: c for c in adapter.introspect_columns(fqn)}
    finally:
        adapter.close()
        client.query(f"DROP TABLE `{table}`").result()

    assert default == "binary"
    assert columns["plain"].collation is None
    assert columns["ci"].collation == "und:ci"


def test_pseudo_columns_are_excluded_and_ordinals_are_not_shifted(tmp_path: Path) -> None:
    """A partitioned table's `_PARTITIONTIME`/`_PARTITIONDATE` pseudo columns carry a NULL
    `ordinal_position`, so synthesised ordinals would shift every real column's own.
    """

    from google.cloud import bigquery

    creds = _live_creds()
    client = bigquery.Client(project=creds["project"])
    client.query(f"CREATE SCHEMA IF NOT EXISTS `{creds['project']}`.`{creds['dataset']}`").result()

    table = f"{creds['project']}.{creds['dataset']}.dbprint_pseudo_columns"
    client.query(f"DROP TABLE IF EXISTS `{table}`").result()
    client.query(
        f"CREATE TABLE `{table}` (first STRING, second STRING) PARTITION BY _PARTITIONDATE",
    ).result()

    adapter = BigqueryAdapter(creds)
    adapter.connect()

    try:
        fqn = f"{creds['dataset']}.dbprint_pseudo_columns"
        adapter.list_tables(include=["*"], exclude=[])
        columns = adapter.introspect_columns(fqn)
    finally:
        adapter.close()
        client.query(f"DROP TABLE `{table}`").result()

    names = [c.name for c in columns]
    assert names == ["first", "second"], f"a pseudo column leaked through: {names}"
    assert [c.ordinal for c in columns] == [1, 2], "ordinals shifted by an excluded pseudo column"


def test_column_descriptions_are_read(tmp_path: Path) -> None:
    """`COLUMN_FIELD_PATHS.description` is queryable at dataset scope, so a documented table
    publishes its column descriptions rather than an empty map.
    """

    from google.cloud import bigquery

    creds = _live_creds()
    client = bigquery.Client(project=creds["project"])
    client.query(f"CREATE SCHEMA IF NOT EXISTS `{creds['project']}`.`{creds['dataset']}`").result()

    table = f"{creds['project']}.{creds['dataset']}.dbprint_column_comments"
    client.query(f"DROP TABLE IF EXISTS `{table}`").result()
    client.query(f"CREATE TABLE `{table}` (documented STRING, plain STRING)").result()
    client.query(
        f"ALTER TABLE `{table}` ALTER COLUMN documented SET OPTIONS "
        "(description = 'a documented column')",
    ).result()

    adapter = BigqueryAdapter(creds)
    adapter.connect()

    try:
        fqn = f"{creds['dataset']}.dbprint_column_comments"
        adapter.list_tables(include=["*"], exclude=[])
        comments = adapter.extract_comments(fqn)
    finally:
        adapter.close()
        client.query(f"DROP TABLE `{table}`").result()

    assert comments.columns.get("documented") == "a documented column"
    assert "plain" not in comments.columns


def test_looks_like_sampling_is_reproducible_across_two_runs(tmp_path: Path) -> None:
    """BigQuery's `RAND()` takes no seed argument, and the emulator's `ORDER BY RAND()` measures
    as a no-op, so reproducibility is provable only against a real project.
    """

    from google.cloud import bigquery

    creds = _live_creds()
    client = bigquery.Client(project=creds["project"])
    client.query(f"CREATE SCHEMA IF NOT EXISTS `{creds['project']}`.`{creds['dataset']}`").result()

    table = f"{creds['project']}.{creds['dataset']}.dbprint_sampling_reproducible"
    client.query(f"DROP TABLE IF EXISTS `{table}`").result()
    values = [f"value-{i}" for i in range(300)]
    client.query(f"CREATE TABLE `{table}` (v STRING)").result()
    rows = ", ".join(f"('{v}')" for v in values)
    client.query(f"INSERT INTO `{table}` (v) VALUES {rows}").result()

    adapter = BigqueryAdapter(creds)
    adapter.connect()

    try:
        fqn = f"{creds['dataset']}.dbprint_sampling_reproducible"
        adapter.list_tables(include=["*"], exclude=[])
        adapter.introspect_columns(fqn)
        first = adapter.sample_values(fqn, "v", 5)
        second = adapter.sample_values(fqn, "v", 5)
    finally:
        adapter.close()
        client.query(f"DROP TABLE `{table}`").result()

    assert first
    assert first == second


def _split_sql(text: str) -> list[str]:
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("--")]

    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]
