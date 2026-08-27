"""The shipped consumer guide is generated, not hand-written (SPEC 1.2.1).

Golden-tests both shipped copies against a fresh run of the generator, since a hand-edited
copy drifts from SPEC invisibly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from dbprint.engine.reading_guide import READING_GUIDE_TEXT


REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE_PATH = REPO_ROOT / "src/dbprint/engine/reading_guide.md"
SKILL_PATH = REPO_ROOT / "docs/examples/skill/dbprint.md"

# SPEC 3.1's classification table, parsed rather than hardcoded, so a new classification
# there fails this file instead of shipping a guide that never mentions it.
_SPEC_PATH = REPO_ROOT / "docs/format/v1/SPEC.md"


def _load_generator():
    """Import scripts/gen_reading_guide.py so the test shares the generator's render path."""

    path = REPO_ROOT / "scripts" / "gen_reading_guide.py"
    spec = importlib.util.spec_from_file_location("gen_reading_guide", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


gen = _load_generator()


def _spec_classifications() -> list[str]:
    section = gen._section(_SPEC_PATH.read_text(), "### 3.1 Defined classifications", "### 3.2")
    rows = gen._table_rows(section)[1:]  # drop the header row

    return [gen._BACKTICKED.search(cells[0]).group(1) for cells in rows]  # type: ignore[union-attr]


def test_the_shipped_package_copy_matches_the_generator() -> None:
    assert GUIDE_PATH.read_text() == gen.build_document()


def test_the_shipped_skill_matches_the_generator() -> None:
    assert SKILL_PATH.read_text() == gen.build_skill_document()


def test_the_runtime_loader_matches_the_shipped_copy() -> None:
    """`READING_GUIDE_TEXT` ships via importlib.resources - confirm it reads the same bytes."""

    assert READING_GUIDE_TEXT == GUIDE_PATH.read_text()


def test_every_spec_classification_gets_a_vocabulary_sentence() -> None:
    text = gen.build_document()

    for name in _spec_classifications():
        assert f"`{name}`" in text, f"{name!r} has no vocabulary sentence"


def test_the_sketch_signal_names_the_decoder_and_carries_no_percentage() -> None:
    text = gen.build_document()
    signals = text.split("## Signals nobody points at")[1]

    assert "`sketch`" in text
    assert "`dbprint.spec.sketch`" in text
    assert "%" not in signals
    assert "exhaustive" in signals
    assert "membership" in signals


def test_the_skill_is_a_layout_protocol_not_a_guide_copy() -> None:
    """SPEC 1.2/1.4: the skill says where a print's files live, the guide how to read them."""

    skill = gen.build_skill_document()

    assert "`manifest.yaml`" in skill
    assert "`tables`" in skill
    assert "`path`" in skill
    assert "`ddl.sql`" in skill
    assert "`statistics.yaml`" in skill
    assert "`prints/<connection_name>/reading.md`" in skill

    # None of the guide's own sections leaked back in - this is a protocol, not a copy.
    assert "## Vocabulary" not in skill
    assert "## Residual traps" not in skill
    assert "## Signals nobody points at" not in skill


def test_the_generator_raises_if_an_anchor_no_longer_holds() -> None:
    """A returning `build_document()` proves every anchored SPEC/adapter fact still holds."""

    broken_matrix = {
        cls: dict(fields)
        for cls, fields in gen._classification_matrix(_SPEC_PATH.read_text()).items()
    }
    broken_matrix["boolean"]["values"] = "-"

    with pytest.raises(AssertionError, match="boolean no longer requires values"):
        gen._check_vocabulary_anchors(broken_matrix)


def test_the_consumer_must_guard_passes_on_the_committed_spec() -> None:
    """A returning `build_document()` already proves this; asserted directly for its own sake."""

    gen._check_consumer_must_coverage(_SPEC_PATH.read_text())


def test_the_consumer_must_guard_fires_on_an_uncited_new_rule() -> None:
    """A consumer MUST that SPEC adds under an uncited, unexempted section fails generation."""

    augmented = _SPEC_PATH.read_text().replace(
        "### 0.1 What this spec covers",
        "### 0.1 What this spec covers\n\n"
        "A consumer MUST do something this guide never mentions or exempts.\n",
    )

    with pytest.raises(AssertionError, match="0.1"):
        gen._check_consumer_must_coverage(augmented)


def test_the_scope_and_redaction_rules_are_present() -> None:
    text = gen.build_document()

    assert "`scope`" in text
    assert "`rows_scanned`" in text
    assert "`redacted`" in text
    assert "floored" in text
    assert "90 days" in text


def test_the_foreign_key_candidate_bullet_names_the_referencing_side() -> None:
    text = gen.build_document()

    assert "the referencing side" in text
    assert "join target" not in text


def test_the_diff_paragraph_is_executable_against_a_single_print() -> None:
    text = gen.build_document()

    assert "latest structured diff" in text
    assert "unevaluated_tables" in text
    assert "several diffs" not in text
    assert "as long as the connection has been diffed" not in text


def test_the_entry_point_names_both_starting_conditions() -> None:
    text = gen.build_document()

    assert "`manifest.yaml`" in text
    assert "`search_columns`" in text


def test_absence_is_pointed_at_spec_7() -> None:
    assert "SPEC 7 names what each absence can mean" in gen.build_document()
