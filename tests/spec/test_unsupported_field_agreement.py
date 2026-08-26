"""SPEC 3.3's `unsupported` field-set sentence must agree with the 2.2.3 matrix's row.

Both statements are hand-written prose read from SPEC.md itself, not a mirror - a guard built
any other way could drift the same way the sections it checks can.
"""

from __future__ import annotations

import re

from tests.spec._spec_markdown import matrix, matrix_classifications, section


_BACKTICKED = re.compile(r"`([^`]+)`")

_PROSE_START = "**SQL types the producer cannot model**"
_PROSE_END = "**A type no rule above names"


def _prose_fields() -> set[str]:
    """Field names SPEC 3.3's `unsupported` paragraph states are emitted, backtick-extracted."""

    block = section(_PROSE_START, _PROSE_END)
    names = set(_BACKTICKED.findall(block))

    return names - set(matrix_classifications())


def _matrix_required_fields() -> set[str]:
    """Field names SPEC 2.2.3's matrix marks REQUIRED (including scoped) for `unsupported`."""

    classifications = matrix_classifications()
    index = classifications.index("unsupported")

    return {field for field, verdicts in matrix().items() if verdicts[index].startswith("R")}


def test_the_prose_paragraph_still_parses() -> None:
    """A guard that silently matched nothing would pass forever."""

    fields = _prose_fields()

    assert "sql_type" in fields
    assert len(fields) >= 5


def test_3_3_and_the_matrix_agree_on_the_unsupported_field_set() -> None:
    prose = _prose_fields()
    required = _matrix_required_fields()

    assert prose == required, (
        f"SPEC 3.3 states {sorted(prose)}, the 2.2.3 matrix requires {sorted(required)}"
    )
