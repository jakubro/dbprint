"""`build.py` - the static site crawler: recreate-from-scratch, the marker gate, vendoring."""

from __future__ import annotations

import shutil
from dataclasses import replace
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

    def test_a_written_page_carries_both_context_purposes(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "site"

        build.build_site([rich_conn], output)

        for page in ("t/primary/seedbank.batch", "s/primary/seedbank"):
            text = (output / page / "index.html").read_text()

            assert 'data-purpose-panel="profile"' in text, page
            assert 'data-purpose-panel="query" hidden' in text, page

    def test_a_failed_route_is_named_and_not_written(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-200 response is neither silently dropped nor counted as written."""

        output = tmp_path / "site"
        real_get = build.FlaskClient.get
        failing_route = "/t/primary/seedbank.batch"

        def flaky_get(self: build.FlaskClient, path: str, *a: object, **kw: object):
            if path == failing_route:

                class _Failed:
                    status_code = 500
                    data = b""

                return _Failed()

            return real_get(self, path, *a, **kw)

        monkeypatch.setattr(build.FlaskClient, "get", flaky_get)

        result = build.build_site([rich_conn], output)

        assert failing_route in result.failed_routes
        assert not (output / "t" / "primary" / "seedbank.batch" / "index.html").is_file()
        assert (output / "t" / "primary" / "seedbank.cultivar" / "index.html").is_file()
        # index + cultivar + schema wrote; batch did not.
        assert result.pages_written == 3

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
        assert 'version:"12.1.0"' in (output / "static" / "vendor" / "mermaid.min.js").read_text()

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


class TestABuiltSiteDependsOnThePrintAlone:
    def test_a_built_page_names_no_path_on_the_building_machine(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "site"

        build.build_site([rich_conn], output)

        pages = [page.read_text() for page in output.rglob("*.html")]

        assert pages
        assert not [p for p in pages if str(tmp_path) in p or str(tmp_path.resolve()) in p]

    def test_a_built_page_states_the_profiled_date_not_an_age(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        manifest_path = rich_conn.output / "primary" / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["seedbank.batch"]["profiled_at"] = "2026-03-09T14:27:36Z"
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        output = tmp_path / "site"

        build.build_site([rich_conn], output)

        page = (output / "t" / "primary" / "seedbank.batch" / "index.html").read_text()

        assert ">Mar 09, 2026 14:27:36 UTC</span>" in page
        assert " ago<" not in page

    def test_one_print_builds_to_the_same_bytes_wherever_it_sits(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        elsewhere = tmp_path / "elsewhere" / "prints"
        shutil.copytree(rich_conn.output, elsewhere)
        build.build_site([rich_conn], tmp_path / "one")
        build.build_site([replace(rich_conn, output=elsewhere)], tmp_path / "two")

        one = {
            p.relative_to(tmp_path / "one"): p.read_bytes()
            for p in (tmp_path / "one").rglob("*")
            if p.is_file()
        }
        two = {
            p.relative_to(tmp_path / "two"): p.read_bytes()
            for p in (tmp_path / "two").rglob("*")
            if p.is_file()
        }

        assert one == two
