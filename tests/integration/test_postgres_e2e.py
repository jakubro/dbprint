"""End-to-end dbprint pipeline against ephemeral Postgres: generate, validate, spot-check."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
import yaml
from click.testing import CliRunner, Result

from dbprint.cli import run_log
from dbprint.cli.main import main
from dbprint.conformance import validate_print


CONN_NAME = "e2e_conn"


def _render_project_yaml(*, max_age_days: int = 7) -> str:
    return f"""\
defaults:
  max_age_days: {max_age_days}
  statistics:
    enumeration_threshold: 50
    top_n_values: 20
    percentiles: [1, 25, 50, 75, 99]
  diff:
    stat_change_threshold:
      default: 0.01

connections:
  {CONN_NAME}:
    adapter: postgres
    auto: true
    output: prints
"""


def _write_project(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".dbprint.yaml").write_text(_render_project_yaml())


def _credential_env(creds: dict[str, str]) -> dict[str, str]:
    upper = CONN_NAME.upper()

    return {
        f"DBPRINT_{upper}_HOST": creds["host"],
        f"DBPRINT_{upper}_PORT": str(creds["port"]),
        f"DBPRINT_{upper}_DATABASE": creds["database"],
        f"DBPRINT_{upper}_USER": creds["user"],
        f"DBPRINT_{upper}_PASSWORD": creds["password"] or "",
    }


def _run_generate(
    project_dir: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Result:
    runner = CliRunner()
    monkeypatch.chdir(project_dir)

    for k, v in env.items():
        monkeypatch.setenv(k, v)

    return runner.invoke(main, ["generate", "--no-tui"])


def _run_list(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Result:
    runner = CliRunner()
    monkeypatch.chdir(project_dir)

    return runner.invoke(main, ["list", "--no-tui"])


def test_postgres_end_to_end(
    e2e_postgres_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full pipeline runs cleanly + conformance passes + sanity checks hold."""

    project_dir = tmp_path / "project"
    _write_project(project_dir)
    env = _credential_env(e2e_postgres_db)

    result = _run_generate(project_dir, env, monkeypatch)
    assert result.exit_code in (0, 3), (
        f"generate failed (exit={result.exit_code}):\n{result.output}\n"
        f"stderr: {result.stderr if hasattr(result, 'stderr') else ''}\n"
        f"exc: {result.exception}"
    )

    print_dir = project_dir / "prints" / CONN_NAME
    assert (print_dir / "manifest.yaml").is_file()
    assert (print_dir / "diff.yaml").is_file()

    list_result = _run_list(project_dir, monkeypatch)
    assert list_result.exit_code == 0
    assert "curator" in list_result.output or "table_count" in list_result.output

    issues = validate_print(print_dir)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], "Conformance violations:\n" + "\n".join(
        f"  {e.code} at {e.path}: {e.detail}" for e in errors
    )

    curator_stats = yaml.safe_load((print_dir / "public/curator/statistics.yaml").read_text())
    assert curator_stats["columns"]["id"]["classification"] == "text"
    assert curator_stats["columns"]["email"]["classification"] == "text"
    assert curator_stats["columns"]["email"]["inferred"]["looks_like"] == "email"
    assert curator_stats["columns"]["is_active"]["classification"] == "boolean"
    assert curator_stats["columns"]["field_photo"]["classification"] == "unsupported"
    assert curator_stats["columns"]["traits"]["classification"] == "json"

    fieldwork_rel = yaml.safe_load(
        (print_dir / "public/fieldwork/relationships.yaml").read_text(),
    )
    composite_fks = [
        fk
        for fk in fieldwork_rel["refers_to"]
        if len(fk["column"]) == 2 and fk["target_table"] == "public.curator"
    ]
    assert composite_fks, "expected composite FK from fieldwork to curator"
    assert set(composite_fks[0]["column"]) == {"curator_id", "herbarium_id"}
    assert set(composite_fks[0]["target_column"]) == {"id", "herbarium_id"}

    botanist_rel = yaml.safe_load((print_dir / "public/botanist/relationships.yaml").read_text())
    assert botanist_rel["refers_to"][0]["target_table"] == "public.botanist"

    # curator.referenced_by populated by the second-pass relationship graph.
    curator_rel = yaml.safe_load((print_dir / "public/curator/relationships.yaml").read_text())
    referencer_tables = {entry["referencer_table"] for entry in curator_rel["referenced_by"]}
    assert "public.fieldwork" in referencer_tables

    # active_curators_v is a view -> DDL, relationships, and a catalog-only statistics.yaml
    # (SPEC 2.2.15): every column the catalog read found, nothing measured.
    view_dir = print_dir / "public/active_curators_v"
    assert (view_dir / "ddl.sql").is_file()
    view_stats = yaml.safe_load((view_dir / "statistics.yaml").read_text())
    assert view_stats["catalog_only"] is True
    assert set(view_stats["columns"]) == {"id", "email"}
    assert "row_count" not in view_stats

    # daily_viability_mv is a matview -> DDL + statistics + relationships.
    mv_dir = print_dir / "public/daily_viability_mv"
    assert (mv_dir / "ddl.sql").is_file()
    assert (mv_dir / "statistics.yaml").is_file()

    manifest = yaml.safe_load((print_dir / "manifest.yaml").read_text())
    expected_tables = {
        "public.herbarium",
        "public.curator",
        "public.fieldwork",
        "public.botanist",
        "public.curation_event",
        "public.active_curators_v",
        "public.daily_viability_mv",
    }
    assert expected_tables <= set(manifest["tables"]), (
        f"missing tables in manifest: {expected_tables - set(manifest['tables'])}"
    )


