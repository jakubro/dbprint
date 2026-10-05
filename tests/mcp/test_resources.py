"""Resource URI parsing + enumeration + reading per MCP.md 3."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from dbprint.config import ConnectionConfig
from dbprint.mcp import McpError, ServedConnections, enumerate_for, list_page, parse_uri, read
from dbprint.mcp import resources as resources_module
from dbprint.mcp.resources import ReferenceRef, ResourceRef


class TestParseUri:
    def test_manifest(self) -> None:
        ref = parse_uri("dbprint://primary/manifest")
        assert ref == ResourceRef(connection="primary", kind="manifest", fqn=None)

    def test_diff(self) -> None:
        ref = parse_uri("dbprint://primary/diff")
        assert ref == ResourceRef(connection="primary", kind="diff", fqn=None)

    def test_reading(self) -> None:
        ref = parse_uri("dbprint://primary/reading")
        assert ref == ResourceRef(connection="primary", kind="reading", fqn=None)

    def test_manifest_annotations(self) -> None:
        ref = parse_uri("dbprint://primary/manifest_annotations")
        assert ref == ResourceRef(connection="primary", kind="manifest_annotations", fqn=None)

    def test_per_table_ddl(self) -> None:
        ref = parse_uri("dbprint://primary/public.curator/ddl")
        assert ref == ResourceRef(connection="primary", kind="ddl", fqn="public.curator")

    def test_per_table_statistics(self) -> None:
        ref = parse_uri("dbprint://primary/arboretum.seedbank.accession/statistics")
        assert isinstance(ref, ResourceRef)
        assert ref.kind == "statistics"
        assert ref.fqn == "arboretum.seedbank.accession"

    def test_per_table_description(self) -> None:
        ref = parse_uri("dbprint://primary/public.curator/description")
        assert isinstance(ref, ResourceRef)
        assert ref.kind == "description"

    def test_per_table_annotations(self) -> None:
        ref = parse_uri("dbprint://primary/public.curator/statistics_annotations")
        assert isinstance(ref, ResourceRef)
        assert ref.kind == "statistics_annotations"

    def test_per_table_relationship_annotations(self) -> None:
        ref = parse_uri("dbprint://primary/public.curator/relationships_annotations")
        assert isinstance(ref, ResourceRef)
        assert ref.kind == "relationships_annotations"

    def test_malformed_scheme(self) -> None:
        with pytest.raises(McpError):
            parse_uri("http://primary/manifest")

    def test_unknown_kind(self) -> None:
        with pytest.raises(McpError):
            parse_uri("dbprint://primary/public.curator/unknownkind")

    def test_empty_authority_reference(self) -> None:
        ref = parse_uri("dbprint:///reference/spec")
        assert ref == ReferenceRef(document="spec")

    def test_empty_authority_other_document(self) -> None:
        ref = parse_uri("dbprint:///reference/assertions")
        assert ref == ReferenceRef(document="assertions")

    def test_empty_authority_unknown_document_is_malformed(self) -> None:
        with pytest.raises(McpError):
            parse_uri("dbprint:///reference/readme")

    def test_empty_authority_not_reference_is_malformed(self) -> None:
        """A connection literally named `""` cannot smuggle a table read through here."""

        with pytest.raises(McpError):
            parse_uri("dbprint:///public.curator/ddl")

    def test_a_connection_literally_named_reference_is_unambiguous(self) -> None:
        """`dbprint://reference/manifest` has a non-empty authority - a real connection name."""

        ref = parse_uri("dbprint://reference/manifest")
        assert ref == ResourceRef(connection="reference", kind="manifest", fqn=None)


class TestEnumerate:
    def test_manifest_diff_and_reading_are_listed(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")
        entries = enumerate_for(state)
        uris = [e.uri for e in entries]
        assert "dbprint://production/manifest" in uris
        assert "dbprint://production/diff" in uris
        assert "dbprint://production/reading" in uris

    def test_diff_still_listed_when_never_computed(self, primary_conn: ConnectionConfig) -> None:
        """MCP.md 3.3: `diff` is producer-written and unconditional - a print with no diff.yaml
        still lists the URI, and `read()` surfaces the absence as `no_diff_available`.
        """

        diff_path = primary_conn.output / primary_conn.name / "diff.yaml"
        diff_path.unlink()
        state = ServedConnections(served={"production": primary_conn}, default="production")
        entries = enumerate_for(state)
        uris = {e.uri for e in entries}

        assert "dbprint://production/diff" in uris

        with pytest.raises(McpError) as excinfo:
            read(state, "dbprint://production/diff")

        assert excinfo.value.code == -32603
        assert "no diff available" in excinfo.value.detail

    def test_reading_still_listed_when_the_guide_is_absent(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """Same unconditional treatment as `diff` - `reading` is producer-written too."""

        from dbprint.engine.reading_guide import READING_GUIDE_FILENAME

        (primary_conn.output / primary_conn.name / READING_GUIDE_FILENAME).unlink()
        state = ServedConnections(served={"production": primary_conn}, default="production")
        entries = enumerate_for(state)
        uris = {e.uri for e in entries}

        assert "dbprint://production/reading" in uris

        with pytest.raises(McpError) as excinfo:
            read(state, "dbprint://production/reading")

        assert excinfo.value.code == -32603
        assert "no reading guide available" in excinfo.value.detail

    def test_three_connection_level_resources_survive_a_dry_run(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """MCP.md 3.3: exactly 3 unconditional resources per connection - a dry run writes
        neither `diff.yaml` nor the reading guide, and the listing must not shrink to 1.
        """

        from dbprint.engine.reading_guide import READING_GUIDE_FILENAME

        print_root = primary_conn.output / primary_conn.name
        (print_root / "diff.yaml").unlink()
        (print_root / READING_GUIDE_FILENAME).unlink()
        expected = {
            "dbprint://production/manifest",
            "dbprint://production/diff",
            "dbprint://production/reading",
        }
        state = ServedConnections(served={"production": primary_conn}, default="production")
        uris = {e.uri for e in enumerate_for(state)}

        assert uris & expected == expected

    def test_manifest_diff_and_reading_still_listed_when_never_generated(
        self,
        tmp_path: Path,
    ) -> None:
        """MCP.md 3.3 gates the connection-level resources on the connection being served, not on
        a print existing - a never-generated connection sits on the same continuum as a dry run.
        """

        empty_conn = ConnectionConfig(
            name="empty",
            adapter="postgres",
            output=tmp_path / "prints",
        )
        state = ServedConnections(served={"empty": empty_conn}, default="empty")

        entries = enumerate_for(state)
        connection_entries = {e.uri for e in entries if e.uri.startswith("dbprint://empty/")}

        assert connection_entries == {
            "dbprint://empty/manifest",
            "dbprint://empty/diff",
            "dbprint://empty/reading",
        }

        for uri in connection_entries:
            with pytest.raises(McpError):
                read(state, uri)

    def test_per_table_entries_included(self, primary_conn: ConnectionConfig) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")
        entries = enumerate_for(state)
        uris = {e.uri for e in entries}
        assert "dbprint://production/arboretum.seedbank.collector/ddl" in uris
        assert "dbprint://production/arboretum.seedbank.collector/statistics" in uris
        assert "dbprint://production/arboretum.seedbank.collector/relationships" in uris

    def test_description_only_when_authored(self, primary_conn: ConnectionConfig) -> None:
        """arboretum.seedbank.vault ships with no description.md - a real table not yet authored."""

        state = ServedConnections(served={"production": primary_conn}, default="production")
        entries = enumerate_for(state)
        uris = {e.uri for e in entries}
        assert "dbprint://production/arboretum.seedbank.vault/description" not in uris

        print_root = primary_conn.output / primary_conn.name
        desc = print_root / "arboretum" / "seedbank" / "vault" / "description.md"
        desc.write_text("Cold-storage vaults where accessions are shelved.\n")
        import yaml as _yaml

        manifest_path = print_root / "manifest.yaml"
        manifest = _yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.vault"]["artifacts"]["description"] = (
            "description.md"
        )
        manifest_path.write_text(_yaml.safe_dump(manifest))

        entries = enumerate_for(state)
        uris = {e.uri for e in entries}
        assert "dbprint://production/arboretum.seedbank.vault/description" in uris

    def test_annotations_only_when_authored(self, primary_conn: ConnectionConfig) -> None:
        """arboretum.seedbank.collector ships with no statistics.annotations.yaml for real."""

        state = ServedConnections(served={"production": primary_conn}, default="production")
        entries = enumerate_for(state)
        uris = {e.uri for e in entries}
        assert (
            "dbprint://production/arboretum.seedbank.collector/statistics_annotations" not in uris
        )

        print_root = primary_conn.output / primary_conn.name
        ann = print_root / "arboretum" / "seedbank" / "collector" / "statistics.annotations.yaml"
        ann.write_text("format_version: 1\ncolumns: {email: note}\n")
        import yaml as _yaml

        manifest_path = print_root / "manifest.yaml"
        manifest = _yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.collector"]["artifacts"][
            "statistics_annotations"
        ] = "statistics.annotations.yaml"
        manifest_path.write_text(_yaml.safe_dump(manifest))

        entries = enumerate_for(state)
        uris = {e.uri for e in entries}
        assert "dbprint://production/arboretum.seedbank.collector/statistics_annotations" in uris

    def test_relationship_annotations_only_when_authored(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """arboretum.seedbank.collector ships with no relationships.annotations.yaml for real."""

        state = ServedConnections(served={"production": primary_conn}, default="production")
        entries = enumerate_for(state)
        uris = {e.uri for e in entries}
        assert (
            "dbprint://production/arboretum.seedbank.collector/relationships_annotations"
            not in uris
        )

        print_root = primary_conn.output / primary_conn.name
        ann = print_root / "arboretum" / "seedbank" / "collector" / "relationships.annotations.yaml"
        ann.write_text("format_version: 1\nrefers_to: []\n")
        import yaml as _yaml

        manifest_path = print_root / "manifest.yaml"
        manifest = _yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.collector"]["artifacts"][
            "relationships_annotations"
        ] = "relationships.annotations.yaml"
        manifest_path.write_text(_yaml.safe_dump(manifest))

        entries = enumerate_for(state)
        uris = {e.uri for e in entries}
        assert "dbprint://production/arboretum.seedbank.collector/relationships_annotations" in uris

    def test_manifest_annotations_only_when_authored(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """The shipped manifest.annotations.yaml is removed first, to start from unauthored."""

        print_root = primary_conn.output / primary_conn.name
        ann = print_root / "manifest.annotations.yaml"
        ann.unlink()
        state = ServedConnections(served={"production": primary_conn}, default="production")
        entries = enumerate_for(state)
        uris = {e.uri for e in entries}
        assert "dbprint://production/manifest_annotations" not in uris

        ann.write_text("format_version: 1\nnotes: warehouse-wide fact\n")

        entries = enumerate_for(state)
        uris = {e.uri for e in entries}
        assert "dbprint://production/manifest_annotations" in uris

    def test_the_two_reference_documents_are_listed_exactly_once_each(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")
        entries = enumerate_for(state)
        uris = [e.uri for e in entries]

        assert uris.count("dbprint:///reference/spec") == 1
        assert uris.count("dbprint:///reference/assertions") == 1

    def test_the_two_reference_documents_lead_the_enumeration(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")
        uris = [e.uri for e in enumerate_for(state)]

        assert uris[0] == "dbprint:///reference/spec"
        assert uris[1] == "dbprint:///reference/assertions"

    def test_reference_documents_are_listed_even_with_no_served_connection(self) -> None:
        state = ServedConnections(served={}, default=None)
        uris = {e.uri for e in enumerate_for(state)}

        assert uris == {"dbprint:///reference/spec", "dbprint:///reference/assertions"}


class TestRead:
    def test_read_manifest(self, primary_conn: ConnectionConfig) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")
        result = read(state, "dbprint://production/manifest")
        assert result.mime_type == "application/yaml"
        assert "format_version" in result.content

    def test_read_ddl_returns_sql_mime(self, primary_conn: ConnectionConfig) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")
        result = read(state, "dbprint://production/arboretum.seedbank.collector/ddl")
        assert result.mime_type == "application/sql"
        assert "CREATE TABLE" in result.content

    def test_read_reading_guide_returns_markdown_mime(self, primary_conn: ConnectionConfig) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")
        result = read(state, "dbprint://production/reading")
        assert result.mime_type == "text/markdown"
        assert "Reading a dbprint print" in result.content

    def test_read_unknown_connection_raises(self, primary_conn: ConnectionConfig) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError):
            read(state, "dbprint://nonexistent/manifest")

    def test_read_unknown_table_raises(self, primary_conn: ConnectionConfig) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError):
            read(state, "dbprint://production/public.missing/ddl")

    def test_declared_but_missing_is_distinguishable_from_never_declared(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """A broken promise and a kind the manifest never made are different failures."""

        print_root = primary_conn.output / primary_conn.name
        (print_root / "arboretum" / "seedbank" / "collector" / "statistics.yaml").unlink()

        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        del manifest["tables"]["arboretum.fixture.shape_probe"]["artifacts"]["relationships"]
        manifest_path.write_text(yaml.safe_dump(manifest))

        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError) as missing:
            read(state, "dbprint://production/arboretum.seedbank.collector/statistics")

        with pytest.raises(McpError) as undeclared:
            read(state, "dbprint://production/arboretum.fixture.shape_probe/relationships")

        assert missing.value.code == -32603
        assert "file is absent" in missing.value.detail
        assert undeclared.value.code == -32602
        assert "is not declared" in undeclared.value.detail

    def test_read_missing_description_raises(self, primary_conn: ConnectionConfig) -> None:
        """arboretum.seedbank.vault ships with no description.md for real."""

        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError):
            read(state, "dbprint://production/arboretum.seedbank.vault/description")

    def test_read_missing_annotations_raises(self, primary_conn: ConnectionConfig) -> None:
        """arboretum.seedbank.collector ships with no statistics.annotations.yaml for real."""

        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError):
            read(state, "dbprint://production/arboretum.seedbank.collector/statistics_annotations")

    def test_declared_but_missing_optional_kind_is_distinguishable_from_never_declared(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """The same distinction the required kinds get, extended to an optional one."""

        print_root = primary_conn.output / primary_conn.name
        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.vault"]["artifacts"]["description"] = (
            "description.md"
        )
        manifest_path.write_text(yaml.safe_dump(manifest))

        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError) as missing:
            read(state, "dbprint://production/arboretum.seedbank.vault/description")

        with pytest.raises(McpError) as undeclared:
            read(state, "dbprint://production/arboretum.seedbank.collector/statistics_annotations")

        assert missing.value.code == -32603
        assert "file is absent" in missing.value.detail
        assert undeclared.value.code == -32602
        assert "is optional and not authored" in undeclared.value.detail

    def test_read_missing_manifest_annotations_raises(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """The shipped manifest.annotations.yaml is removed first, to start from unauthored."""

        (primary_conn.output / primary_conn.name / "manifest.annotations.yaml").unlink()
        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError):
            read(state, "dbprint://production/manifest_annotations")

    def test_read_authored_manifest_annotations(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        ann = primary_conn.output / primary_conn.name / "manifest.annotations.yaml"
        ann.write_text("format_version: 1\nnotes: warehouse-wide fact\n")
        state = ServedConnections(served={"production": primary_conn}, default="production")

        result = read(state, "dbprint://production/manifest_annotations")

        assert result.mime_type == "application/yaml"
        assert "warehouse-wide fact" in result.content

    def test_declared_but_missing_manifest_annotations_is_distinguishable_from_never_declared(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """The connection-level kind gets the same distinction the per-table ones do."""

        print_root = primary_conn.output / primary_conn.name
        manifest_path = print_root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest.pop("manifest_annotations", None)
        manifest_path.write_text(yaml.safe_dump(manifest))
        (print_root / "manifest.annotations.yaml").unlink(missing_ok=True)

        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError) as undeclared:
            read(state, "dbprint://production/manifest_annotations")

        manifest["manifest_annotations"] = "manifest.annotations.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest))

        with pytest.raises(McpError) as missing:
            read(state, "dbprint://production/manifest_annotations")

        assert undeclared.value.code == -32602
        assert "is optional and not authored" in undeclared.value.detail
        assert missing.value.code == -32603
        assert "file is absent" in missing.value.detail

    def test_read_missing_relationship_annotations_raises(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        """arboretum.seedbank.collector ships with no relationships.annotations.yaml for real."""

        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError):
            read(
                state,
                "dbprint://production/arboretum.seedbank.collector/relationships_annotations",
            )

    def test_read_authored_relationship_annotations(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        import yaml as _yaml

        print_root = primary_conn.output / primary_conn.name
        ann = print_root / "arboretum" / "seedbank" / "collector" / "relationships.annotations.yaml"
        ann.write_text("format_version: 1\nrefers_to: []\n")
        manifest_path = print_root / "manifest.yaml"
        manifest = _yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.collector"]["artifacts"][
            "relationships_annotations"
        ] = "relationships.annotations.yaml"
        manifest_path.write_text(_yaml.safe_dump(manifest))
        state = ServedConnections(served={"production": primary_conn}, default="production")

        result = read(
            state,
            "dbprint://production/arboretum.seedbank.collector/relationships_annotations",
        )

        assert result.mime_type == "application/yaml"
        assert "refers_to: []" in result.content

    def test_read_authored_annotations(self, primary_conn: ConnectionConfig) -> None:
        import yaml as _yaml

        print_root = primary_conn.output / primary_conn.name
        ann = print_root / "arboretum" / "seedbank" / "collector" / "statistics.annotations.yaml"
        ann.write_text("format_version: 1\ncolumns: {email: note}\n")
        manifest_path = print_root / "manifest.yaml"
        manifest = _yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["arboretum.seedbank.collector"]["artifacts"][
            "statistics_annotations"
        ] = "statistics.annotations.yaml"
        manifest_path.write_text(_yaml.safe_dump(manifest))
        state = ServedConnections(served={"production": primary_conn}, default="production")

        result = read(
            state,
            "dbprint://production/arboretum.seedbank.collector/statistics_annotations",
        )

        assert result.mime_type == "application/yaml"
        assert "email: note" in result.content

    def test_read_is_fresh_on_every_call(self, primary_conn: ConnectionConfig) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")
        ddl_path = (
            primary_conn.output
            / primary_conn.name
            / "arboretum"
            / "seedbank"
            / "collector"
            / "ddl.sql"
        )
        first = read(state, "dbprint://production/arboretum.seedbank.collector/ddl")
        ddl_path.write_text("CREATE TABLE different (x int);\n")
        second = read(state, "dbprint://production/arboretum.seedbank.collector/ddl")
        assert first.content != second.content
        assert "different" in second.content

    def test_read_reference_document_needs_no_connection(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`_read` is monkeypatched past the editable-install packaging gap, so this proves
        `read()`'s dispatch rather than the packaging lookup."""

        from dbprint.mcp import reference as reference_module

        monkeypatch.setattr(reference_module, "_read", lambda document: f"# {document} body\n")

        state = ServedConnections(served={}, default=None)
        result = read(state, "dbprint:///reference/spec")

        assert result.mime_type == "text/markdown"
        assert result.content == "# spec body\n"


