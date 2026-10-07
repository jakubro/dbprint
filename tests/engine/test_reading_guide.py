"""The shipped consumer guide is generated, not hand-written (SPEC 1.2.1).

Golden-tests the shipped copy against a fresh run of the generator, since a hand-edited copy
drifts from SPEC invisibly. The example skill is hand-written and pinned separately, by
tests/test_skill_claims_agreement.py.
"""

from __future__ import annotations

import pytest

from tests._scripts import REPO_ROOT, load_script
from tests.spec._spec_markdown import section, table_rows


GUIDE_PATH = REPO_ROOT / "src/dbprint/engine/reading_guide.md"

# SPEC 3.1's classification table, parsed rather than hardcoded, so a new classification
# there fails this file instead of shipping a guide that never mentions it.
_SPEC_PATH = REPO_ROOT / "docs/format/v1/SPEC.md"


gen = load_script("gen_reading_guide")


def _spec_classifications() -> list[str]:
    rows = table_rows(section("### 3.1 Defined classifications", "### 3.2"))[1:]

    return [cells[0].strip("`") for cells in rows]


def test_the_shipped_package_copy_matches_the_generator() -> None:
    assert GUIDE_PATH.read_text() == gen.build_document()


def test_every_spec_classification_gets_a_vocabulary_sentence() -> None:
    text = gen.build_document()

    for name in _spec_classifications():
        assert f"`{name}`" in text, f"{name!r} has no vocabulary sentence"


def test_the_sketch_signal_names_the_decoder_and_carries_no_percentage() -> None:
    text = gen.build_document()
    signals = text.split("## The diff, reference lists and sketches")[1]

    assert "`sketch`" in text
    assert "`dbprint.spec.sketch`" in text
    assert "%" not in signals
    assert "exhaustive" in signals
    assert "membership" in signals


def test_the_generator_raises_if_an_anchor_no_longer_holds() -> None:
    """A returning `build_document()` proves every anchored SPEC/adapter fact still holds."""

    broken_matrix = {
        cls: dict(fields)
        for cls, fields in gen._classification_matrix(_SPEC_PATH.read_text()).items()
    }
    broken_matrix["boolean"]["values"] = "-"

    with pytest.raises(AssertionError, match="boolean no longer requires values"):
        gen._check_vocabulary_anchors(broken_matrix)


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


def test_a_complete_list_is_the_whole_column_only_over_the_rows_scanned() -> None:
    text = gen.build_document()

    assert "the whole column — over the rows scanned where the file carries `scope`" in text


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
    assert "An MCP client calls the server's tools instead" in text


def test_absence_is_pointed_at_spec_7() -> None:
    assert "SPEC 7 names what each absence can mean" in gen.build_document()


def test_every_detection_value_gets_named_in_the_guide() -> None:
    """`relationships.schema.json`'s `Detection` enum, not a hardcoded list - a fourth value
    added there fails this test instead of shipping a guide that still enumerates three.
    """

    text = gen.build_document()

    for value in gen._detection_values():
        assert f"`{value}`" in text, f"{value!r} has no vocabulary sentence"


def test_the_detection_guard_fires_on_an_unnamed_value() -> None:
    """A returning `build_document()` already proves this; asserted directly for its own sake."""

    sentence = next(s for name, s in gen._VOCABULARY if name == "foreign_key_candidate")

    with pytest.raises(AssertionError, match="guessed"):
        gen._check_detection_enumeration(["declared", "inferred", "measured", "guessed"], sentence)


def test_measured_edges_are_not_a_stronger_schema_claim() -> None:
    text = gen.build_document()

    assert "`measured`" in text
    assert "stronger claim about the schema" in text
    assert "stronger claim about the data" in text


def test_the_depends_on_two_encodings_trap_is_present() -> None:
    text = gen.build_document()

    assert "`depends_on: []`" in text
    assert "the key omitted entirely" in text


def test_the_timeline_gap_trap_is_present() -> None:
    text = gen.build_document()

    assert "timeline gap is not a zero" in text
    assert "`timeline.buckets`" in text


def test_the_scope_paragraph_names_sum_as_non_rescalable() -> None:
    text = gen.build_document()
    strategy = text.split("## Reading strategy")[1]

    assert "`sum` is not rescalable to table grain" in strategy
