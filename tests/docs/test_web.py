"""`web.py` - routes render, templates don't crash on every fixture shape, filters behave."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from dbprint.config import ConnectionConfig
from dbprint.docs import web
from dbprint.docs.web import _human_number, _non_breaking, _pretty_datetime, _relative_time


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

    def test_columns_card_carries_the_skyline_preview(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        body = client.get("/t/primary/seedbank.batch").data.decode()

        assert '<div class="label">columns</div>' in body
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

        body = client.get("/t/primary/seedbank.botanists").data.decode()

        assert "null with:" in body
        assert "Columns null on the same rows" in body

    def test_singular_mention_links_to_plural_table(
        self,
        companion_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([companion_conn]).test_client()

        body = client.get("/t/primary/seedbank.botanists").data.decode()

        assert '<a href="/t/primary/seedbank.botanists">botanist</a>' in body

    def test_sidebar_toggle_button_present(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        body = client.get("/").data.decode()

        assert 'id="sidebar-toggle"' in body

    def test_chart_panels_carry_a_fullscreen_button(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        body = client.get("/t/primary/seedbank.batch").data.decode()

        assert body.count("data-fs-toggle") == 2  # skyline panel + relationship diagram panel
        assert "data-zoomable" in body  # only the diagram panel supports zoom/pan

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
        assert "100.0% covered" in body
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


class TestHumanNumber:
    def test_thousands(self) -> None:
        assert _human_number(1500) == "1.5K"

    def test_boundary_rollover_to_next_unit(self) -> None:
        assert _human_number(999_999) == "1M"

    def test_non_numeric_is_blank(self) -> None:
        assert _human_number("not a number") == ""


class TestNonBreaking:
    def test_replaces_underscores(self) -> None:
        assert _non_breaking("foo_bar") == "foo\u00a0bar"

    def test_none_passthrough(self) -> None:
        assert _non_breaking(None) is None


class TestPrettyDatetime:
    def test_date_only(self) -> None:
        assert _pretty_datetime("2026-05-17") == "May 17, 2026"

    def test_unrepresentable_extreme_date_passes_through(self) -> None:
        assert _pretty_datetime("52030-01-01T00:00:00") == "52030-01-01T00:00:00"

    def test_non_string_passthrough(self) -> None:
        assert _pretty_datetime(42) == 42


class TestRelativeTime:
    def test_recent_reads_in_hours(self) -> None:
        recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")

        assert "hour" in _relative_time(recent)

    @pytest.mark.parametrize("value", ["52030-01-01T00:00:00", 42, None])
    def test_unrepresentable_or_non_string_passes_through(self, value: object) -> None:
        assert _relative_time(value) == value
