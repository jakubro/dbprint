"""Baseline manifest / statistics hydration tests."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from dbprint.engine.baseline import contained, escaping_links, generate_refusal, read_manifest
from dbprint.engine.carried import CommittedPrint
from dbprint.engine.diff import (
    GrainKeyState,
    PhysicalLayoutKeyState,
    PhysicalLayoutState,
    TableState,
)
from tests._links import ESCAPING, linked_tree


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


def _states(prints: Path) -> dict[str, TableState] | None:
    return CommittedPrint.load(prints).baseline_states()


class TestColumnHydration:
    def test_columns_populated_from_statistics(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        states = _states(prints)

        assert states is not None
        state = states["public.curator"]
        assert state.columns is not None
        assert set(state.columns) == {"id", "email"}

    def test_sql_type_carried_through(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        states = _states(prints)

        assert states is not None
        cols = states["public.curator"].columns
        assert cols is not None
        assert cols["id"].sql_type == "uuid"
        assert cols["email"].sql_type == "varchar"

    def test_nullable_carried_through(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        states = _states(prints)

        assert states is not None
        cols = states["public.curator"].columns
        assert cols is not None
        assert cols["id"].nullable is False
        assert cols["email"].nullable is True

    def test_default_stays_none_v1_boundary(self, tmp_path: Path) -> None:
        """statistics.yaml carries no `default` field (see `diff.ColumnState.default_known`)."""

        prints = _seed_print(tmp_path)
        states = _states(prints)

        assert states is not None
        cols = states["public.curator"].columns
        assert cols is not None
        assert cols["id"].default is None
        assert cols["email"].default is None

    def test_missing_statistics_yaml_leaves_columns_none(self, tmp_path: Path) -> None:
        states = _states(_print_without_statistics(tmp_path))

        assert states is not None
        assert states["public.curator"].columns is None

    def test_corrupt_statistics_yaml_leaves_columns_none(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        (prints / "public" / "curator" / "statistics.yaml").write_text("{ not: valid")

        states = _states(prints)

        assert states is not None
        assert states["public.curator"].columns is None


class TestRowCountHydration:
    def test_row_count_and_method_carried_through(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        states = _states(prints)

        assert states is not None
        state = states["public.curator"]
        assert state.row_count == 3
        assert state.row_count_method == "exact"

    def test_missing_statistics_yaml_leaves_row_count_none(self, tmp_path: Path) -> None:
        states = _states(_print_without_statistics(tmp_path))

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

        states = _states(prints)

        assert states is not None
        assert states["public.curator"].row_count == 3
        assert states["public.curator"].columns is None


class TestScopedHydration:
    def test_no_scope_block_hydrates_false(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        states = _states(prints)

        assert states is not None
        assert states["public.curator"].scoped is False

    def test_a_scope_block_hydrates_true(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        stats_path = prints / "public" / "curator" / "statistics.yaml"
        data = yaml.safe_load(stats_path.read_text())
        data["scope"] = {"rows_scanned": 1, "sample": 0.5}
        stats_path.write_text(yaml.safe_dump(data))

        states = _states(prints)

        assert states is not None
        assert states["public.curator"].scoped is True

    def test_missing_statistics_yaml_leaves_scoped_false(self, tmp_path: Path) -> None:
        """A baseline predating `scope` reads as unscoped, not as a stop-comparing signal."""

        states = _states(_print_without_statistics(tmp_path))

        assert states is not None
        assert states["public.curator"].scoped is False


class TestGrainAndPhysicalLayoutHydration:
    def test_missing_grain_hydrates_none(self, tmp_path: Path) -> None:
        """A baseline predating `grain` carries none - not an empty `keys` list."""

        prints = _seed_print(tmp_path)
        states = _states(prints)

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

        states = _states(prints)

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
        states = _states(prints)

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

        states = _states(prints)

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

        states = _states(prints)

        assert states is not None
        assert states["public.curator"].columns is None
        assert states["public.curator"].statistics is None

    def test_relationships_holding_a_scalar_leaves_relationships_none(
        self,
        tmp_path: Path,
    ) -> None:
        prints = _seed_print(tmp_path)
        (prints / "public" / "curator" / "relationships.yaml").write_text("just a string\n")

        states = _states(prints)

        assert states is not None
        assert states["public.curator"].relationships is None

    def test_relationships_holding_a_scalar_yields_no_incoming_edges(
        self,
        tmp_path: Path,
    ) -> None:
        prints = _seed_print(tmp_path)
        (prints / "public" / "curator" / "relationships.yaml").write_text("just a string\n")

        assert CommittedPrint.load(prints).tables["public.curator"].referenced_by == ()

    def test_an_ignored_artifact_is_named(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        prints = _seed_print(tmp_path)
        stats = prints / "public" / "curator" / "statistics.yaml"
        stats.write_text("- a\n- b\n")

        with caplog.at_level(logging.WARNING):
            CommittedPrint.load(prints)

        assert str(stats) in caplog.text
        assert "list" in caplog.text

    def test_manifest_holding_a_sequence_is_no_baseline(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        (prints / "manifest.yaml").write_text("- one\n- two\n")

        assert read_manifest(prints).state == "malformed"

    def test_manifest_whose_tables_is_a_sequence_is_no_baseline(self, tmp_path: Path) -> None:
        """Every walker below iterates `tables` as a map, so a list there is unusable."""

        prints = _seed_print(tmp_path)
        (prints / "manifest.yaml").write_text(
            yaml.safe_dump({"format_version": 1, "tables": ["public.curator"]}),
        )

        assert read_manifest(prints).state == "malformed"

    def test_an_unusable_manifest_is_named_in_the_generate_refusal(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        manifest_path = prints / "manifest.yaml"
        manifest_path.write_text("- one\n- two\n")
        refusal = generate_refusal(read_manifest(prints), prints, "primary")

        assert refusal is not None
        assert str(manifest_path) in refusal

    def test_an_entry_a_reader_cannot_follow_is_named_as_dropped(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.specimen_loan"] = {"path": 7}
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))
        read = read_manifest(prints)

        assert read.state == "ok"
        assert read.dropped == ("public.specimen_loan",)
        assert "public.specimen_loan" in (generate_refusal(read, prints, "primary") or "")

    def test_a_table_entry_that_is_not_a_mapping_is_skipped(self, tmp_path: Path) -> None:
        """One unusable entry costs its own table a baseline, not its neighbours'."""

        prints = _seed_print(tmp_path)
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.specimen_loan"] = "not an entry"
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        states = _states(prints)

        assert states is not None
        assert set(states) == {"public.curator"}
        assert states["public.curator"].columns is not None

    def test_a_tables_key_written_with_no_value_is_no_baseline(self, tmp_path: Path) -> None:
        """`dict.get` cannot tell an empty key from an absent one; a reader can."""

        prints = _seed_print(tmp_path)
        (prints / "manifest.yaml").write_text("format_version: 1\ntables:\n")

        assert read_manifest(prints).state == "malformed"

    def test_an_empty_artifact_is_absent_without_comment(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An empty file has nothing in it to be the wrong shape."""

        prints = _seed_print(tmp_path)
        (prints / "public" / "curator" / "statistics.yaml").write_text("")

        with caplog.at_level(logging.WARNING):
            states = _states(prints)

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

        committed = CommittedPrint.load(prints)
        states = committed.baseline_states()
        edges = committed.tables["public.curator"].referenced_by

        assert states is not None
        relationships = states["public.curator"].relationships
        assert relationships is not None
        fk = relationships[0]
        assert (fk.on_delete, fk.on_update, fk.detection) == (None, None, "inferred")

        incoming = edges[0]
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

        states = _states(prints)

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

        states = _states(prints)

        assert states == {}

    def test_a_non_string_artifact_name_leaves_that_table_unhydrated(
        self,
        tmp_path: Path,
    ) -> None:
        prints = _seed_print(tmp_path)
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.curator"]["artifacts"]["statistics"] = 7
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        states = _states(prints)

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

        states = _states(prints)

        assert states is not None
        assert set(states) == {"public.curator"}
        assert states["public.curator"].columns is not None


def _print_without_statistics(tmp_path: Path) -> Path:
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

    return prints


class TestLinksOutOfThePrint:
    """One walk finds every link below the print root that resolves outside it."""

    def test_escaping_links(self, tmp_path: Path) -> None:
        root, _ = linked_tree(tmp_path)

        assert escaping_links(root) == ESCAPING

    def test_a_root_that_does_not_exist_holds_none(self, tmp_path: Path) -> None:
        assert escaping_links(tmp_path / "absent") == frozenset()

    def test_contained(self, tmp_path: Path) -> None:
        root, outside = linked_tree(tmp_path)

        assert contained(root, root / "public" / "curator" / "inside.md")
        assert not contained(root, root / "public" / "curator" / "description.md")
        assert not contained(root, outside / "notes.md")

    def test_an_entry_under_a_link_out_is_dropped(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        shutil.move(prints / "public" / "curator", outside / "curator")
        (prints / "public" / "curator").symlink_to(outside / "curator")

        read = read_manifest(prints)

        assert read.state == "ok"
        assert read.dropped == ("public.curator",)
        assert generate_refusal(read, prints, "primary") is not None

    def test_an_entry_reached_through_a_link_inside_the_print_is_dropped(
        self,
        tmp_path: Path,
    ) -> None:
        prints = _seed_print(tmp_path)
        outside = tmp_path / "outside.yaml"
        shutil.move(prints / "public" / "curator", prints / "shelf")
        shutil.move(prints / "shelf" / "statistics.yaml", outside)
        (prints / "shelf" / "statistics.yaml").symlink_to(outside)
        (prints / "public" / "curator").symlink_to(prints / "shelf")

        assert read_manifest(prints).dropped == ("public.curator",)

    @pytest.mark.parametrize(
        ("path", "plant"),
        [
            ("shelf/loop", lambda prints: (prints / "shelf" / "loop").symlink_to(prints / "shelf")),
            ("shelf\\stack", lambda prints: (prints / "shelf\\stack").symlink_to(prints / "shelf")),
            (".", lambda prints: (prints / "statistics.yaml").symlink_to(prints / "shelf" / "s")),
        ],
        ids=["link-back-up-the-tree", "backslash-in-a-name", "the-root-itself"],
    )
    def test_an_entry_spelled_past_the_walk_is_dropped(
        self,
        tmp_path: Path,
        path: str,
        plant: Callable[[Path], object],
    ) -> None:
        prints = _seed_print(tmp_path)
        outside = tmp_path / "outside.yaml"
        shutil.move(prints / "public" / "curator", prints / "shelf")
        shutil.move(prints / "shelf" / "statistics.yaml", outside)
        (prints / "shelf" / "s").symlink_to(outside)
        (prints / "shelf" / "statistics.yaml").symlink_to(prints / "shelf" / "s")
        plant(prints)
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.curator"]["path"] = path
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        assert read_manifest(prints).dropped == ("public.curator",)

    def test_an_entry_routed_out_and_back_is_dropped(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        shutil.move(prints / "public" / "curator", prints / "shelf")
        (prints / "shelf" / "loop").symlink_to(prints / "shelf")
        (prints / "shelf" / "out").symlink_to(outside)
        (outside / "back").symlink_to(prints / "shelf")
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.curator"]["path"] = "shelf/loop/out/back"
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        assert read_manifest(prints).dropped == ("public.curator",)

    def test_a_backslash_named_link_out_reached_and_back_inside_is_dropped(
        self,
        tmp_path: Path,
    ) -> None:
        prints = _seed_print(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        shutil.move(prints / "public" / "curator", prints / "real")
        (prints / "shelf\\out").symlink_to(outside)
        (outside / "back").symlink_to(prints / "real")
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.curator"]["path"] = "shelf\\out/back"
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        assert read_manifest(prints).dropped == ("public.curator",)

    def test_a_backslash_named_directory_is_not_the_link_its_slashed_spelling_names(
        self,
        tmp_path: Path,
    ) -> None:
        prints = _seed_print(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (prints / "shelf").mkdir()
        (prints / "shelf" / "out").symlink_to(outside)
        shutil.move(prints / "public" / "curator", prints / "shelf\\out")
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.curator"]["path"] = "shelf\\out"
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        assert read_manifest(prints).dropped == ()

    def test_an_entry_that_cannot_resolve_is_dropped_not_raised(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        manifest = yaml.safe_load((prints / "manifest.yaml").read_text())
        manifest["tables"]["public.curator"]["path"] = "public/cur\0ator"
        manifest["tables"][7] = {"type": "table", "artifacts": {"ddl": "ddl.sql"}}
        (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

        assert read_manifest(prints).dropped == ("public.curator",)

    def test_a_manifest_linked_out_reads_as_malformed(self, tmp_path: Path) -> None:
        prints = _seed_print(tmp_path)
        outside = tmp_path / "manifest.yaml"
        shutil.move(prints / "manifest.yaml", outside)
        (prints / "manifest.yaml").symlink_to(outside)

        read = read_manifest(prints)

        assert read.state == "malformed"
        assert read.reason == "it resolves outside the connection root"
