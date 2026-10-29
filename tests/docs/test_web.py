"""`web.py` - routes render, templates don't crash on every fixture shape, filters behave."""

from __future__ import annotations

import html
import re
import shutil
import socket
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from dbprint.config import ConnectionConfig
from dbprint.docs import web
from dbprint.docs.web import (
    _non_breaking,
    _number,
    _percent,
    _pretty_datetime,
    _relative_time,
    _render_markdown,
)
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

    def test_a_served_page_states_the_profiled_age(self, rich_conn: ConnectionConfig) -> None:
        client = web.create_app([rich_conn]).test_client()

        page = client.get("/t/primary/seedbank.batch").get_data(as_text=True)

        assert re.search(r'profiled <span class="badge badge-muted" title="[^"]+">[^<]* ago<', page)

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
        assert b'version:"12.2.0"' in response.data

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


_URL_ATTRIBUTES = frozenset({"href", "src", "srcdoc", "action", "formaction", "xlink:href"})


class _Parsed(HTMLParser):
    def __init__(self, page: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[str] = []
        self.text: list[str] = []
        self._tag = ""
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))
        self._tag = tag

    def handle_data(self, data: str) -> None:
        (self.scripts if self._tag == "script" else self.text).append(data)

    def live(self, marker: str) -> list[str]:
        """Where `marker` reaches the page as markup or script rather than as text."""

        attributes = [
            f"{tag}[{name}]"
            for tag, attrs in self.elements
            for name, value in attrs.items()
            if (name.startswith("on") or name in _URL_ATTRIBUTES) and marker in (value or "")
        ]

        return attributes + [f"script: {data[:40]}" for data in self.scripts if marker in data]

    def shows(self, marker: str) -> bool:
        return marker in "".join(self.text)


class TestUserMarkdown:
    @pytest.mark.parametrize(
        "text",
        [
            "<script>alert('seedbank')</script>",
            "before <b onclick=\"alert('seedbank')\">bold</b> after",
        ],
    )
    def test_raw_html_shows_as_text(self, text: str) -> None:
        parsed = _Parsed(web._render_markdown(text))

        assert parsed.live("seedbank") == []
        assert {tag for tag, _ in parsed.elements} <= {"p"}
        assert parsed.shows("seedbank")

    @pytest.mark.parametrize(
        "text",
        [
            "[x](javascript:alert(1))",
            "[x](&#106;avascript:alert(1))",
            "[x](javascript&colon;alert(1))",
            "[x](JAVASCRIPT:alert(1))",
            "[x](java&#9;script:alert(1))",
            "[x][r]\n\n[r]: javascript:alert(1)",
            "[x](data:text/html,<b>x</b>)",
        ],
    )
    def test_a_refused_link_keeps_its_text_and_loses_its_target(self, text: str) -> None:
        parsed = _Parsed(web._render_markdown(text))

        assert parsed.elements[1:] == [("a", {})]
        assert parsed.shows("x")

    def test_a_refused_image_keeps_its_alt_and_loses_its_source(self) -> None:
        parsed = _Parsed(web._render_markdown("![herbarium sheet](javascript:alert(1))"))

        assert parsed.elements[1:] == [("img", {"alt": "herbarium sheet"})]

    @pytest.mark.parametrize(
        ("text", "target"),
        [
            ("[x](https://seedbank.example/a)", "https://seedbank.example/a"),
            ("[x](http://seedbank.example/a)", "http://seedbank.example/a"),
            ("[x](mailto:curator@seedbank.example)", "mailto:curator@seedbank.example"),
            ("[x](/t/primary/seedbank.batch)", "/t/primary/seedbank.batch"),
            ("[x](#col-batch_id)", "#col-batch_id"),
            ("<curator@seedbank.example>", "mailto:curator@seedbank.example"),
        ],
    )
    def test_a_web_mail_or_relative_link_stays_live(self, text: str, target: str) -> None:
        anchors = [a for tag, a in _Parsed(web._render_markdown(text)).elements if tag == "a"]

        assert anchors == [{"href": target}]

    def test_ordinary_markdown_still_renders(self) -> None:
        text = "*dried* seed\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\nshelf S001\n```\n"
        tags = {tag for tag, _ in _Parsed(web._render_markdown(text)).elements}

        assert {"em", "table", "td", "pre", "code"} <= tags