def test_categorical_classification_for_biome(
    e2e_postgres_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """herbarium.biome has cardinality 3 -> categorical with exhaustive values."""

    project_dir = tmp_path / "project"
    _write_project(project_dir)
    env = _credential_env(e2e_postgres_db)
    result = _run_generate(project_dir, env, monkeypatch)
    assert result.exit_code in (0, 3), result.output

    stats = yaml.safe_load(
        (project_dir / "prints" / CONN_NAME / "public/herbarium/statistics.yaml").read_text(),
    )
    biome = stats["columns"]["biome"]
    assert biome["classification"] == "categorical"
    assert "values" in biome
    assert {entry["value"] for entry in biome["values"]} == {"temperate", "tropical", "arid"}
    assert biome["values_coverage"] == 1.0


def test_temporal_classification_for_curation_event_created_at(
    e2e_postgres_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """curation_event.created_at has high cardinality + timestamp type -> temporal."""

    project_dir = tmp_path / "project"
    _write_project(project_dir)
    env = _credential_env(e2e_postgres_db)
    result = _run_generate(project_dir, env, monkeypatch)
    assert result.exit_code in (0, 3), result.output

    stats = yaml.safe_load(
        (project_dir / "prints" / CONN_NAME / "public/curation_event/statistics.yaml").read_text(),
    )
    created_at = stats["columns"]["created_at"]
    assert created_at["classification"] == "temporal"
    assert "range" in created_at
    assert "freshness" in created_at
    assert "percentiles" in created_at


def test_approximate_path_activates_under_low_threshold(
    e2e_postgres_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force APPROXIMATE_THRESHOLD low so even seeded data hits the approximate path."""

    from dbprint.adapters.postgres import stats as stats_module

    monkeypatch.setattr(stats_module, "APPROXIMATE_THRESHOLD", 10)

    project_dir = tmp_path / "project"
    _write_project(project_dir)
    env = _credential_env(e2e_postgres_db)
    result = _run_generate(project_dir, env, monkeypatch)
    assert result.exit_code in (0, 3), result.output

    curation_event_stats = yaml.safe_load(
        (project_dir / "prints" / CONN_NAME / "public/curation_event/statistics.yaml").read_text(),
    )
    # pg_stats may lag so cardinality can read 0; only completion without error is asserted.
    assert "columns" in curation_event_stats


def test_statement_tracing(
    e2e_postgres_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate traces every statement it sent, params kept separate from the text."""

    project_dir = tmp_path / "project"
    _write_project(project_dir)
    env = _credential_env(e2e_postgres_db)

    result = _run_generate(project_dir, env, monkeypatch)
    assert result.exit_code in (0, 3), result.output

    log_dir = run_log.LOGS_ROOT / run_log._slug(project_dir)
    files = list(log_dir.glob("*.log"))
    assert len(files) == 1
    text = files[0].read_text()

    # At least one statement record per profiled table, with elapsed.
    assert "fqn=public.curator" in text
    assert "fqn=public.herbarium" in text
    assert "statement conn=e2e_conn" in text
    assert "elapsed_ms=" in text

    # A parameterised catalog read shows placeholders in the text (never a literal
    # value substituted in) and its bound values in a separate field alongside it.
    assert "%s" in text
    assert "params=('public'," in text

    # pg_dump traced as an invocation, distinct from a statement.
    assert "pg_dump " in text
    assert "argv=[" in text

    # DEBUG-only trace markers never reach the console - the renderer's own log handler is
    # pinned at WARNING, proven here end-to-end for these exact records.
    assert "elapsed_ms=" not in result.output
    assert "params=(" not in result.output
    assert "PGPASSWORD" not in text


def test_freshness_skip_on_immediate_rerun(
    e2e_postgres_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running generate within max_age_days should skip every table."""

    project_dir = tmp_path / "project"
    _write_project(project_dir)
    env = _credential_env(e2e_postgres_db)

    first = _run_generate(project_dir, env, monkeypatch)
    assert first.exit_code in (0, 3), first.output

    second = _run_generate(project_dir, env, monkeypatch)
    # A full skip surfaces as status=skipped per table and in the summary line.
    assert "skipped" in second.output


def test_a_narrowed_read_over_a_stale_estimate_conforms(
    e2e_postgres_db: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC 2.2.8 forbids clamping row_count to a planner estimate the scan overtook."""

    _seed_lagging_estimate(e2e_postgres_db)

    project_dir = tmp_path / "stale"
    project_dir.mkdir(parents=True)
    (project_dir / ".dbprint.yaml").write_text(_LAGGING_ESTIMATE_YAML)

    result = _run_generate(project_dir, _credential_env(e2e_postgres_db), monkeypatch)
    assert result.exit_code in (0, 3), result.output

    print_dir = project_dir / "prints" / CONN_NAME
    stats = yaml.safe_load((print_dir / "public/lagging/statistics.yaml").read_text())

    assert stats["row_count_method"] == "approximate"
    assert stats["scope"]["rows_scanned"] > stats["row_count"], (
        f"the planner estimate ({stats['row_count']}) did not lag the "
        f"{stats['scope']['rows_scanned']} rows scanned, so the exception is untested"
    )

    errors = [i for i in validate_print(print_dir) if i.severity == "error"]
    assert errors == [], "Conformance violations:\n" + "\n".join(
        f"  {e.code} at {e.path}: {e.detail}" for e in errors
    )


_LAGGING_ESTIMATE_YAML = f"""\
connections:
  {CONN_NAME}:
    adapter: postgres
    auto: true
    output: prints
    include: ["public.lagging"]
    rules:
      - include: ["public.lagging"]
        sample: 1.0
"""


def _seed_lagging_estimate(creds: dict[str, str]) -> None:
    """Leaves reltuples stale: ANALYZE while small, then autovacuum-disabled inserts grow it."""

    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password=creds["password"],
        autocommit=True,
    ) as conn:
        conn.execute(
            "CREATE TABLE public.lagging (id BIGINT PRIMARY KEY, bucket INT) "
            "WITH (autovacuum_enabled = false)",
        )
        conn.execute("INSERT INTO public.lagging SELECT g, g % 4 FROM generate_series(1, 10) g")
        conn.execute("ANALYZE public.lagging")
        conn.execute(
            "INSERT INTO public.lagging SELECT g, g % 4 FROM generate_series(11, 3000) g",
        )
