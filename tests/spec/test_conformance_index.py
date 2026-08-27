"""docs/reference/conformance.md must carry every code the validator can emit, at its severity.

Checked against a fresh render and against the validator's own `Issue(...)` call sites.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from tests.spec._issue_codes import SEVERITY_MAP, emitted_codes


def _load_generator():
    """Import scripts/gen_conformance_index.py so the test shares the generator's render path."""

    path = Path(__file__).resolve().parents[2] / "scripts" / "gen_conformance_index.py"
    spec = importlib.util.spec_from_file_location("gen_conformance_index", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


gen = _load_generator()

_ROW = re.compile(r"^\| `([^`]+)` \| (error|warning) \|", re.MULTILINE)


def _page_codes(text: str) -> dict[str, str]:
    """Every `code: severity` pair rendered on the index, in SPEC 6.3's severity spelling."""

    return {code: SEVERITY_MAP[severity] for code, severity in _ROW.findall(text)}


def test_the_page_still_parses() -> None:
    """A row regex that matched nothing would pass every comparison below forever."""

    page = _page_codes(gen.DOCS_PATH.read_text())

    assert len(page) >= 100
    assert page["layout.missing-manifest"] == "E"


class TestGoldenReference:
    def test_committed_index_matches_a_fresh_render(self) -> None:
        committed = gen.DOCS_PATH.read_text()
        assert committed == gen.build_document(), (
            "docs/reference/conformance.md is out of date with SPEC 6.3. "
            "Run `just docs` and commit the result."
        )

    def test_golden_check_detects_a_new_catalog_row(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A code added to the specification must change the rendered page."""

        mutated = gen.SPEC_PATH.read_text().replace(
            "| `ddl.empty-file` | E |",
            "| `ddl.drift-marker-xyz` | E | Planted |\n| `ddl.empty-file` | E |",
            1,
        )
        spec_copy = tmp_path / "SPEC.md"
        spec_copy.write_text(mutated)
        monkeypatch.setattr(gen, "SPEC_PATH", spec_copy)

        rendered = gen.build_document()

        assert "ddl.drift-marker-xyz" in _page_codes(rendered)
        assert rendered != gen.DOCS_PATH.read_text()


class TestValidatorAgreement:
    def test_every_emitted_code_is_on_the_page(self) -> None:
        missing = sorted(set(emitted_codes()) - set(_page_codes(gen.DOCS_PATH.read_text())))

        assert not missing, f"emitted by the validator and absent from the index: {missing}"

    def test_every_listed_code_is_emitted(self) -> None:
        unused = sorted(set(_page_codes(gen.DOCS_PATH.read_text())) - set(emitted_codes()))

        assert not unused, f"listed on the index but never emitted: {unused}"

    def test_severities_agree(self) -> None:
        emitted = emitted_codes()
        page = _page_codes(gen.DOCS_PATH.read_text())
        mismatched = sorted(code for code in page if page[code] != emitted[code])

        assert not mismatched, f"the index severity disagrees with the code for: {mismatched}"


class TestStatedTotals:
    def test_the_counts_in_the_prose_match_the_rows(self) -> None:
        text = gen.DOCS_PATH.read_text()
        page = _page_codes(text)
        errors = sum(1 for severity in page.values() if severity == "E")

        assert f"{len(page)} codes: {errors} error, {len(page) - errors} warning." in text


class TestSpecLinks:
    def test_every_row_links_into_the_specification(self) -> None:
        rows = [line for line in gen.DOCS_PATH.read_text().splitlines() if _ROW.match(line)]

        assert rows
        assert all("(../format/v1/SPEC.md#" in row for row in rows)
