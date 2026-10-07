"""`web.py` - routes render, templates don't crash on every fixture shape, filters behave."""

from __future__ import annotations

import html
import re
import shutil
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from dbprint.config import ConnectionConfig
from dbprint.docs import web
from dbprint.docs.web import _non_breaking, _number, _percent, _pretty_datetime, _relative_time
from dbprint.engine import AssemblyOptions, assemble_context
from dbprint.engine.baseline import read_artifact
from tests._cli import run_cli
from tests._scripts import REPO_ROOT


class TestRoutes:
    def test_index_lists_every_table(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        response = client.get("/")

        assert response.status_code == 200
        assert b"seedbank.batch" in response.data
        assert b"seedbank.cultivar" in response.data

    def test_index_renders_every_connection_not_only_the_first(
        self,
        rich_conn: ConnectionConfig,
        second_conn: ConnectionConfig,
    ) -> None:
        # Every connection passed to create_app renders, with no auto:true narrowing.
        client = web.create_app([rich_conn, second_conn]).test_client()

        response = client.get("/")

        assert response.status_code == 200
        assert b"seedbank.batch" in response.data
        assert b"public.germination_reading" in response.data

    def test_second_connections_table_is_reachable_by_its_own_route(
        self,
        rich_conn: ConnectionConfig,
        second_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([rich_conn, second_conn]).test_client()

        response = client.get("/t/secondary/public.germination_reading")

        assert response.status_code == 200
        assert b"public.germination_reading" in response.data

    def test_table_page_renders_every_new_surface(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        response = client.get("/t/primary/seedbank.batch")
        body = response.data.decode()

        assert response.status_code == 200
        assert "Grain" in body
        assert "Null" in body
        assert "Clustered by" in body
        assert "Dependencies" in body
        assert "sketch" in body  # sketch_available badge on cultivar_id
        assert "flowchart LR" in body  # relationship diagram source

    def test_declared_missing_artifact_is_named_on_the_page(
        self,
        declared_missing_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([declared_missing_conn]).test_client()

        body = client.get("/t/primary/public.t").data.decode()

        assert "Missing: statistics" in body

    def test_summary_cards_include_grain_cardinality_completeness(
        self,
        rich_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([rich_conn]).test_client()

        body = client.get("/t/primary/seedbank.batch").data.decode()

        for label in ("grain", "cardinality", "completeness"):
            assert f'<div class="label">{label}</div>' in body

    def test_cardinality_and_null_count_carry_the_exact_value_reachable(
        self,
        rich_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([rich_conn]).test_client()

        body = client.get("/t/primary/seedbank.batch").data.decode()

        assert re.search(r'class="num" title="\d+">', body)

    def test_columns_card_carries_the_skyline_preview(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        body = client.get("/t/primary/seedbank.batch").data.decode()

        assert "skyline skyline-mini" in body

    def test_metadata_value_is_titlecased(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        body = client.get("/t/primary/seedbank.batch").data.decode()

        assert '<div class="big">Table</div>' in body

    def test_grain_key_column_is_a_hyperlink(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        body = client.get("/t/primary/seedbank.batch").data.decode()

        assert '<a href="#col-batch_id">batch_id</a>' in body

    def test_annotated_grain_key_note_renders_on_the_page(
        self,
        grain_note_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([grain_note_conn]).test_client()

        body = client.get("/t/primary/seedbank.batch").data.decode()

        assert "unique in practice, never enforced" in body
        assert "annotated" in body

    def test_physical_name_line_is_hidden(self, scoped_conn: ConnectionConfig) -> None:
        client = web.create_app([scoped_conn]).test_client()

        body = client.get("/t/primary/seedbank.curation_event").data.decode()

        assert "actionType" not in body  # the only place this fixture's physical_name appeared

    def test_null_companion_note_and_relocated_table_in_columns_tab(
        self,
        companion_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([companion_conn]).test_client()

        body = client.get("/t/primary/seedbank.botanist").data.decode()

        assert "null with:" in body
        assert "Columns null on the same rows" in body

    def test_plural_mention_links_to_singular_table(
        self,
        companion_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([companion_conn]).test_client()

        body = client.get("/t/primary/seedbank.botanist").data.decode()

        assert '<a href="/t/primary/seedbank.botanist">botanists</a>' in body

    def test_sidebar_toggle_button_present(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        body = client.get("/").data.decode()

        assert 'id="sidebar-toggle"' in body

    def test_table_page_never_leaks_a_sketch_payload(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        response = client.get("/t/primary/seedbank.batch")

        assert b"AAAA" not in response.data  # the fixture's raw sketch.values blob

    def test_redacted_column_never_renders_a_boxplot(self, redacted_conn: ConnectionConfig) -> None:
        client = web.create_app([redacted_conn]).test_client()

        response = client.get("/t/primary/seedbank.curator_profile")
        body = response.data.decode()

        assert response.status_code == 200
        assert "boxplot" not in body

    def test_exhaustive_coverage_under_scope_never_claims_the_whole_table(
        self,
        scoped_conn: ConnectionConfig,
    ) -> None:
        # values_coverage 1.0 over a 1% sample must never read as a whole-table claim.
        client = web.create_app([scoped_conn]).test_client()

        response = client.get("/t/primary/seedbank.curation_event")
        body = response.data.decode()

        assert response.status_code == 200
        assert "scanned" in body.lower()
        assert "100% covered" in body
        for overclaim in ("entire domain", "entire table", "complete domain", "whole table"):
            assert overclaim not in body.lower()

    def test_unrepresentable_dates_render_without_crashing(
        self,
        edge_case_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([edge_case_conn]).test_client()

        response = client.get("/t/primary/public.legacy_dates")
        body = response.data.decode()

        assert response.status_code == 200
        assert "unrepresentable" in body.lower()
        assert "measured duplicates" in body.lower()

    def test_a_degraded_read_is_marked_at_both_grains(
        self,
        degraded_conn: ConnectionConfig,
    ) -> None:
        """The one surface built for a human must not render a failed read as a blank cell."""

        client = web.create_app([degraded_conn]).test_client()

        body = client.get("/t/primary/seedbank.storage_reading").data.decode()

        assert "unmeasured: distribution, freshness" in body
        assert "whether a key is declared" in body
        assert "which columns are null on the same rows is unknown" in body
        assert "no dependency between columns is ruled out" in body

    def test_the_page_and_the_context_word_each_lost_block_alike(
        self,
        degraded_conn: ConnectionConfig,
    ) -> None:
        root = degraded_conn.output / degraded_conn.name
        context = assemble_context(
            manifest=read_artifact(root / "manifest.yaml"),
            print_root=root,
            tables=["seedbank.storage_reading"],
            options=AssemblyOptions(),
        ).text
        page = html.unescape(
            web.create_app([degraded_conn])
            .test_client()
            .get("/t/primary/seedbank.storage_reading")
            .data.decode(),
        )
        sentences = [
            line.split(": ", 1)[1]
            for line in context.split("## Blocks in the file's `unmeasured` list", 1)[
                1
            ].splitlines()
            if line.startswith("- `")
        ]

        assert len(sentences) == 3
        assert all(sentence in page for sentence in sentences), sentences

    def test_a_measured_page_carries_no_lost_marker(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        body = client.get("/t/primary/seedbank.batch").data.decode()

        assert "unmeasured: " not in body
        assert "did not measure it" not in body

    def test_empty_columns_table_shows_the_notice(
        self,
        empty_columns_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([empty_columns_conn]).test_client()

        response = client.get("/t/primary/public.narrow")

        assert response.status_code == 200
        assert b"matched no rows" in response.data

    def test_schema_page_renders(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        response = client.get("/s/primary/seedbank")

        assert response.status_code == 200
        assert b"seedbank.batch" in response.data

    def test_unknown_connection_is_404(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        assert client.get("/t/nope/seedbank.batch").status_code == 404

    def test_unknown_table_is_404(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        assert client.get("/t/primary/seedbank.nonexistent").status_code == 404

    def test_unknown_schema_is_404(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        assert client.get("/s/primary/nonexistent").status_code == 404

    def test_vendored_mermaid_is_served_same_origin(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        response = client.get("/static/vendor/mermaid.min.js")

        assert response.status_code == 200

    def test_no_cdn_reference_anywhere_in_a_rendered_page(
        self,
        rich_conn: ConnectionConfig,
    ) -> None:
        # Asset tags only - a url-classified column's own values are legitimately absolute.
        client = web.create_app([rich_conn]).test_client()
        asset_load = re.compile(r'<(?:script|link)[^>]+(?:src|href)="https?://')

        for path in ("/", "/t/primary/seedbank.batch", "/s/primary/seedbank"):
            body = client.get(path).data.decode()
            assert "cdn.jsdelivr.net" not in body
            assert not asset_load.search(body)


class TestNumberFilters:
    def test_a_count_is_its_plain_digits(self) -> None:
        assert _number(9999) == "9999"

    def test_a_small_statistic_is_positional(self) -> None:
        assert (_number(0.00049), _pretty_datetime(4.9e-08)) == ("0.00049", "0.000000049")

    def test_a_share_short_of_complete_is_not_100_percent(self) -> None:
        assert _percent(0.9999) == "99.99%"

    def test_non_numeric_is_blank(self) -> None:
        assert (_number("not a number"), _percent(None)) == ("", "")


class TestNonBreaking:
    def test_replaces_underscores(self) -> None:
        assert _non_breaking("foo_bar") == "foo\u00a0bar"

    def test_none_passthrough(self) -> None:
        assert _non_breaking(None) is None


class TestPrettyDatetime:
    def test_date_only(self) -> None:
        assert _pretty_datetime("2026-03-09") == "Mar 09, 2026"

    def test_unrepresentable_extreme_date_passes_through(self) -> None:
        assert _pretty_datetime("52030-01-01T00:00:00") == "52030-01-01T00:00:00"

    def test_a_number_is_spelled_as_the_artifact_spells_it(self) -> None:
        assert _pretty_datetime(1.8446744073709548e19) == "18446744073709548000.0"

    def test_a_non_string_non_number_passes_through(self) -> None:
        assert _pretty_datetime(None) is None


class TestRelativeTime:
    def test_recent_reads_in_hours(self) -> None:
        recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")

        assert "hour" in _relative_time(recent)

    @pytest.mark.parametrize("value", ["52030-01-01T00:00:00", 42, None])
    def test_unrepresentable_or_non_string_passes_through(self, value: object) -> None:
        assert _relative_time(value) == value


@pytest.fixture
def reference_conn(tmp_path: Path) -> ConnectionConfig:
    """A copy of the packaged reference example, which holds a zero-row and a catalog-only table."""

    source = REPO_ROOT / "docs/format/v1/examples/production/prints"
    shutil.copytree(source, tmp_path / "prints")

    return ConnectionConfig(name="production", adapter="postgres", output=tmp_path / "prints")


class TestAPageClaimsOnlyWhatItsDataSupports:
    def test_a_zero_row_table_reads_no_rows_not_full(
        self,
        reference_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([reference_conn]).test_client()

        body = client.get("/t/production/arboretum.seedbank.storage_reading").data.decode()

        assert "no rows measured" in body
        assert "skyline-bar" not in body
        assert "100%</div>" not in body

    def test_an_unqueried_object_draws_no_null_rate_bar(
        self,
        reference_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([reference_conn]).test_client()

        body = client.get("/t/production/arboretum.seedbank.accession_summary").data.decode()

        assert 'class="bar-fill" style="width:' not in body


class TestAnnotationsWithoutReadableStatistics:
    def test_the_page_still_shows_the_note(self, reference_conn: ConnectionConfig) -> None:
        root = reference_conn.output / reference_conn.name
        manifest = yaml.safe_load((root / "manifest.yaml").read_text())
        entry = manifest["tables"]["arboretum.seedbank.collector"]
        entry["artifacts"]["statistics_annotations"] = "statistics.annotations.yaml"
        (root / "manifest.yaml").write_text(yaml.safe_dump(manifest))
        table_dir = root / entry["path"]
        (table_dir / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "columns": {"email": {"note": "a field station address"}}},
            ),
        )
        (table_dir / "statistics.yaml").write_text("not: valid: yaml: [")
        client = web.create_app([reference_conn]).test_client()

        body = client.get("/t/production/arboretum.seedbank.collector").data.decode()

        assert "a field station address" in body


class TestServe:
    def test_a_port_in_use_raises_before_announcing(self, reference_conn: ConnectionConfig) -> None:
        from dbprint.docs import serve

        announced: list[bool] = []

        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()

            with pytest.raises(OSError):
                serve(
                    [reference_conn],
                    "127.0.0.1",
                    taken.getsockname()[1],
                    lambda: announced.append(True),
                )

        assert announced == []


@pytest.fixture
def reference_project(tmp_path: Path) -> Path:
    """The packaged reference example whole - config and print - so the real CLI can read it."""

    project = tmp_path / "production"
    shutil.copytree(REPO_ROOT / "docs/format/v1/examples/production", project)

    return project


@pytest.fixture
def carried_project(reference_project: Path) -> Path:
    """The reference project after a run that failed `vault`, whose earlier entry it carries."""

    manifest_path = reference_project / "prints" / "production" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["failed_tables"] = ["arboretum.seedbank.vault"]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

    return reference_project


def _context_sources(page: str) -> dict[str, str]:
    found = re.findall(
        r'data-purpose-panel="(\w+)".*?<pre class="context-source">(.*?)</pre>',
        page,
        re.DOTALL,
    )

    return {purpose: html.unescape(text) for purpose, text in found}


class TestTheContextTab:
    """The tab shows, per purpose, exactly what `dbprint context` prints for the page's tables."""

    @pytest.mark.parametrize(
        ("route", "selection"),
        [
            ("/t/production/arboretum.seedbank.accession", "arboretum.seedbank.accession"),
            ("/s/production/arboretum.seedbank", "arboretum.seedbank.*"),
            ("/s/production/arboretum.fixture", "arboretum.fixture.*"),
        ],
    )
    def test_each_purpose_is_byte_identical_to_the_cli(
        self,
        reference_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        route: str,
        selection: str,
    ) -> None:
        conn = ConnectionConfig(
            name="production",
            adapter="postgres",
            output=reference_project / "prints",
        )
        page = web.create_app([conn]).test_client().get(route).get_data(as_text=True)
        shown = _context_sources(page)

        for purpose in ("profile", "query"):
            result = run_cli(
                reference_project,
                monkeypatch,
                ["context", selection, "production", "--purpose", purpose, "--no-tui"],
            )

            assert result.exit_code == 0, result.output
            assert shown[purpose] == result.stdout

    def test_a_multi_table_schema_carries_the_connection_header(
        self,
        reference_conn: ConnectionConfig,
    ) -> None:
        page = (
            web.create_app([reference_conn]).test_client().get("/s/production/arboretum.seedbank")
        )
        shown = _context_sources(page.get_data(as_text=True))

        assert shown["profile"].startswith("# Context for connection production (9 tables)")

    def test_a_one_table_schema_matches_that_tables_own_tab(
        self,
        reference_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([reference_conn]).test_client()
        schema = _context_sources(
            client.get("/s/production/arboretum.fixture").get_data(as_text=True),
        )
        table = _context_sources(
            client.get("/t/production/arboretum.fixture.shape_probe").get_data(as_text=True),
        )

        assert schema == table

    def test_profile_shows_first_and_query_is_hidden(
        self,
        reference_conn: ConnectionConfig,
    ) -> None:
        page = (
            web.create_app([reference_conn])
            .test_client()
            .get(
                "/t/production/arboretum.seedbank.accession",
            )
        )
        text = page.get_data(as_text=True)

        assert '<button class="tab-btn" data-tab="context">Context</button>' in text
        assert '<div data-purpose-panel="profile">' in text
        assert '<div data-purpose-panel="query" hidden>' in text

    def test_a_carried_unprofiled_table_matches_the_cli(
        self,
        carried_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        conn = ConnectionConfig(
            name="production",
            adapter="postgres",
            output=carried_project / "prints",
        )
        page = web.create_app([conn]).test_client().get("/t/production/arboretum.seedbank.vault")
        shown = _context_sources(page.get_data(as_text=True))
        result = run_cli(
            carried_project,
            monkeypatch,
            ["context", "arboretum.seedbank.vault", "production", "--no-tui"],
        )

        assert "Unprofiled: " in shown["profile"]
        assert shown["profile"] == result.stdout

    def test_markup_in_a_value_renders_as_text(self) -> None:
        rendered = web._render_context("| v |\n|---|\n| '<script>x</script>' [a](javascript:x) |\n")

        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered
        assert "href" not in rendered

    def test_the_identity_lines_keep_their_breaks(self) -> None:
        assert "Adapter: postgres<br />" in web._render_context(
            "# Table: t\nAdapter: postgres\nGrain: id\n",
        )
