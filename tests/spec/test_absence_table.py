"""SPEC 7.2 lists exactly the fields the SPEC 2.2.3 matrix lets a column omit.

The matrix is parsed from the markdown, not from `spec/statistics_matrix.py`: that module
is an unguarded mirror of the same rows, so comparing against it checks copy against copy.
"""

from __future__ import annotations

import re

import pytest

from tests.spec._spec_markdown import matrix as _matrix
from tests.spec._spec_markdown import section as _section
from tests.spec._spec_markdown import table_rows as _table_rows


# The only verdict leaving no absence to interpret; every conditional form allows omission.
_ALWAYS_REQUIRED = "R"

_BACKTICKED = re.compile(r"`([^`]+)`")


def _absence_table_fields() -> list[str]:
    """Every field named in the first column of SPEC 7.2, in document order."""

    rows = _table_rows(_section("### 7.2 Absent per-column fields", "### 7.3"))

    return [field for cells in rows[1:] for field in _BACKTICKED.findall(cells[0])]


def test_the_matrix_still_parses() -> None:
    """A guard that silently matched nothing would pass forever."""

    matrix = _matrix()

    assert len(matrix) >= 25
    assert set(matrix["sql_type"]) == {_ALWAYS_REQUIRED}
    assert "—" in matrix["freshness"]


def test_every_omittable_field_is_listed() -> None:
    omittable = {
        field
        for field, verdicts in _matrix().items()
        if any(verdict != _ALWAYS_REQUIRED for verdict in verdicts)
    }

    assert omittable - set(_absence_table_fields()) == set()


def test_no_always_required_field_is_listed() -> None:
    """A field REQUIRED everywhere has no absence to explain; listing one misleads."""

    always_required = {
        field
        for field, verdicts in _matrix().items()
        if all(verdict == _ALWAYS_REQUIRED for verdict in verdicts)
    }

    assert always_required & set(_absence_table_fields()) == set()


def test_the_absence_table_invents_no_field() -> None:
    assert set(_absence_table_fields()) - set(_matrix()) == set()


def test_no_field_is_listed_twice() -> None:
    """Two rows for one field would let a reader stop at whichever they met first."""

    listed = _absence_table_fields()

    assert sorted(listed) == sorted(set(listed))


def test_every_row_names_a_cause_and_a_discriminator() -> None:
    rows = _table_rows(_section("### 7.2 Absent per-column fields", "### 7.3"))

    for cells in rows[1:]:
        assert len(cells) == 3, cells
        assert all(cell for cell in cells), cells


@pytest.mark.parametrize(
    "shape",
    ["`values: []`", "`refers_to: []`", "`referenced_by: []`", "`grain.keys: []`"],
)
def test_an_emitted_empty_collection_is_read_in_7_4_and_not_as_an_absence(shape: str) -> None:
    """An emitted empty list is a measurement, so it belongs beside neither absence table."""

    assert shape in _section("### 7.4 Empty is not absent", "## Appendix A")
    assert shape not in _section("### 7.2 Absent per-column fields", "### 7.3")


def test_producer_failure_is_stated_as_representable_only_through_the_marker() -> None:
    """The one cause no row can carry, so 7.1 names where it IS carried instead.

    A reader who missed that would take every absence below for a property of the data.
    """

    preamble = _section("### 7.1 The two absences", "### 7.2")

    assert "`unmeasured`" in preamble
    assert "only on an artifact that carries the marker" in preamble
    assert "not representable at all" not in preamble
