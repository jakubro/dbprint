"""Baseline manifest / statistics hydration tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from dbprint.engine.baseline import (
    baseline_states_from_manifest,
    hydrate_baseline_states,
    load_baseline_manifest,
    load_incoming_edges,
)
from dbprint.engine.diff import GrainKeyState, PhysicalLayoutKeyState, PhysicalLayoutState


def _seed_print(tmp_path: Path) -> Path:
    """Write a minimal valid print under tmp_path with one table public.curator."""

    prints = tmp_path / "primary"
    table_dir = prints / "public" / "curator"
    table_dir.mkdir(parents=True)

    manifest = {
        "format_version": 1,
        "generated_at": "2026-06-08T00:00:00Z",
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.1.0",
        "tables": {
            "public.curator": {
                "type": "table",
                "path": "public/curator",
                "artifacts": {
                    "statistics": "statistics.yaml",
                    "relationships": "relationships.yaml",
                },
                "row_count": 3,
                "columns": 2,
                "profiled_at": "2026-06-08T00:00:00Z",
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "public.curator",
        "type": "table",
        "profiled_at": "2026-06-08T00:00:00Z",
        "row_count": 3,
        "row_count_method": "exact",
        "columns": {
            "id": {
                "sql_type": "uuid",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 3,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "categorical",
            },
            "email": {
                "sql_type": "varchar",
                "nullable": True,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 3,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "text",
            },
        },
    }
    relationships = {
        "format_version": 1,
        "table": "public.curator",
        "profiled_at": "2026-06-08T00:00:00Z",
        "refers_to": [],
        "referenced_by": [],
    }

    (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (table_dir / "statistics.yaml").write_text(yaml.safe_dump(statistics))
    (table_dir / "relationships.yaml").write_text(yaml.safe_dump(relationships))

    return prints


class TestColumnHydration:
    def test_columns_populated_from_statistics(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        state = states["public.curator"]
        assert state.columns is not None
        assert set(state.columns) == {"id", "email"}

    def test_sql_type_carried_through(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        cols = states["public.curator"].columns
        assert cols is not None
        assert cols["id"].sql_type == "uuid"
        assert cols["email"].sql_type == "varchar"

    def test_nullable_carried_through(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        cols = states["public.curator"].columns
        assert cols is not None
        assert cols["id"].nullable is False
        assert cols["email"].nullable is True

    def test_default_stays_none_v1_boundary(self, tmp_path: Path) -> None:
        """statistics.yaml carries no `default` field (see `diff.ColumnState.default_known`)."""

        prints = _seed_print(tmp_path)
        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        cols = states["public.curator"].columns
        assert cols is not None
        assert cols["id"].default is None
        assert cols["email"].default is None

    def test_missing_statistics_yaml_leaves_columns_none(self, tmp_path: Path) -> None:
        prints = tmp_path / "primary"
        (prints / "public" / "curator").mkdir(parents=True)
        manifest = {
            "format_version": 1,
            "tables": {
                "public.curator": {
                    "type": "table",
                    "path": "public/curator",
                    "artifacts": {"statistics": "statistics.yaml"},
                    "columns": 0,
                    "profiled_at": "2026-06-08T00:00:00Z",
                },
            },
        }
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        loaded = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(loaded)
        hydrate_baseline_states(states, prints, loaded)

        assert states is not None
        assert states["public.curator"].columns is None

    def test_corrupt_statistics_yaml_leaves_columns_none(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        (prints / "public" / "curator" / "statistics.yaml").write_text("{ not: valid")

        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        assert states["public.curator"].columns is None


class TestRowCountHydration:
    def test_row_count_and_method_carried_through(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        state = states["public.curator"]
        assert state.row_count == 3
        assert state.row_count_method == "exact"

    def test_missing_statistics_yaml_leaves_row_count_none(self, tmp_path: Path) -> None:
        prints = tmp_path / "primary"
        (prints / "public" / "curator").mkdir(parents=True)
        manifest = {
            "format_version": 1,
            "tables": {
                "public.curator": {
                    "type": "table",
                    "path": "public/curator",
                    "artifacts": {"statistics": "statistics.yaml"},
                    "columns": 0,
                    "profiled_at": "2026-06-08T00:00:00Z",
                },
            },
        }
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        loaded = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(loaded)
        hydrate_baseline_states(states, prints, loaded)

        assert states is not None
        assert states["public.curator"].row_count is None
        assert states["public.curator"].row_count_method is None

    def test_malformed_columns_field_still_hydrates_row_count(self, tmp_path: Path) -> None:
        """A malformed `columns` map shouldn't hide a valid top-level row_count."""

        prints = _seed_print(tmp_path)
        stats_path = prints / "public" / "curator" / "statistics.yaml"
        data = yaml.safe_load(stats_path.read_text())
        data["columns"] = "not-a-mapping"
        stats_path.write_text(yaml.safe_dump(data))

        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        assert states["public.curator"].row_count == 3
        assert states["public.curator"].columns is None


