"""The engine's and the validator's link walks are twins: one answer over the same tree."""

from __future__ import annotations

from pathlib import Path

from dbprint.conformance import layout
from dbprint.engine import baseline
from tests._links import ESCAPING, linked_tree


def test_both_walks_find_the_same_links(tmp_path: Path) -> None:
    root, _ = linked_tree(tmp_path)

    assert baseline.escaping_links(root) == layout.escaping_links(root) == ESCAPING


def test_both_resolve_the_paths_a_manifest_opens(tmp_path: Path) -> None:
    root, _ = linked_tree(tmp_path)
    manifest = {"tables": {"shared.loop": {"path": "shared/loop", "artifacts": {"ddl": "ddl.sql"}}}}

    assert (
        baseline.escaping_links(root, manifest)
        == layout.escaping_links(root, manifest)
        == ESCAPING | {"shared/loop/ddl.sql"}
    )
