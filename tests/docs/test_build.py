"""`build.py` - the static site crawler: recreate-from-scratch, the marker gate, vendoring."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dbprint.config import ConnectionConfig
from dbprint.docs import build


class TestBuildSite:
    def test_writes_index_table_and_schema_pages(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "site"

        result = build.build_site([rich_conn], output)

        assert (output / "index.html").is_file()
        assert (output / "t" / "primary" / "seedbank.batch" / "index.html").is_file()
        assert (output / "t" / "primary" / "seedbank.cultivar" / "index.html").is_file()
        assert (output / "s" / "primary" / "seedbank" / "index.html").is_file()
        assert result.pages_written == 4

    def test_writes_pages_for_every_connection_passed(
        self,
        rich_conn: ConnectionConfig,
        second_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "site"

        result = build.build_site([rich_conn, second_conn], output)

        assert (output / "t" / "primary" / "seedbank.batch" / "index.html").is_file()
        assert (output / "t" / "secondary" / "public.germination_reading" / "index.html").is_file()
        assert (output / "s" / "secondary" / "public" / "index.html").is_file()
        # index + primary's 2 tables + 1 schema + secondary's 1 table + 1 schema
        assert result.pages_written == 6

    def test_copies_static_assets_including_vendored_mermaid(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "site"

        build.build_site([rich_conn], output)

        assert (output / "static" / "app.js").is_file()
        assert (output / "static" / "app.css").is_file()
        assert (output / "static" / "vendor" / "mermaid.min.js").is_file()

    def test_writes_the_ownership_marker(self, rich_conn: ConnectionConfig, tmp_path: Path) -> None:
        output = tmp_path / "site"

        build.build_site([rich_conn], output)

        assert (output / build.MARKER_FILENAME).is_file()

    def test_no_schema_page_for_a_bare_table_name(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        # (none) is a schema_key() sentinel for ungrouped tables, never a navigable page.
        output = tmp_path / "site"

        build.build_site([rich_conn], output)

        assert not (output / "s" / "primary" / "(none)").exists()

    def test_recreate_drops_a_page_for_a_removed_table(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "site"
        build.build_site([rich_conn], output)
        assert (output / "t" / "primary" / "seedbank.cultivar" / "index.html").is_file()

        manifest_path = rich_conn.output / "primary" / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        del manifest["tables"]["seedbank.cultivar"]
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

        build.build_site([rich_conn], output, force=True)

        assert not (output / "t" / "primary" / "seedbank.cultivar").exists()

    def test_refuses_an_unowned_existing_output_without_force(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "site"
        output.mkdir()
        (output / "my_own_file.txt").write_text("not dbprint's to delete\n")

        with pytest.raises(build.OutputNotOwnedError):
            build.build_site([rich_conn], output)

        assert (output / "my_own_file.txt").is_file()

    def test_force_overrides_the_missing_marker_guard(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "site"
        output.mkdir()
        (output / "my_own_file.txt").write_text("gone after --force\n")

        build.build_site([rich_conn], output, force=True)

        assert not (output / "my_own_file.txt").exists()
        assert (output / "index.html").is_file()

    def test_a_prior_dbprint_build_is_recreated_without_force(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "site"
        build.build_site([rich_conn], output)  # first build, writes the marker

        result = build.build_site([rich_conn], output)

        assert result.pages_written == 4