class TestPrintTextNeverRunsLive:
    """Every page that renders a description or a note, with a print that carries markup."""

    def test_a_description_and_a_column_note(self, rich_conn: ConnectionConfig) -> None:
        table = rich_conn.print_root / "seedbank" / "batch"
        (table / "description.md").write_text("<script>alert('seedbank-description')</script>\n")
        annotations = yaml.safe_load((table / "statistics.annotations.yaml").read_text())
        annotations["columns"]["cultivar_id"]["note"] = '<img src=x onerror="seedbank-note">'
        (table / "statistics.annotations.yaml").write_text(yaml.safe_dump(annotations))

        page = web.create_app([rich_conn]).test_client().get("/t/primary/seedbank.batch")
        parsed = _Parsed(page.data.decode())

        for marker in ("seedbank-description", "seedbank-note"):
            assert parsed.live(marker) == []
            assert parsed.shows(marker)

    def test_a_grain_key_note(self, grain_note_conn: ConnectionConfig) -> None:
        path = grain_note_conn.print_root / "seedbank" / "batch" / "statistics.annotations.yaml"
        annotations = yaml.safe_load(path.read_text())
        annotations["grain"]["keys"][0]["note"] = "[shelf](javascript:alert('seedbank-grain'))"
        path.write_text(yaml.safe_dump(annotations))

        page = web.create_app([grain_note_conn]).test_client().get("/t/primary/seedbank.batch")
        parsed = _Parsed(page.data.decode())

        assert parsed.live("seedbank-grain") == []
        assert parsed.shows("shelf")

    def test_a_note_beside_unreadable_statistics(self, reference_conn: ConnectionConfig) -> None:
        root = reference_conn.output / reference_conn.name
        manifest = yaml.safe_load((root / "manifest.yaml").read_text())
        entry = manifest["tables"]["arboretum.seedbank.collector"]
        entry["artifacts"]["statistics_annotations"] = "statistics.annotations.yaml"
        (root / "manifest.yaml").write_text(yaml.safe_dump(manifest))
        note = '<b onmouseover="seedbank-unreadable">station</b>'
        (root / entry["path"] / "statistics.annotations.yaml").write_text(
            yaml.safe_dump({"format_version": 1, "columns": {"email": {"note": note}}}),
        )
        (root / entry["path"] / "statistics.yaml").write_text("not: valid: yaml: [")

        client = web.create_app([reference_conn]).test_client()
        parsed = _Parsed(client.get("/t/production/arboretum.seedbank.collector").data.decode())

        assert parsed.live("seedbank-unreadable") == []
        assert parsed.shows("seedbank-unreadable")

    def test_connection_notes_on_the_index(self, rich_conn: ConnectionConfig) -> None:
        (rich_conn.print_root / "manifest.annotations.yaml").write_text(
            yaml.safe_dump(
                {"format_version": 1, "notes": "<script>alert('seedbank-connection')</script>"},
            ),
        )

        parsed = _Parsed(web.create_app([rich_conn]).test_client().get("/").data.decode())

        assert parsed.live("seedbank-connection") == []
        assert parsed.shows("seedbank-connection")

    def test_mermaid_runs_at_the_strict_level(self, rich_conn: ConnectionConfig) -> None:
        page = web.create_app([rich_conn]).test_client().get("/t/primary/seedbank.batch")

        assert 'securityLevel: "strict"' in page.data.decode()


_APP_JS = REPO_ROOT / "src" / "dbprint" / "docs" / "static" / "app.js"
_INNER_HTML_RE = re.compile(r"\.innerHTML\s*=\s*([^;]+);")


def test_app_js_writes_no_data_through_inner_html() -> None:
    """A row's `data-type` is decoded by the browser, so data written as markup parses live."""

    assigned = _INNER_HTML_RE.findall(_APP_JS.read_text(encoding="utf-8"))

    assert assigned, "the sweep found no innerHTML write at all"
    assert [rhs for rhs in assigned if "${" in rhs or "+" in rhs] == []


