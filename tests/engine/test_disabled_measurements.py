"""A measurement switched off for one table is absent and named in `disabled` (SPEC 2.2.21)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import MockAdapter, MockTable, UniqueKeyMeta
from dbprint.config import RedactRule, RuleConfig, RuleMatch
from dbprint.conformance import validate_print
from dbprint.engine import Engine, GenerateRequest
from dbprint.engine.context_assembler import AssemblyOptions, assemble
from dbprint.mcp import ServedConnections, dispatch
from dbprint.spec.absence import Absence
from dbprint.spec.predicate import PatternPredicate, inapplicable_reason, resolve
from tests._curator import conn_config, curator_fixture, referencing_fixture
from tests._engine_run import conformance_errors


CURATOR = "public.curator"
HERBARIUM = "public.herbarium"


class _Recorder(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.sampled: list[str] = []

    def sample_values(self, fqn: str, *args: Any, **kwargs: Any) -> Any:
        self.sampled.append(fqn)

        return super().sample_values(fqn, *args, **kwargs)


def _off(tmp_path: Path, profile: dict[str, Any], *extra: RuleConfig) -> Any:
    rule = RuleConfig(match=RuleMatch(tables=(CURATOR,)), profile=profile)

    return replace(conn_config(tmp_path), rules=(rule, *extra))


def _statistics(tmp_path: Path, name: str) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / f"primary/public/{name}/statistics.yaml").read_text())


def _manifest(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary/manifest.yaml").read_text())


def _rewrite(path: Path, change: Any) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _codes(tmp_path: Path) -> set[str]:
    return {issue.code for issue in validate_print(tmp_path / "primary")}


def test_a_switched_off_null_census_is_absent_named_and_never_unmeasured(tmp_path: Path) -> None:
    Engine(
        MockAdapter(curator_fixture()),
        _off(tmp_path, {"null_patterns": {"enabled": False}}),
        tmp_path,
    ).generate()
    curator, herbarium = _statistics(tmp_path, "curator"), _statistics(tmp_path, "herbarium")

    assert curator["disabled"] == ["null_patterns"]
    assert "null_patterns" not in curator
    assert "unmeasured" not in curator
    assert "disabled" not in herbarium
    assert _manifest(tmp_path)["tables"][CURATOR]["profile"] == {
        "null_patterns": {"enabled": False},
    }
    assert conformance_errors(tmp_path / "primary") == []


def test_every_reader_says_not_measured_never_none(tmp_path: Path) -> None:
    conn = _off(tmp_path, {"null_patterns": {"enabled": False}})
    Engine(MockAdapter(curator_fixture()), conn, tmp_path).generate()
    manifest = _manifest(tmp_path)
    text = assemble(manifest, tmp_path / "primary", [CURATOR], AssemblyOptions()).text
    query = assemble(
        manifest,
        tmp_path / "primary",
        [CURATOR],
        AssemblyOptions(purpose="query"),
    ).text

    assert "Not measured: switched off - columns null on the same rows" in text
    assert "Not measured: switched off - columns null on the same rows" in query
    assert "not measured: switched off for this table, so which columns are null" in text
    assert "switched off" in text.split("## Terms", 1)[1]


def test_looks_like_switched_off_draws_no_sample_and_publishes_no_verdict(tmp_path: Path) -> None:
    adapter = _Recorder(curator_fixture())
    Engine(adapter, _off(tmp_path, {"looks_like": {"enabled": False}}), tmp_path).generate()
    curator = _statistics(tmp_path, "curator")

    assert CURATOR not in adapter.sampled
    assert HERBARIUM in adapter.sampled
    assert curator["disabled"] == ["looks_like"]
    assert not [c for c in curator["columns"].values() if "looks_like" in (c.get("inferred") or {})]
    assert conformance_errors(tmp_path / "primary") == []


def test_a_redact_rule_keyed_on_looks_like_refuses_a_table_that_switches_it_off(
    tmp_path: Path,
) -> None:
    conn = _off(tmp_path, {"looks_like": {"enabled": False}})
    conn = replace(
        conn,
        profile=replace(conn.profile, redact=(RedactRule(looks_like=("email",)),)),
    )
    result = Engine(MockAdapter(curator_fixture()), conn, tmp_path).generate()
    failed = next(t for t in result.tables if t.fqn == CURATOR)

    assert failed.status == "failed"
    assert "switches looks_like off while a redact rule selects columns by looks_like" in (
        failed.error or ""
    )
    assert next(t for t in result.tables if t.fqn == HERBARIUM).status == "ok"


def _inferable() -> dict[str, MockTable]:
    return referencing_fixture(
        CURATOR,
        "herbarium_id",
        HERBARIUM,
        ddl="CREATE TABLE public.curator (id uuid);\n",
        relationships=[],
        parent_keys=(UniqueKeyMeta(columns=("id",), primary=True),),
    )


def _refers_to(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "primary/public/curator/relationships.yaml"

    return yaml.safe_load(path.read_text())["refers_to"]


def test_inference_switched_off_leaves_the_table_a_target_but_infers_nothing(
    tmp_path: Path,
) -> None:
    Engine(MockAdapter(_inferable()), conn_config(tmp_path), tmp_path).generate()
    inferred = [e for e in _refers_to(tmp_path) if e["detection"] == "inferred"]
    off = replace(
        conn_config(tmp_path),
        rules=(
            RuleConfig(
                match=RuleMatch(tables=(CURATOR,)),
                profile={"relationships": {"infer": False}},
            ),
        ),
    )
    Engine(MockAdapter(_inferable()), off, tmp_path).generate(GenerateRequest(force=True))
    target = yaml.safe_load((tmp_path / "primary/public/herbarium/relationships.yaml").read_text())

    assert inferred
    assert [e for e in _refers_to(tmp_path) if e["detection"] == "inferred"] == []
    assert _statistics(tmp_path, "curator")["disabled"] == ["relationships.infer"]
    assert target["eligible_target"] is True
    assert conformance_errors(tmp_path / "primary") == []


def test_sketches_switched_off_leave_no_sketch_on_that_table_only(tmp_path: Path) -> None:
    Engine(
        MockAdapter(curator_fixture()),
        _off(tmp_path, {"sketches": {"enabled": False}}),
        tmp_path,
    ).generate()
    curator, herbarium = _statistics(tmp_path, "curator"), _statistics(tmp_path, "herbarium")

    assert curator["disabled"] == ["sketches"]
    assert not [c for c in curator["columns"].values() if "sketch" in c]
    assert [c for c in herbarium["columns"].values() if "sketch" in c]
    assert conformance_errors(tmp_path / "primary") == []


def test_flipping_a_switch_reports_no_drift(tmp_path: Path) -> None:
    Engine(MockAdapter(curator_fixture()), conn_config(tmp_path), tmp_path).generate()
    conn = _off(tmp_path, {"looks_like": {"enabled": False}, "sketches": {"enabled": False}})
    result = Engine(MockAdapter(curator_fixture()), conn, tmp_path).compute_diff()

    assert [c for c in result.diff["changes"] if c.get("table") == CURATOR] == []


class TestConformance:
    def test_a_named_measurement_still_emitted_is_an_error(self, tmp_path: Path) -> None:
        Engine(MockAdapter(curator_fixture()), conn_config(tmp_path), tmp_path).generate()
        path = tmp_path / "primary/public/curator/statistics.yaml"
        _rewrite(path, lambda data: data.update(disabled=["null_patterns"]))

        assert "stats.disabled-names-emitted-field" in _codes(tmp_path)
        assert "manifest.profile-disabled-mismatch" in _codes(tmp_path)

    def test_a_switched_off_block_named_unmeasured_is_an_error(self, tmp_path: Path) -> None:
        Engine(
            MockAdapter(curator_fixture()),
            _off(tmp_path, {"null_patterns": {"enabled": False}}),
            tmp_path,
        ).generate()
        path = tmp_path / "primary/public/curator/statistics.yaml"
        _rewrite(path, lambda data: data.update(unmeasured=["null_patterns"]))

        assert "stats.disabled-and-unmeasured" in _codes(tmp_path)

    def test_a_switched_off_census_is_not_owed(self, tmp_path: Path) -> None:
        Engine(
            MockAdapter(curator_fixture()),
            _off(tmp_path, {"null_patterns": {"enabled": False}}),
            tmp_path,
        ).generate()

        assert "stats.null-patterns-absent-with-nulls" not in _codes(tmp_path)

    def test_an_inferred_edge_under_switched_off_inference_is_an_error(
        self,
        tmp_path: Path,
    ) -> None:
        Engine(MockAdapter(_inferable()), conn_config(tmp_path), tmp_path).generate()
        path = tmp_path / "primary/public/curator/statistics.yaml"
        _rewrite(path, lambda data: data.update(disabled=["relationships.infer"]))

        assert "relationships.inferred-edge-under-disabled" in _codes(tmp_path)

    def test_a_measured_edge_touching_switched_off_sketches_is_an_error(
        self,
        tmp_path: Path,
    ) -> None:
        Engine(MockAdapter(curator_fixture()), conn_config(tmp_path), tmp_path).generate()
        path = tmp_path / "primary/public/curator/relationships.yaml"
        _rewrite(
            path,
            lambda data: data["refers_to"].append(
                {
                    "column": ["id"],
                    "target_table": HERBARIUM,
                    "target_column": ["id"],
                    "detection": "measured",
                },
            ),
        )
        stats = tmp_path / "primary/public/herbarium/statistics.yaml"
        _rewrite(stats, lambda data: data.update(disabled=["sketches"]))

        assert "relationships.measured-edge-under-disabled" in _codes(tmp_path)


def test_a_looks_like_search_names_the_tables_it_could_not_search(tmp_path: Path) -> None:
    conn = _off(tmp_path, {"looks_like": {"enabled": False}})
    Engine(MockAdapter(curator_fixture()), conn, tmp_path).generate()
    state = ServedConnections(served={conn.name: conn}, default=conn.name)
    reply = dispatch(state, "search_columns", {"looks_like": "uuid"})

    assert isinstance(reply, dict)
    assert reply["switched_off_tables"] == [CURATOR]


def test_a_predicate_on_a_switched_off_field_is_inapplicable_not_failed() -> None:
    column = {"classification": "text", "cardinality": 5, "sql_type": "text"}
    predicate = PatternPredicate(expected="email")
    ref = resolve(column, "looks_like", disabled={"looks_like"})

    assert ref.reading.state is Absence.DISABLED
    assert inapplicable_reason(column, "c", "looks_like", predicate, ref) == (
        "stat 'looks_like' is not measured for column 'c': switched off for this table"
    )
