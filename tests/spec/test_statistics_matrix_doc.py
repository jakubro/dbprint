"""docs/reference/statistics-matrix.md must agree with `spec/statistics_matrix.py` cell for
cell - a comparison against the module, never a restatement that can silently drift.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from dbprint.spec.statistics_matrix import FORBIDDEN_FIELDS, REQUIRED_FIELDS


def _load_generator():
    """Import scripts/gen_statistics_matrix.py so the test shares the generator's render path."""

    path = Path(__file__).resolve().parents[2] / "scripts" / "gen_statistics_matrix.py"
    spec = importlib.util.spec_from_file_location("gen_statistics_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


gen = _load_generator()

_ROW = re.compile(r"^\| `(\w+)` \| (.+) \| (.+) \|$", re.MULTILINE)


def _page_rows(text: str) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Per classification: (required-beyond-base, forbidden), parsed off the backticked cells."""

    return {
        classification: (_cell_fields(required), _cell_fields(forbidden))
        for classification, required, forbidden in _ROW.findall(text)
    }


def _cell_fields(cell: str) -> frozenset[str]:
    return frozenset(re.findall(r"`(\w+)`", cell))


def _reconstructed_required(classification: str, cell_fields: frozenset[str]) -> frozenset[str]:
    """Undo the generator's own base-8 compression, mirroring `gen._row`'s branch."""

    if gen._BASE_FIELDS <= REQUIRED_FIELDS[classification]:
        return cell_fields | gen._BASE_FIELDS

    return cell_fields


def test_the_page_still_parses() -> None:
    """A row regex that matched nothing would pass every comparison below forever."""

    rows = _page_rows(gen.DOCS_PATH.read_text())

    assert len(rows) == len(gen._CLASSIFICATIONS)
    assert "boolean" in rows


def test_committed_page_matches_a_fresh_render() -> None:
    committed = gen.DOCS_PATH.read_text()

    assert committed == gen.build_document(), (
        "docs/reference/statistics-matrix.md is out of date with spec/statistics_matrix.py. "
        "Run `just docs` and commit the result."
    )


class TestModuleAgreement:
    def test_required_fields_agree_cell_for_cell(self) -> None:
        """`unsupported`'s cell already carries its full set (SPEC 2.2.3: fewer than the base
        8), so only the other seven reconstruct by re-adding the base before comparing.
        """

        rows = _page_rows(gen.DOCS_PATH.read_text())
        mismatched = [
            classification
            for classification in gen._CLASSIFICATIONS
            if _reconstructed_required(classification, rows[classification][0])
            != REQUIRED_FIELDS[classification]
        ]

        assert not mismatched, f"required fields disagree with the module for: {mismatched}"

    def test_forbidden_fields_agree_cell_for_cell(self) -> None:
        rows = _page_rows(gen.DOCS_PATH.read_text())
        mismatched = [
            classification
            for classification in gen._CLASSIFICATIONS
            if rows[classification][1] != FORBIDDEN_FIELDS[classification]
        ]

        assert not mismatched, f"forbidden fields disagree with the module for: {mismatched}"

    def test_a_field_added_to_the_module_changes_the_page(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A required field the module gains and the page doesn't must fail, not pass quietly."""

        monkeypatch.setitem(
            REQUIRED_FIELDS,
            "boolean",
            REQUIRED_FIELDS["boolean"] | frozenset({"drift_marker_xyz"}),
        )
        rendered = gen.build_document()

        assert "drift_marker_xyz" in rendered
        assert rendered != gen.DOCS_PATH.read_text()
