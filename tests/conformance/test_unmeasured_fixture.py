"""The one committed print that carries `unmeasured`, and the gate holding it to the producer.

Both shipped examples come from healthy runs, so no `statistics.yaml` on disk shows the marker.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from dbprint.conformance import validate_print
from tests.conftest import normalize_print_tree
from tests.fixtures import unmeasured_print


COMMITTED = unmeasured_print.COMMITTED / unmeasured_print.CONNECTION
LOST = (
    "distribution",
    "frequencies",
    "freshness",
    "percentiles",
    "quantized_count",
    "range",
    "values",
)


def _statistics() -> dict:
    return yaml.safe_load((COMMITTED / "seedbank" / "accession" / "statistics.yaml").read_text())


class TestTheCommittedPrintIsWhatTheProducerWrites:
    def test_it_matches_a_fresh_build(self, tmp_path: Path) -> None:
        """Regenerate with `python -m tests.fixtures.unmeasured_print` when this fails."""

        fresh = unmeasured_print.build(tmp_path)

        assert normalize_print_tree(COMMITTED) == normalize_print_tree(fresh)

    def test_it_validates_through_both_conformance_layers(self) -> None:
        """`validate_print` runs the JSON Schemas and the semantic checks over one tree."""

        assert validate_print(COMMITTED) == []


class TestItDemonstratesBothGrains:
    """Removing either marker from the committed copy fails here as well as in the golden."""

    def test_a_column_names_the_whole_temporal_block_it_lost(self) -> None:
        column = _statistics()["columns"]["logged_at"]

        assert column["unmeasured"] == list(LOST)
        assert all(name not in column for name in LOST)

    def test_the_file_names_the_census_it_could_not_take(self) -> None:
        statistics = _statistics()

        assert statistics["unmeasured"] == ["null_patterns"]
        assert "null_patterns" not in statistics

    def test_a_column_carries_the_nulls_the_census_never_read(self) -> None:
        """Without one, an absent `null_patterns` would be a true statement rather than a lost read."""

        assert _statistics()["columns"]["field_notes"]["null_count"] > 0
