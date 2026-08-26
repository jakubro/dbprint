"""The reading guide against the shared claims register, anchored on SPEC.

The guide is generic prose, not a per-print rendering, so a claim checks the guide TEACHES
the register's obligation rather than restating a print literal. The guide is built entirely
from literals in `scripts/gen_reading_guide.py`, so every claim instead anchors on a SPEC
section and a field name SPEC uses there: renaming the field, moving the section, or dropping
the teaching each fail this module.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "docs" / "format" / "v1" / "SPEC.md"

_HEADING = re.compile(r"^#{2,4} (\d+(?:\.\d+)*)", re.MULTILINE)


def _load_generator():
    """Import scripts/gen_reading_guide.py, matching tests/engine/test_reading_guide.py."""

    path = REPO_ROOT / "scripts" / "gen_reading_guide.py"
    spec = importlib.util.spec_from_file_location("gen_reading_guide", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


gen = _load_generator()

COVERS = frozenset(
    {
        "scoped_table",
        "redacted_column",
        "future_dated_temporal",
        "truncated_fk_values",
        "unevaluated_diff_table",
        "empty_columns_map",
        "approximate_row_count",
        "incomplete_grain_search",
        "catalog_only_table",
        "declared_missing_artifact",
    },
)

# register key -> (SPEC section that defines the state, a field name SPEC uses there).
# The field is the anchor: neither SPEC nor the guide can drop it without failing here.
ANCHORS: dict[str, tuple[str, str]] = {
    "scoped_table": ("2.2.8", "rows_scanned"),
    "redacted_column": ("2.2.9", "redacted"),
    "future_dated_temporal": ("2.2.4", "freshness"),
    "truncated_fk_values": ("2.2.3", "values_coverage"),
    "unevaluated_diff_table": ("2.6.4", "unevaluated_tables"),
    "empty_columns_map": ("2.2.15", "catalog_only"),
    "approximate_row_count": ("2.2.1", "row_count_method"),
    "incomplete_grain_search": ("2.2.12", "exhausted"),
    # Shares empty_columns_map's anchor: both obligations rest on the same guide sentence -
    # a catalog_only object's absent fields read as unasked, never as a measured emptiness.
    "catalog_only_table": ("2.2.15", "catalog_only"),
    "declared_missing_artifact": ("7.3", "artifacts"),
}


def _guide() -> str:
    """The guide's body with whitespace collapsed, so a claim survives a line rewrap."""

    return " ".join(gen.build_document().split())


def _spec_section(number: str) -> str:
    """The body of one SPEC section, up to the next heading of any depth."""

    spec = SPEC_PATH.read_text()
    starts = [(m.start(), m.group(1)) for m in _HEADING.finditer(spec)]
    found = next((i for i, (_, num) in enumerate(starts) if num == number), None)

    assert found is not None, f"SPEC has no section {number}"

    end = starts[found + 1][0] if found + 1 < len(starts) else len(spec)

    return spec[starts[found][0] : end]


@pytest.mark.parametrize(("key", "anchor"), sorted(ANCHORS.items()))
def test_spec_defines_the_field_this_claim_anchors_on(key: str, anchor: tuple[str, str]) -> None:
    """The anchor is SPEC's, not this module's - an invented field name fails here first."""

    del key
    section, field = anchor

    assert field in _spec_section(section)


@pytest.mark.parametrize(("key", "anchor"), sorted(ANCHORS.items()))
def test_the_guide_teaches_the_field_spec_defines(key: str, anchor: tuple[str, str]) -> None:
    """Stripping the obligation from the guide fails the register entry that needs it."""

    del key
    _, field = anchor

    assert field in _guide()


@pytest.mark.parametrize(("key", "anchor"), sorted(ANCHORS.items()))
def test_the_guide_points_at_the_section_that_defines_it(key: str, anchor: tuple[str, str]) -> None:
    """A reader has to be able to reach the normative text, not just the guide's summary."""

    del key
    section, _ = anchor

    assert section in _guide()


def test_every_register_entry_has_an_anchor() -> None:
    """A state added to the register with no SPEC anchor fails here rather than passing quietly."""

    from tests.consumer import register

    assert {state.key for state in register.REGISTER} == set(ANCHORS)