def test_the_inner_html_sweep_sees_an_interpolated_write() -> None:
    planted = "header.innerHTML = `<td>${label}</td>`;\ncell.innerHTML = '<b>' + key + '</b>';"

    assert len(_INNER_HTML_RE.findall(planted)) == 2
    assert all("${" in rhs or "+" in rhs for rhs in _INNER_HTML_RE.findall(planted))


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


_REL_OBSERVED_RE = re.compile(r'<div class="rel-observed">(.*?)</div>', re.DOTALL)


def _measured_joins(page: str) -> list[str]:
    return [" ".join(html.unescape(block).split()) for block in _REL_OBSERVED_RE.findall(page)]


class TestTheMeasuredJoinDetail:
    def test_containment_follows_the_coverage_in_both_directions(
        self,
        reference_conn: ConnectionConfig,
    ) -> None:
        client = web.create_app([reference_conn]).test_client()

        joins = _measured_joins(
            client.get("/t/production/arboretum.seedbank.accession").data.decode(),
        )

        assert joins[0].endswith(
            "covers 100% of target, 100% of referencing values contained (400 answerable)",
        )
        assert joins[2].endswith(
            "covers 12.8% of target, 100% of referencing values contained (117 answerable)",
        )
        assert [join for join in joins if " ," in join] == []

    def test_an_edge_without_containment_ends_at_the_coverage(
        self,
        rich_conn: ConnectionConfig,
    ) -> None:
        path = rich_conn.print_root / "seedbank" / "batch" / "relationships.yaml"
        relationships = yaml.safe_load(path.read_text())
        del relationships["refers_to"][0]["observed"]["containment"]
        path.write_text(yaml.safe_dump(relationships))

        page = web.create_app([rich_conn]).test_client().get("/t/primary/seedbank.batch")

        assert _measured_joins(page.data.decode()) == [
            "fanout avg 7.5 (max 9), covers 100% of target",
        ]


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


def test_a_target_path_alone_still_renders_on_the_edge(rich_conn: ConnectionConfig) -> None:
    annotations = rich_conn.print_root / "seedbank" / "batch" / "relationships.annotations.yaml"
    data = yaml.safe_load(annotations.read_text())
    data["refers_to"].append(
        {
            "column": ["cultivar_id"],
            "target_table": "seedbank.cultivar",
            "target_column": ["traits"],
            "target_path": ["code"],
        },
    )
    annotations.write_text(yaml.safe_dump(data))
    client = web.create_app([rich_conn]).test_client()

    batch = html.unescape(client.get("/t/primary/seedbank.batch").data.decode())
    cultivar = html.unescape(client.get("/t/primary/seedbank.cultivar").data.decode())

    assert "via (column) -> code" in batch
    assert "via (column) -> code" in cultivar


class TestOnlyALoopbackHostIsAnswered:
    """A page that rebinds its own name to loopback reaches no page of the site."""

    @pytest.mark.parametrize("path", ["/", "/t/primary/seedbank.batch"])
    def test_a_foreign_host_gets_400(self, rich_conn: ConnectionConfig, path: str) -> None:
        client = web.create_app([rich_conn]).test_client()

        assert client.get(path, headers={"Host": "rebound.example:8765"}).status_code == 400

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1:8765", "localhost:8765", "LOCALHOST:8765", "[::1]:8765", "localhost"],
    )
    def test_a_loopback_host_is_served(self, rich_conn: ConnectionConfig, host: str) -> None:
        client = web.create_app([rich_conn]).test_client()

        assert client.get("/", headers={"Host": host}).status_code == 200