class TestScopedHydration:
    def test_no_scope_block_hydrates_false(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        assert states["public.curator"].scoped is False

    def test_a_scope_block_hydrates_true(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        stats_path = prints / "public" / "curator" / "statistics.yaml"
        data = yaml.safe_load(stats_path.read_text())
        data["scope"] = {"rows_scanned": 1, "sample": 0.5}
        stats_path.write_text(yaml.safe_dump(data))

        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        assert states["public.curator"].scoped is True

    def test_missing_statistics_yaml_leaves_scoped_false(self, tmp_path: Path) -> None:
        """A baseline predating `scope` reads as unscoped, not as a stop-comparing signal."""

        prints = tmp_path / "primary"
        (prints / "public" / "curator").mkdir(parents=True)
        manifest = {
            "format_version": 1,
            "tables": {
                "public.curator": {
                    "type": "table",
                    "path": "public/curator",
                    "artifacts": {"statistics": "statistics.yaml"},
                    "columns": 0,
                    "profiled_at": "2026-06-08T00:00:00Z",
                },
            },
        }
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        loaded = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(loaded)
        hydrate_baseline_states(states, prints, loaded)

        assert states is not None
        assert states["public.curator"].scoped is False


class TestGrainAndPhysicalLayoutHydration:
    def test_missing_grain_hydrates_none(self, tmp_path: Path) -> None:
        """A baseline predating `grain` carries none - not an empty `keys` list."""

        prints = _seed_print(tmp_path)
        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        assert states["public.curator"].grain is None

    def test_a_grain_block_hydrates_its_keys(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        stats_path = prints / "public" / "curator" / "statistics.yaml"
        data = yaml.safe_load(stats_path.read_text())
        data["grain"] = {
            "keys": [{"columns": ["id"], "detection": "declared"}],
            "search": {"exhausted": True},
        }
        stats_path.write_text(yaml.safe_dump(data))

        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        grain = states["public.curator"].grain
        assert grain is not None
        assert grain.keys == (GrainKeyState(columns=("id",), detection="declared"),)
        assert grain.search_exhausted is True

    def test_missing_physical_layout_hydrates_the_unclustered_sentinel(
        self,
        tmp_path: Path,
    ) -> None:
        """Absence means "not clustered" per SPEC 2.2.11, never a comparison-suppressing None."""

        prints = _seed_print(tmp_path)
        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        assert states["public.curator"].physical_layout == PhysicalLayoutState(
            mechanism="",
            keys=(),
        )

    def test_a_physical_layout_block_hydrates_its_keys(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        stats_path = prints / "public" / "curator" / "statistics.yaml"
        data = yaml.safe_load(stats_path.read_text())
        data["physical_layout"] = {
            "mechanism": "cluster",
            "keys": [{"expression": "id"}],
        }
        stats_path.write_text(yaml.safe_dump(data))

        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        layout = states["public.curator"].physical_layout
        assert layout == PhysicalLayoutState(
            mechanism="cluster",
            keys=(PhysicalLayoutKeyState(expression="id", column=None),),
        )


class TestWrongShapeArtifacts:
    """An artifact that parses but is not the shape its reader assumes is absent, and named
    in a warning rather than silently ignored.
    """

    def test_statistics_holding_a_list_leaves_columns_none(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        (prints / "public" / "curator" / "statistics.yaml").write_text("- a\n- b\n")

        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        assert states["public.curator"].columns is None
        assert states["public.curator"].statistics is None

    def test_relationships_holding_a_scalar_leaves_relationships_none(
        self,
        tmp_path: Path,
    ) -> None:
        prints = _seed_print(tmp_path)
        (prints / "public" / "curator" / "relationships.yaml").write_text("just a string\n")

        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        assert states["public.curator"].relationships is None

    def test_relationships_holding_a_scalar_yields_no_incoming_edges(
        self,
        tmp_path: Path,
    ) -> None:
        prints = _seed_print(tmp_path)
        (prints / "public" / "curator" / "relationships.yaml").write_text("just a string\n")

        assert load_incoming_edges(prints, load_baseline_manifest(prints)) == {}

    def test_an_ignored_artifact_is_named(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        prints = _seed_print(tmp_path)
        stats = prints / "public" / "curator" / "statistics.yaml"
        stats.write_text("- a\n- b\n")

        with caplog.at_level(logging.WARNING):
            manifest = load_baseline_manifest(prints)
            hydrate_baseline_states(baseline_states_from_manifest(manifest), prints, manifest)

        assert str(stats) in caplog.text
        assert "list" in caplog.text

    def test_manifest_holding_a_sequence_is_no_baseline(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        (prints / "manifest.yaml").write_text("- one\n- two\n")

        assert load_baseline_manifest(prints) is None

    def test_manifest_whose_tables_is_a_sequence_is_no_baseline(self, tmp_path: Path) -> None:
        """Every walker below iterates `tables` as a map, so a list there is unusable."""

        prints = _seed_print(tmp_path)
        (prints / "manifest.yaml").write_text(
            yaml.safe_dump({"format_version": 1, "tables": ["public.curator"]}),
        )

        assert load_baseline_manifest(prints) is None

    def test_an_ignored_manifest_is_named(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        prints = _seed_print(tmp_path)
        manifest_path = prints / "manifest.yaml"
        manifest_path.write_text("- one\n- two\n")

        with caplog.at_level(logging.WARNING):
            load_baseline_manifest(prints)

        assert str(manifest_path) in caplog.text

    def test_a_table_entry_that_is_not_a_mapping_is_skipped(self, tmp_path: Path) -> None:
        """One unusable entry costs its own table a baseline, not its neighbours'."""

        prints = _seed_print(tmp_path)
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.specimen_loan"] = "not an entry"
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        loaded = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(loaded)
        hydrate_baseline_states(states, prints, loaded)

        assert states is not None
        assert set(states) == {"public.curator"}
        assert states["public.curator"].columns is not None

    def test_a_tables_key_written_with_no_value_is_no_baseline(self, tmp_path: Path) -> None:
        """`dict.get` cannot tell an empty key from an absent one; a reader can."""

        prints = _seed_print(tmp_path)
        (prints / "manifest.yaml").write_text("format_version: 1\ntables:\n")

        assert load_baseline_manifest(prints) is None

    def test_an_empty_artifact_is_absent_without_comment(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An empty file has nothing in it to be the wrong shape."""

        prints = _seed_print(tmp_path)
        (prints / "public" / "curator" / "statistics.yaml").write_text("")

        with caplog.at_level(logging.WARNING):
            manifest = load_baseline_manifest(prints)
            states = baseline_states_from_manifest(manifest)
            hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        assert states["public.curator"].columns is None
        assert caplog.text == ""


class TestRelationshipEdgeDefaults:
    """An inferred edge declares no referential action (SPEC 2.3.8), so neither may be defaulted
    to a declared-looking value; an absent `detection` hydrates as `inferred`, no SPEC default.
    """

    def _write_relationships(self, tmp_path: Path, relationships: dict) -> Path:
        prints = _seed_print(tmp_path)
        (prints / "public" / "curator" / "relationships.yaml").write_text(
            yaml.safe_dump(relationships),
        )

        return prints

    def test_an_edge_omitting_the_fields_hydrates_as_inferred_with_no_action(
        self,
        tmp_path: Path,
    ) -> None:
        prints = self._write_relationships(
            tmp_path,
            {
                "format_version": 1,
                "table": "public.curator",
                "profiled_at": "2026-06-08T00:00:00Z",
                "refers_to": [
                    {
                        "column": ["herbarium_id"],
                        "target_table": "public.herbarium",
                        "target_column": ["id"],
                    },
                ],
                "referenced_by": [
                    {
                        "column": ["id"],
                        "referencer_table": "public.accession",
                        "referencer_column": ["curator_id"],
                    },
                ],
            },
        )

        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)
        edges = load_incoming_edges(prints, manifest)

        assert states is not None
        relationships = states["public.curator"].relationships
        assert relationships is not None
        fk = relationships[0]
        assert (fk.on_delete, fk.on_update, fk.detection) == (None, None, "inferred")

        incoming = edges["public.curator"][0]
        assert (incoming.on_delete, incoming.on_update, incoming.detection) == (
            None,
            None,
            "inferred",
        )

    def test_an_edge_carrying_the_fields_hydrates_them_verbatim(self, tmp_path: Path) -> None:
        prints = self._write_relationships(
            tmp_path,
            {
                "format_version": 1,
                "table": "public.curator",
                "profiled_at": "2026-06-08T00:00:00Z",
                "refers_to": [
                    {
                        "column": ["herbarium_id"],
                        "target_table": "public.herbarium",
                        "target_column": ["id"],
                        "on_delete": "CASCADE",
                        "on_update": "NO ACTION",
                        "detection": "declared",
                    },
                ],
                "referenced_by": [],
            },
        )

        manifest = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(manifest)
        hydrate_baseline_states(states, prints, manifest)

        assert states is not None
        relationships = states["public.curator"].relationships
        assert relationships is not None
        fk = relationships[0]
        assert (fk.on_delete, fk.on_update, fk.detection) == ("CASCADE", "NO ACTION", "declared")


class TestAnEntryTheReaderCannotFollow:
    """`path` and the artifact names are joined onto a directory, so a non-string value
    drops that entry whole while the tables beside it hydrate normally.
    """

    def test_a_non_string_path_drops_its_own_table(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.curator"]["path"] = 5
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        loaded = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(loaded)

        assert states == {}

    def test_a_non_string_artifact_name_leaves_that_table_unhydrated(
        self,
        tmp_path: Path,
    ) -> None:
        prints = _seed_print(tmp_path)
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.curator"]["artifacts"]["statistics"] = 7
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        loaded = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(loaded)
        hydrate_baseline_states(states, prints, loaded)

        assert states is not None
        assert states["public.curator"].columns is None
        assert states["public.curator"].relationships is not None

    def test_a_neighbour_is_untouched(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.specimen_loan"] = {
            **manifest["tables"]["public.curator"],
            "path": 5,
        }
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        loaded = load_baseline_manifest(prints)
        states = baseline_states_from_manifest(loaded)
        hydrate_baseline_states(states, prints, loaded)

        assert states is not None
        assert set(states) == {"public.curator"}
        assert states["public.curator"].columns is not None