class TestAWronglyShapedManifestIsAnErrorNotACrash:
    """A manifest that parses but no reader can walk must error (MCP.md 8), not crash."""

    @pytest.mark.parametrize(
        "body",
        [
            "- one\n- two\n",
            "just a string\n",
            "format_version: 1\ntables:\n  - public.curator\n",
            "format_version: 1\ntables:\n",
        ],
    )
    def test_reading_a_table_resource_raises_a_protocol_error(
        self,
        primary_conn: ConnectionConfig,
        body: str,
    ) -> None:
        (primary_conn.output / primary_conn.name / "manifest.yaml").write_text(body)
        state = ServedConnections({"production": primary_conn}, default="production")

        with pytest.raises(McpError):
            read(state, "dbprint://production/arboretum.seedbank.collector/ddl")

    def test_enumeration_raises_a_protocol_error(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        (primary_conn.output / primary_conn.name / "manifest.yaml").write_text("- one\n- two\n")
        state = ServedConnections({"production": primary_conn}, default="production")

        with pytest.raises(McpError):
            enumerate_for(state)

    def test_an_entry_that_is_not_a_mapping_is_skipped_not_fatal(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        import yaml

        manifest_path = primary_conn.output / primary_conn.name / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["seedbank.herbarium"] = "not an entry"
        manifest_path.write_text(yaml.safe_dump(manifest))
        state = ServedConnections({"production": primary_conn}, default="production")
        uris = [r.uri for r in enumerate_for(state)]

        assert any("arboretum.seedbank.collector" in uri for uri in uris)
        assert not any("seedbank.herbarium" in uri for uri in uris)


def test_every_per_table_resource_names_the_tool_that_reads_it_interpreted(
    primary_conn: ConnectionConfig,
) -> None:
    """A raw file is listed beside the tool an agent should call instead."""

    state = ServedConnections(served={primary_conn.name: primary_conn}, default=primary_conn.name)
    prefix = f"dbprint://{primary_conn.name}/"
    per_table = [
        e for e in enumerate_for(state) if e.uri.startswith(prefix) and e.uri.count("/") == 4
    ]

    assert per_table
    assert all("get_table_context" in e.description for e in per_table), [
        e.uri for e in per_table if "get_table_context" not in e.description
    ]


class TestPagedReads:
    """MCP.md 3.4: a resource larger than a page is read page by page through its URI."""

    @staticmethod
    def _wide_statistics(conn: ConnectionConfig) -> Path:
        path = conn.output / conn.name / "arboretum" / "seedbank" / "taxon" / "statistics.yaml"
        path.write_text(path.read_text() + "".join(f"# field note {i}\n" for i in range(3_000)))

        return path

    def test_a_small_file_is_one_page(self, primary_conn: ConnectionConfig) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")
        result = read(state, "dbprint://production/arboretum.seedbank.vault/ddl")

        assert (result.page, result.pages) == (1, 1)
        assert result.version

    def test_every_page_fits_and_the_pages_rebuild_the_file_exactly(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        path = self._wide_statistics(primary_conn)
        state = ServedConnections(served={"production": primary_conn}, default="production")
        uri = "dbprint://production/arboretum.seedbank.taxon/statistics"
        first = read(state, uri)
        rest = [
            read(state, f"{uri}?page={n}&version={first.version}")
            for n in range(2, first.pages + 1)
        ]

        assert first.pages > 1
        assert all(len(page.content) <= 20_000 for page in [first, *rest])
        assert "".join(page.content for page in [first, *rest]) == path.read_text()

    def test_a_line_longer_than_a_page_splits_and_still_rebuilds(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        path = (
            primary_conn.output / primary_conn.name / "arboretum" / "seedbank" / "vault" / "ddl.sql"
        )
        path.write_text(path.read_text() + "-- " + "x" * 45_000 + "\n")
        state = ServedConnections(served={"production": primary_conn}, default="production")
        uri = "dbprint://production/arboretum.seedbank.vault/ddl"
        first = read(state, uri)
        pages = [first] + [
            read(state, f"{uri}?page={n}&version={first.version}")
            for n in range(2, first.pages + 1)
        ]

        assert first.pages >= 3
        assert "".join(page.content for page in pages) == path.read_text()

    def test_every_page_fits_once_escaped_for_the_wire(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        path = (
            primary_conn.output / primary_conn.name / "arboretum" / "seedbank" / "vault" / "ddl.sql"
        )
        path.write_text(path.read_text() + "".join(f'-- "{i}" \\ "quoted"\n' for i in range(3_000)))
        state = ServedConnections(served={"production": primary_conn}, default="production")
        uri = "dbprint://production/arboretum.seedbank.vault/ddl"
        first = read(state, uri)
        pages = [first] + [
            read(state, f"{uri}?page={n}&version={first.version}")
            for n in range(2, first.pages + 1)
        ]

        assert all(len(json.dumps(page.content)) - 2 <= 20_000 for page in pages)
        assert "".join(page.content for page in pages) == path.read_text()

    def test_a_file_rewritten_during_the_read_is_read_again(
        self,
        primary_conn: ConnectionConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        digests = iter(["before", "during", "after", "after"])
        monkeypatch.setattr(resources_module.paging, "files_digest", lambda _paths: next(digests))
        state = ServedConnections(served={"production": primary_conn}, default="production")

        assert read(state, "dbprint://production/arboretum.seedbank.vault/ddl").version == "after"

    def test_a_file_that_keeps_changing_is_refused(
        self,
        primary_conn: ConnectionConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        digests = iter(["a", "b", "c", "d"])
        monkeypatch.setattr(resources_module.paging, "files_digest", lambda _paths: next(digests))
        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError) as caught:
            read(state, "dbprint://production/arboretum.seedbank.vault/ddl")

        assert "changed" in caught.value.detail

    @pytest.mark.parametrize("page", ["0", "99", "x", "\u00b2", "\u0663"])
    def test_a_page_outside_the_range_names_the_range(
        self,
        primary_conn: ConnectionConfig,
        page: str,
    ) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError) as caught:
            read(state, f"dbprint://production/arboretum.seedbank.vault/ddl?page={page}")

        assert caught.value.code == -32602
        assert "pages run 1 to 1" in caught.value.detail

    def test_a_version_the_file_has_moved_past_is_refused(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        path = self._wide_statistics(primary_conn)
        state = ServedConnections(served={"production": primary_conn}, default="production")
        uri = "dbprint://production/arboretum.seedbank.taxon/statistics"
        version = read(state, uri).version
        path.write_text(path.read_text() + "# one more\n")

        with pytest.raises(McpError) as caught:
            read(state, f"{uri}?page=2&version={version}")

        assert caught.value.code == -32602
        assert "changed" in caught.value.detail

    def test_a_later_page_without_a_version_is_refused(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_statistics(primary_conn)
        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError) as caught:
            read(state, "dbprint://production/arboretum.seedbank.taxon/statistics?page=2")

        assert "version" in caught.value.detail

    def test_an_unknown_query_key_is_a_malformed_uri(self, primary_conn: ConnectionConfig) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")

        with pytest.raises(McpError) as caught:
            read(state, "dbprint://production/arboretum.seedbank.vault/ddl?offset=2")

        assert "does not match" in caught.value.detail


class TestPagedEnumeration:
    """MCP.md 3.3: `resources/list` pages under the bound, in its own order."""

    @staticmethod
    def _wide_manifest(conn: ConnectionConfig, tables: int) -> None:
        path = conn.output / conn.name / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        entry = next(iter(manifest["tables"].values()))
        manifest["tables"] = {f"seedbank.t{i:04d}": dict(entry) for i in range(tables)}
        path.write_text(yaml.safe_dump(manifest))

    def test_pages_walk_the_whole_enumeration_in_order(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 200)
        state = ServedConnections(served={"production": primary_conn}, default="production")
        pages = [list_page(state, None)]

        while pages[-1].next_cursor is not None:
            pages.append(list_page(state, pages[-1].next_cursor))

        assert len(pages) > 1
        assert [e.uri for p in pages for e in p.entries] == [e.uri for e in enumerate_for(state)]

    def test_a_small_enumeration_is_one_page(self, primary_conn: ConnectionConfig) -> None:
        state = ServedConnections(served={"production": primary_conn}, default="production")
        listed = list_page(state, None)

        assert listed.next_cursor is None
        assert [e.uri for e in listed.entries] == [e.uri for e in enumerate_for(state)]

    def test_a_cursor_over_a_rewritten_manifest_is_refused(
        self,
        primary_conn: ConnectionConfig,
    ) -> None:
        self._wide_manifest(primary_conn, 200)
        state = ServedConnections(served={"production": primary_conn}, default="production")
        cursor = list_page(state, None).next_cursor
        self._wide_manifest(primary_conn, 201)

        with pytest.raises(McpError) as caught:
            list_page(state, cursor)

        assert "changed" in caught.value.detail
