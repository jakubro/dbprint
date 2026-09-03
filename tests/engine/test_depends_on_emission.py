"""What the engine writes for a view/matview's `depends_on` field (SPEC 2.2.17) - the catalog
read belongs to the adapters; these cover the threading, encoding and suppression after it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import ColumnMeta, CommentsMeta, MockAdapter, MockTable, TableType
from dbprint.config import ConnectionConfig
from dbprint.engine import Engine
from dbprint.engine.context_assembler import AssemblyOptions, assemble


def _table(namespace_path: tuple[str, str], ddl: str, table_type: TableType = "table") -> MockTable:
    return MockTable(
        type=table_type,
        namespace_path=namespace_path,
        ddl=ddl,
        columns=[
            ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={},
        samples={},
    )


def _fixture() -> dict[str, MockTable]:
    return {
        "public.wide": _table(("public", "wide"), "CREATE TABLE public.wide (id int);\n"),
        "public.narrow": _table(("public", "narrow"), "CREATE TABLE public.narrow (id int);\n"),
        "public.wide_v": _table(
            ("public", "wide_v"),
            "CREATE VIEW public.wide_v AS SELECT w.id FROM public.wide w, public.narrow n;\n",
            table_type="view",
        ),
        "public.wide_mv": _table(
            ("public", "wide_mv"),
            "CREATE MATERIALIZED VIEW public.wide_mv AS SELECT id FROM public.wide;\n",
            table_type="matview",
        ),
        "public.orphan_v": _table(
            ("public", "orphan_v"),
            "CREATE VIEW public.orphan_v AS SELECT 1 AS id;\n",
            table_type="view",
        ),
    }


def _generate(tmp_path: Path, dependencies: dict[str, tuple[str, ...]] | None) -> Path:
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
    )
    Engine(MockAdapter(_fixture(), dependencies=dependencies), conn, tmp_path).generate()

    return tmp_path / "w"


def _statistics(root: Path, schema: str, name: str) -> dict[str, Any]:
    return yaml.safe_load((root / schema / name / "statistics.yaml").read_text())


class TestPresence:
    def test_a_view_lists_every_object_it_reads(self, tmp_path: Path) -> None:
        root = _generate(
            tmp_path,
            dependencies={
                "public.wide_v": ("public.wide", "public.narrow"),
                "public.wide_mv": ("public.wide",),
            },
        )
        data = _statistics(root, "public", "wide_v")

        assert set(data["depends_on"]) == {"public.wide", "public.narrow"}

    def test_a_matview_carries_the_field_too(self, tmp_path: Path) -> None:
        """Unlike a plain view, a matview is never `catalog_only` - the field is unaffected."""

        root = _generate(tmp_path, dependencies={"public.wide_mv": ("public.wide",)})
        data = _statistics(root, "public", "wide_mv")

        assert data["depends_on"] == ["public.wide"]
        assert "catalog_only" not in data

    def test_a_view_reading_nothing_publishes_an_empty_list(self, tmp_path: Path) -> None:
        root = _generate(tmp_path, dependencies={"public.orphan_v": ()})
        data = _statistics(root, "public", "orphan_v")

        assert data["depends_on"] == []


class TestOmission:
    def test_a_view_absent_from_the_map_omits_the_key(self, tmp_path: Path) -> None:
        """Resolved-but-not-listed and never-asked read as the same bytes - both omit."""

        root = _generate(tmp_path, dependencies={"public.wide_mv": ("public.wide",)})
        data = _statistics(root, "public", "wide_v")

        assert "depends_on" not in data

    def test_a_none_map_omits_the_key_on_every_view(self, tmp_path: Path) -> None:
        """The whole connection could not ask - every view/matview omits, not just one."""

        root = _generate(tmp_path, dependencies=None)

        assert "depends_on" not in _statistics(root, "public", "wide_v")
        assert "depends_on" not in _statistics(root, "public", "wide_mv")


class TestNeverOnATable:
    def test_a_plain_table_never_carries_the_field_even_if_the_map_names_it(
        self,
        tmp_path: Path,
    ) -> None:
        root = _generate(tmp_path, dependencies={"public.wide": ("public.narrow",)})
        data = _statistics(root, "public", "wide")

        assert "depends_on" not in data


class TestContextRendering:
    def test_the_qualifier_line_states_what_the_view_reads(self, tmp_path: Path) -> None:
        root = _generate(tmp_path, dependencies={"public.wide_v": ("public.wide", "public.narrow")})
        manifest = yaml.safe_load((root / "manifest.yaml").read_text())

        text = assemble(
            manifest,
            root,
            ["public.wide_v"],
            AssemblyOptions(format="md", include_ddl=False),
        ).text

        assert "Depends on: public.wide, public.narrow" in text

    def test_an_omitted_key_renders_no_qualifier_line(self, tmp_path: Path) -> None:
        root = _generate(tmp_path, dependencies=None)
        manifest = yaml.safe_load((root / "manifest.yaml").read_text())

        text = assemble(
            manifest,
            root,
            ["public.wide_v"],
            AssemblyOptions(format="md", include_ddl=False),
        ).text

        assert "Depends on:" not in text


class TestStructuredFormats:
    def test_the_structured_yaml_carries_the_field_whole(self, tmp_path: Path) -> None:
        root = _generate(tmp_path, dependencies={"public.wide_v": ("public.wide",)})
        manifest = yaml.safe_load((root / "manifest.yaml").read_text())
        rendered = yaml.safe_load(
            assemble(
                manifest,
                root,
                ["public.wide_v"],
                AssemblyOptions(format="yaml", include_ddl=False),
            ).text,
        )

        assert rendered["statistics"]["depends_on"] == ["public.wide"]
