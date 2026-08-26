"""What the engine writes for a table's functional dependencies (SPEC 2.2.13).

The measurement belongs to the adapters (`probe_dependencies`, covered in
`tests/adapters/test_base_contract.py::TestProbeDependencies`); these cover candidate
selection, the strength threshold, and the skips that leave `dependencies` empty.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import ColumnMeta, ColumnStats, CommentsMeta, MockAdapter, MockTable
from dbprint.config import ConnectionConfig, RuleConfig
from dbprint.engine import Engine


class TestCandidateSelection:
    def test_a_name_adjacent_pair_is_measured_and_emitted(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, strengths={("status", "status_label"): 1.0})

        assert payload["dependencies"] == [
            {"determinant": "status", "dependent": "status_label", "strength": 1.0},
        ]

    def test_a_low_cardinality_pair_with_unrelated_names_is_measured(
        self,
        tmp_path: Path,
    ) -> None:
        payload = _generate(tmp_path, strengths={("rank", "biome"): 1.0})

        assert {"determinant": "rank", "dependent": "biome", "strength": 1.0} in payload[
            "dependencies"
        ]

    def test_direction_follows_cardinality_not_declaration_order(self, tmp_path: Path) -> None:
        """`biome` (cardinality 2) cannot determine `rank` (5) - only the reverse."""

        payload = _generate(
            tmp_path,
            strengths={("biome", "rank"): 1.0, ("rank", "biome"): 1.0},
        )

        determinants = {(d["determinant"], d["dependent"]) for d in payload["dependencies"]}

        assert ("rank", "biome") in determinants
        assert ("biome", "rank") not in determinants

    def test_a_cardinality_tie_tests_both_directions(self, tmp_path: Path) -> None:
        """Equal cardinalities make either orientation mathematically possible."""

        payload = _generate(
            tmp_path,
            strengths={("status", "status_label"): 1.0, ("status_label", "status"): 1.0},
        )

        determinants = {(d["determinant"], d["dependent"]) for d in payload["dependencies"]}

        assert ("status", "status_label") in determinants
        assert ("status_label", "status") in determinants

    def test_a_near_unique_determinant_is_excluded(self, tmp_path: Path) -> None:
        """A candidate key determines everything vacuously - never a reportable finding."""

        payload = _generate(tmp_path, strengths={("status_id", "status"): 1.0})

        assert payload["dependencies"] == []

    def test_a_constant_dependent_is_excluded(self, tmp_path: Path) -> None:
        """Determined by everything the same way - never a reportable finding."""

        payload = _generate(tmp_path, strengths={("status", "status_constant"): 1.0})

        assert payload["dependencies"] == []

    def test_a_null_bearing_column_never_enters_a_candidate_pair(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, strengths={("status", "status_note"): 1.0})

        assert payload["dependencies"] == []

    def test_an_unrelated_high_cardinality_pair_is_never_a_candidate(
        self,
        tmp_path: Path,
    ) -> None:
        """Neither name-adjacent nor both low-cardinality - MockAdapter is never even asked."""

        payload = _generate(tmp_path, strengths={("viability_pct", "rank"): 1.0})

        assert payload["dependencies"] == []


class TestStrengthThreshold:
    def test_a_weak_pairing_is_not_reported(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, strengths={("rank", "biome"): 0.3})

        assert payload["dependencies"] == []

    def test_a_near_dependency_is_reported_with_its_strength(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, strengths={("rank", "biome"): 0.998})

        assert payload["dependencies"] == [
            {"determinant": "rank", "dependent": "biome", "strength": 0.998},
        ]

    def test_no_dependency_reports_nothing(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, strengths={})

        assert payload["dependencies"] == []


class TestSkipConditions:
    def test_scope_suppresses_the_search(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            strengths={("status", "status_label"): 1.0},
            sample=0.5,
        )

        assert payload["dependencies"] == []

    def test_an_empty_table_suppresses_the_search(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            strengths={("status", "status_label"): 1.0},
            row_count=0,
        )

        assert payload["dependencies"] == []


def _generate(
    tmp_path: Path,
    *,
    strengths: dict[tuple[str, str], float],
    sample: float | None = None,
    row_count: int = 10_000,
) -> dict[str, Any]:
    rules = (RuleConfig(sample=sample),) if sample is not None else ()
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
        rules=rules,
    )
    fixture = _fixture(strengths, row_count)
    Engine(MockAdapter(fixture), conn, tmp_path).generate()

    return yaml.safe_load((tmp_path / "w" / "public" / "wide" / "statistics.yaml").read_text())


# One column per candidate-selection rule: `status`/`status_label` name-adjacent at equal
# cardinality, `rank`/`biome` unrelated, `status_*` degenerate, `viability_pct` neither.
_CARDINALITIES = {
    "status": 3,
    "status_label": 3,
    "rank": 5,
    "biome": 2,
    "status_id": 9_999,
    "status_constant": 1,
    "status_note": 3,
    "viability_pct": 500,
}


def _fixture(strengths: dict[tuple[str, str], float], row_count: int) -> dict[str, MockTable]:
    def _column(name: str, *, null_count: int = 0) -> ColumnStats:
        cardinality = _CARDINALITIES[name]

        return ColumnStats(
            sql_type="text",
            nullable=null_count > 0,
            null_count=null_count,
            null_rate=null_count / row_count if row_count else 0.0,
            cardinality=cardinality,
            cardinality_ratio=cardinality / row_count if row_count else 0.0,
            cardinality_method="exact",
            values=(),
            values_coverage=1.0,
            distribution="uniform",
        )

    names = tuple(_CARDINALITIES)
    columns = [
        ColumnMeta(name=name, sql_type="text", nullable=False, default=None, ordinal=i)
        for i, name in enumerate(names, start=1)
    ]
    stats = {name: _column(name, null_count=(1 if name == "status_note" else 0)) for name in names}

    return {
        "public.wide": MockTable(
            type="table",
            namespace_path=("public", "wide"),
            ddl="CREATE TABLE public.wide (placeholder text);\n",
            columns=columns,
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats=stats,
            samples={},
            row_count=row_count,
            dependency_strengths=dict(strengths),
        ),
    }