class _Anchors(HTMLParser):
    """Each `a` with its `href` and text, the deepest `a` nesting seen, and every `pre`'s text."""

    def __init__(self, page: str) -> None:
        super().__init__()
        self.links: list[tuple[str | None, str]] = []
        self.sources: list[str | None] = []
        self.blocks: list[str] = []
        self.deepest = 0
        self._open: list[list[str]] = []
        self._in_pre = False
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._open.append([dict(attrs).get("href") or "", ""])
            self.deepest = max(self.deepest, len(self._open))
        elif tag == "img":
            self.sources.append(dict(attrs).get("src"))
        elif tag == "pre":
            self._in_pre = True
            self.blocks.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._open:
            href, text = self._open.pop()
            self.links.append((href, text))
        elif tag == "pre":
            self._in_pre = False

    def handle_data(self, data: str) -> None:
        for anchor in self._open:
            anchor[1] += data

        if self._in_pre:
            self.blocks[-1] += data


class TestMentions:
    """A table mention links in the rendered prose, never inside Markdown's own structure."""

    MENTIONS: ClassVar[dict[str, str]] = {
        "bed": "/t/main/db.s.bed",
        "beds": "/t/main/db.s.bed",
        "garden": "/t/main/db.s.garden",
    }

    def _render(self, text: str) -> _Anchors:
        return _Anchors(str(_render_markdown(text, self.MENTIONS)))

    @pytest.mark.parametrize(
        ("text", "link"),
        [
            (
                "[the site](https://seedbank.example/beds)",
                ("https://seedbank.example/beds", "the site"),
            ),
            (
                "[the bed guide](https://seedbank.example/guide)",
                ("https://seedbank.example/guide", "the bed guide"),
            ),
            (
                "[registry][reg]\n\n[reg]: https://seedbank.example/bed-registry",
                ("https://seedbank.example/bed-registry", "registry"),
            ),
        ],
    )
    def test_a_link_keeps_its_target_and_text_whole(self, text: str, link: tuple[str, str]) -> None:
        rendered = self._render(text)

        assert rendered.links == [link]
        assert rendered.deepest == 1

    def test_an_autolink_opens_no_link_inside_itself(self) -> None:
        rendered = self._render("<https://seedbank.example/bed>")

        assert rendered.links == [("https://seedbank.example/bed", "https://seedbank.example/bed")]
        assert rendered.deepest == 1

    @pytest.mark.parametrize(
        "text",
        ["see https://seedbank.example/bed/map today", "write to bed@seedbank.example"],
    )
    def test_an_address_in_prose_stays_text(self, text: str) -> None:
        assert self._render(text).links == []

    def test_an_indented_code_block_stays_as_written(self) -> None:
        rendered = self._render("Query:\n\n    SELECT * FROM bed\n")

        assert rendered.blocks == ["SELECT * FROM bed\n"]
        assert rendered.links == []

    def test_an_image_keeps_its_source(self) -> None:
        rendered = self._render("![bed photo](https://seedbank.example/bed.png)")

        assert rendered.sources == ["https://seedbank.example/bed.png"]

    def test_a_prose_mention_and_its_plural_link_to_the_table(self) -> None:
        rendered = self._render("every garden holds one bed, and beds are numbered")

        assert rendered.links == [
            ("/t/main/db.s.garden", "garden"),
            ("/t/main/db.s.bed", "bed"),
            ("/t/main/db.s.bed", "beds"),
        ]

    def test_only_a_code_span_naming_the_table_exactly_is_linked(self) -> None:
        rendered = self._render("use `bed`, not `bed_size`")

        assert rendered.links == [("/t/main/db.s.bed", "bed")]

    def test_no_map_links_nothing(self) -> None:
        assert _Anchors(str(_render_markdown("one bed"))).links == []

    def test_a_table_page_keeps_a_link_whose_address_names_a_table(
        self,
        rich_conn: ConnectionConfig,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "prints/primary/seedbank/batch/description.md").write_text(
            "Sown per [the cultivar register](https://seedbank.example/cultivar-register); "
            "see `cultivar` for species.\n",
        )
        page = web.create_app([rich_conn]).test_client().get("/t/primary/seedbank.batch")
        links = _Anchors(page.get_data(as_text=True)).links

        assert ("https://seedbank.example/cultivar-register", "the cultivar register") in links
        assert ("/t/primary/seedbank.cultivar", "cultivar") in links
