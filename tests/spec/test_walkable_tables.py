"""Which manifest entries a reader may follow, and which artifacts it may open from one."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from dbprint.engine import baseline
from dbprint.spec.artifacts import declared_artifacts, outside_print, table_path, walkable_tables


_ENTRY = {"path": "public/curator", "artifacts": {"ddl": "ddl.sql"}}


@pytest.mark.parametrize(
    ("manifest", "followed"),
    [
        pytest.param({"tables": {"public.curator": "not an entry"}}, [], id="entry-not-a-mapping"),
        pytest.param({"tables": {"public.curator": {"path": 5}}}, [], id="path-not-a-string"),
        pytest.param(
            {"tables": {"public.curator": _ENTRY, "public.specimen_loan": 5}},
            ["public.curator"],
            id="one-of-two-broken",
        ),
        pytest.param({"tables": None}, [], id="tables-empty"),
        pytest.param({}, [], id="no-tables-key"),
        pytest.param(None, [], id="no-manifest"),
        pytest.param(
            {"tables": {"public.curator": {"type": "table"}}},
            ["public.curator"],
            id="no-path",
        ),
    ],
)
def test_an_unusable_entry_drops_only_itself(manifest: Any, followed: list[str]) -> None:
    assert list(walkable_tables(manifest)) == followed


@pytest.mark.parametrize(
    ("entry", "opened"),
    [
        pytest.param({**_ENTRY, "artifacts": 5}, {}, id="artifacts-not-a-map"),
        pytest.param(
            {**_ENTRY, "artifacts": {"ddl": 7, "statistics": "statistics.yaml"}},
            {"statistics": "statistics.yaml"},
            id="one-name-not-a-string",
        ),
        pytest.param({"path": "public/curator"}, {}, id="no-artifacts"),
    ],
)
def test_a_name_that_is_not_a_string_is_never_opened(
    entry: dict[str, Any],
    opened: dict[str, str],
) -> None:
    assert declared_artifacts(entry) == opened


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(["one", "two"], id="sequence"),
        pytest.param("a string", id="scalar"),
        pytest.param({"tables": ["public.curator"]}, id="tables-is-a-sequence"),
    ],
)
def test_a_document_the_walkers_are_not_defined_for_is_refused_upstream(document: Any) -> None:
    assert baseline.manifest_shape_error(document) is not None


@pytest.mark.parametrize(
    ("key", "entry"),
    [
        pytest.param("public.curator", {"path": "../.."}, id="climbs"),
        pytest.param("public.curator", {"path": "/abs/curator"}, id="absolute"),
        pytest.param("public.curator", {"path": "public/../x"}, id="dotdot-segment"),
        pytest.param("public.curator", {"path": "a\\..\\b"}, id="backslash-dotdot"),
        pytest.param("public.curator", {"path": "C:/x"}, id="drive-letter"),
        pytest.param("/public/curator", {"type": "table"}, id="fqn-fallback-absolute"),
    ],
)
def test_an_entry_leading_out_of_the_root_is_not_followed(key: str, entry: dict) -> None:
    manifest = {"tables": {key: entry, "public.herbarium": {"path": "public/herbarium"}}}

    assert list(walkable_tables(manifest)) == ["public.herbarium"]
    assert set(outside_print(manifest)) == {key}


@pytest.mark.parametrize("path", ["public/curator", "public/..x"])
def test_a_relative_path_is_followed(path: str) -> None:
    assert list(walkable_tables({"tables": {"public.curator": {"path": path}}})) == [
        "public.curator",
    ]


class TestLinksOutOfTheRoot:
    ENTRY: ClassVar[dict[str, Any]] = {
        "path": "public/curator",
        "artifacts": {"ddl": "ddl.sql", "description": "description.md"},
    }
    MANIFEST: ClassVar[dict[str, Any]] = {"tables": {"public.curator": ENTRY}}

    @pytest.mark.parametrize(
        ("link", "cause"),
        [
            (
                "public/curator",
                "public/curator is a symlink to a directory outside the connection root",
            ),
            ("public", "public is a symlink to a directory outside the connection root"),
            (
                "public/curator/description.md",
                "public/curator/description.md is a symlink to a file outside the connection root",
            ),
        ],
    )
    def test_the_entry_is_refused_naming_the_link(self, link: str, cause: str) -> None:
        assert walkable_tables(self.MANIFEST, {link}) == {}
        assert outside_print(self.MANIFEST, {link}) == {"public.curator": cause}

    @pytest.mark.parametrize("path", ["public/./curator", "./public//curator"])
    def test_a_path_spelled_another_way_is_still_refused(self, path: str) -> None:
        manifest = {"tables": {"public.curator": {**self.ENTRY, "path": path}}}

        assert walkable_tables(manifest, {"public/curator"}) == {}

    def test_a_backslash_is_part_of_a_name_not_a_separator(self) -> None:
        manifest = {"tables": {"public.curator": {**self.ENTRY, "path": "public\\curator"}}}

        assert walkable_tables(manifest, {"public/curator"}) == manifest["tables"]
        assert walkable_tables(manifest, {"public\\curator"}) == {}

    def test_a_file_declared_at_the_root_itself_is_refused(self) -> None:
        manifest = {"tables": {"public.curator": {**self.ENTRY, "path": "."}}}

        assert outside_print(manifest, {"ddl.sql"}) == {
            "public.curator": "ddl.sql is a symlink to a file outside the connection root",
        }

    def test_an_entry_with_no_path_is_refused_for_a_root_file_linked_out(self) -> None:
        manifest = {"tables": {"public.curator": {"artifacts": {"statistics": "statistics.yaml"}}}}

        assert outside_print(manifest, {"statistics.yaml"}) == {
            "public.curator": "statistics.yaml is a symlink to a file outside the connection root",
        }

    def test_an_undeclared_file_linked_out_leaves_the_entry_followed(self) -> None:
        links = {"public/curator/statistics.yaml"}

        assert list(walkable_tables(self.MANIFEST, links)) == ["public.curator"]

    def test_the_cause_of_a_climbing_path(self) -> None:
        manifest = {"tables": {"public.curator": {"path": "../../.."}}}

        assert outside_print(manifest) == {
            "public.curator": "its path '../../..' leaves the connection root",
        }


@pytest.mark.parametrize(
    ("artifacts", "opened"),
    [
        pytest.param({"description": "../notes.md"}, {}, id="climbing-name"),
        pytest.param({"ddl": "other.sql"}, {}, id="another-name"),
        pytest.param(
            {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
            {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
            id="canonical-names",
        ),
    ],
)
def test_only_a_kinds_own_filename_is_opened(artifacts: dict, opened: dict) -> None:
    assert declared_artifacts({"path": "public/curator", "artifacts": artifacts}) == opened


def test_the_directory_of_an_entry_without_a_path_is_its_fqns() -> None:
    assert table_path("public.curator", {"type": "table"}) == "public/curator"
    assert table_path("public.curator", {"path": "public/curator_v2"}) == "public/curator_v2"
