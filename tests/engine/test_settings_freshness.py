"""A committed table stays fresh only while the configuration would still publish it as it is."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import MockAdapter
from dbprint.config import (
    ReadConfig,
    ReadSettings,
    RelationshipsProfile,
    RuleMatch,
    TimelineProfile,
)
from dbprint.config.project import ConnectionConfig, RedactRule, RuleConfig
from dbprint.engine import EXIT_PARTIAL, Engine, GenerateRequest, GenerateResult
from tests._curator import conn_config, curator_fixture
from tests._engine_run import conformance_errors


CURATOR = "public.curator"
HERBARIUM = "public.herbarium"
MASK_HERBARIUM_ID = RedactRule(columns=("public.curator.herbarium_id",), with_="mask")


def _generate(conn: ConnectionConfig, tmp_path: Path, **request: Any) -> GenerateResult:
    return Engine(MockAdapter(curator_fixture()), conn, tmp_path).generate(
        GenerateRequest(**request),
    )


def _statuses(result: GenerateResult) -> dict[str, str]:
    return {t.fqn: t.status for t in result.tables}


def _manifest(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text(encoding="utf-8"))


def _column(tmp_path: Path, column: str) -> dict[str, Any]:
    path = tmp_path / "primary" / "public" / "curator" / "statistics.yaml"

    return yaml.safe_load(path.read_text(encoding="utf-8"))["columns"][column]


class TestRedactionChanges:
    def test_a_rule_added_re_reads_only_the_table_it_covers(self, tmp_path: Path) -> None:
        conn = conn_config(tmp_path)
        _generate(conn, tmp_path)

        result = _generate(
            replace(conn, profile=replace(conn.profile, redact=(MASK_HERBARIUM_ID,))),
            tmp_path,
        )

        assert _statuses(result) == {CURATOR: "ok", HERBARIUM: "skipped"}
        assert _column(tmp_path, "herbarium_id")["redacted"] == "mask"

    def test_a_rule_removed_re_reads_the_table(self, tmp_path: Path) -> None:
        conn = replace(
            conn_config(tmp_path),
            profile=replace(conn_config(tmp_path).profile, redact=(MASK_HERBARIUM_ID,)),
        )
        _generate(conn, tmp_path)

        result = _generate(replace(conn, profile=replace(conn.profile, redact=())), tmp_path)

        assert _statuses(result)[CURATOR] == "ok"
        assert "redacted" not in _column(tmp_path, "herbarium_id")

    def test_nothing_changed_skips_everything(self, tmp_path: Path) -> None:
        conn = replace(
            conn_config(tmp_path),
            profile=replace(conn_config(tmp_path).profile, redact=(MASK_HERBARIUM_ID,)),
        )
        _generate(conn, tmp_path)

        assert set(_statuses(_generate(conn, tmp_path)).values()) == {"skipped"}

    def test_a_dropped_column_keeps_its_detection(self, tmp_path: Path) -> None:
        """The recompute reads `inferred` back from the file, so `drop` must not remove it."""

        drop = RedactRule(columns=("public.curator.id",), with_="drop")
        conn = replace(
            conn_config(tmp_path),
            profile=replace(conn_config(tmp_path).profile, redact=(drop,)),
        )
        _generate(conn, tmp_path)

        assert _column(tmp_path, "id")["inferred"]["candidate_key"] is True
        assert set(_statuses(_generate(conn, tmp_path)).values()) == {"skipped"}


class TestStatisticsAndScopeChanges:
    def test_a_statistics_parameter_changed_re_reads_the_table_it_governs(
        self,
        tmp_path: Path,
    ) -> None:
        conn = conn_config(tmp_path)
        _generate(conn, tmp_path)
        rule = RuleConfig(
            match=RuleMatch(tables=(CURATOR,)),
            profile={"statistics": {"top_n_values": 5}},
        )

        result = _generate(replace(conn, rules=(rule,)), tmp_path)

        assert _statuses(result) == {CURATOR: "ok", HERBARIUM: "skipped"}

    def test_a_filter_added_re_reads_the_table_it_governs(self, tmp_path: Path) -> None:
        conn = conn_config(tmp_path)
        _generate(conn, tmp_path)
        rule = RuleConfig(
            match=RuleMatch(tables=(HERBARIUM,)),
            profile={"read": {"tables": {"mode": "filter", "filter": "id IS NOT NULL"}}},
        )

        result = _generate(replace(conn, rules=(rule,)), tmp_path)

        assert _statuses(result) == {CURATOR: "skipped", HERBARIUM: "ok"}


class TestCeiling:
    def test_the_governing_ceiling_is_recorded_and_compared_as_a_ceiling(
        self,
        tmp_path: Path,
    ) -> None:
        conn = replace(
            conn_config(tmp_path),
            profile=replace(
                conn_config(tmp_path).profile,
                read=ReadSettings(tables=ReadConfig(mode="sample", max_rows=1000)),
            ),
        )
        _generate(conn, tmp_path)

        assert _manifest(tmp_path)["tables"][CURATOR]["max_rows_scanned"] == 1000
        assert set(_statuses(_generate(conn, tmp_path)).values()) == {"skipped"}

        result = _generate(
            replace(
                conn,
                profile=replace(
                    conn.profile,
                    read=ReadSettings(tables=ReadConfig(mode="sample", max_rows=2000)),
                ),
            ),
            tmp_path,
        )

        assert set(_statuses(result).values()) == {"ok"}
        assert _manifest(tmp_path)["tables"][CURATOR]["max_rows_scanned"] == 2000

    def test_no_ceiling_records_no_key(self, tmp_path: Path) -> None:
        _generate(conn_config(tmp_path), tmp_path)

        assert "max_rows_scanned" not in _manifest(tmp_path)["tables"][CURATOR]


class TestProfilingSwitches:
    def test_the_profile_is_recorded(self, tmp_path: Path) -> None:
        _generate(
            replace(
                conn_config(tmp_path),
                profile=replace(
                    conn_config(tmp_path).profile,
                    timeline=TimelineProfile(enabled=False),
                ),
            ),
            tmp_path,
        )

        assert _manifest(tmp_path)["profile"]["timeline"] == {"enabled": False}

    def test_a_switch_flipped_by_a_rule_re_reads_only_the_tables_it_reaches(
        self,
        tmp_path: Path,
    ) -> None:
        conn = conn_config(tmp_path)
        _generate(conn, tmp_path)
        rule = RuleConfig(
            match=RuleMatch(tables=(HERBARIUM,)),
            profile={"null_patterns": {"enabled": False}},
        )

        result = _generate(replace(conn, rules=(rule,)), tmp_path)
        entry = _manifest(tmp_path)["tables"][HERBARIUM]

        assert _statuses(result) == {CURATOR: "skipped", HERBARIUM: "ok"}
        assert next(t for t in result.tables if t.fqn == HERBARIUM).reason == (
            "profile (null_patterns.enabled, null_patterns.top_n) changed since it was profiled"
        )
        assert entry["profile"] == {"null_patterns": {"enabled": False}}

    def test_a_switch_re_reads_a_scoped_table_too(self, tmp_path: Path) -> None:
        rule = RuleConfig(
            match=RuleMatch(tables=(HERBARIUM,)),
            profile={"read": {"tables": {"mode": "filter", "filter": "id IS NOT NULL"}}},
        )
        conn = replace(conn_config(tmp_path), rules=(rule,))
        _generate(conn, tmp_path)

        result = _generate(
            replace(conn, profile=replace(conn.profile, timeline=TimelineProfile(enabled=False))),
            tmp_path,
        )

        assert _statuses(result) == {CURATOR: "ok", HERBARIUM: "ok"}

    def test_a_disabled_features_parameter_change_re_reads_nothing(self, tmp_path: Path) -> None:
        off = RuleConfig(profile={"looks_like": {"enabled": False}})
        conn = replace(conn_config(tmp_path), rules=(off,))
        _generate(conn, tmp_path)
        resized = RuleConfig(profile={"looks_like": {"enabled": False, "sample_size": 7}})

        result = _generate(replace(conn, rules=(resized,)), tmp_path)

        assert set(_statuses(result).values()) == {"skipped"}

    def test_a_print_without_a_recorded_profile_re_reads_every_table(self, tmp_path: Path) -> None:
        conn = conn_config(tmp_path)
        _generate(conn, tmp_path)
        path = tmp_path / "primary" / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        del manifest["profile"]
        path.write_text(yaml.safe_dump(manifest))

        result = _generate(conn, tmp_path)

        assert set(_statuses(result).values()) == {"ok"}
        assert result.tables[0].reason == "profile (not recorded) changed since it was profiled"

    def test_the_inference_switch_re_reads_a_view_too(self, tmp_path: Path) -> None:
        fixture = curator_fixture()
        view = replace(fixture[CURATOR], type="view", namespace_path=("public", "seed_ledger"))
        fixture["public.seed_ledger"] = view
        conn = conn_config(tmp_path)
        Engine(MockAdapter(fixture), conn, tmp_path).generate(GenerateRequest())

        flipped = replace(
            conn,
            profile=replace(
                conn.profile,
                relationships=RelationshipsProfile(infer=not conn.profile.relationships.infer),
            ),
        )
        result = Engine(MockAdapter(fixture), flipped, tmp_path).generate(GenerateRequest())

        assert _statuses(result)["public.seed_ledger"] == "ok"


class TestATableThisRunDoesNotRead:
    def test_an_out_of_scope_table_now_covered_fails_the_run(self, tmp_path: Path) -> None:
        conn = conn_config(tmp_path)
        _generate(conn, tmp_path)
        before = _manifest(tmp_path)["tables"][CURATOR]
        path = tmp_path / "primary" / "public" / "curator" / "statistics.yaml"
        committed = path.read_bytes()

        result = _generate(
            replace(conn, profile=replace(conn.profile, redact=(MASK_HERBARIUM_ID,))),
            tmp_path,
            cli_include=(HERBARIUM,),
            force=True,
        )
        failure = next(t for t in result.tables if t.fqn == CURATOR)

        assert result.exit_code == EXIT_PARTIAL
        assert failure.status == "failed"
        assert failure.error is not None and "herbarium_id" in failure.error
        assert path.read_bytes() == committed
        assert _manifest(tmp_path)["tables"][CURATOR] == before

    def test_a_carried_entry_states_the_parameters_it_was_measured_under(
        self,
        tmp_path: Path,
    ) -> None:
        conn = conn_config(tmp_path)
        _generate(conn, tmp_path)
        changed = replace(
            conn,
            profile=replace(
                conn.profile,
                statistics=replace(conn.profile.statistics, top_n_values=40),
            ),
        )

        _generate(changed, tmp_path, cli_include=(HERBARIUM,), force=True)
        entry = _manifest(tmp_path)["tables"][CURATOR]

        assert _manifest(tmp_path)["profile"]["statistics"]["top_n_values"] == 40
        assert entry["profile"]["statistics"]["top_n_values"] == 20
        assert not conformance_errors(tmp_path / "primary")
