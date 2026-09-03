"""`spec.statistics_matrix` must agree with the SPEC 2.2.3 field matrix it transcribes.

The engine and the conformance validator both read the module, so they can only agree with
each other, never with the specification - this is the one check against the markdown.
"""

from __future__ import annotations

import re

from dbprint.spec.statistics_matrix import FORBIDDEN_FIELDS, REQUIRED_FIELDS
from tests.spec._spec_markdown import SPEC, matrix, matrix_classifications


_H1_RE = re.compile(r"^# ", re.MULTILINE)


# The column-field verdict forms of SPEC 2.2.3's legend, escaped to keep this file ASCII.
# "R (scoped)" is reached only by `rows_scanned` and is handled by name below.
_REQUIRED = "R"
_OPTIONAL = "O"
_FORBIDDEN = "\u2014"  # the table's em-dash: MUST NOT emit
_R_DAGGER = "R\u2020"  # REQUIRED unless SPEC 2.2.3's dropped-bound footnote applies
_R_DOUBLE_DAGGER = "R\u2021"  # REQUIRED unless the prose-column footnote applies
_R_PILCROW = "R\u00b6"  # REQUIRED unless the single-row-redacted-aggregate footnote applies
_R_PILCROW_VBAR = "R\u00b6\u2016"  # both the pilcrow condition and the type-admission footnote
_R_REFERENCE_MARK = "R\u203b"  # REQUIRED unless the day-resolution footnote applies

# SPEC 2.2.3's footnote conditions: the flat row is REQUIRED and each reader subtracts the
# condition procedurally, so every footnote - alone or combined - maps to plain R's membership.
_REQUIRED_VERDICTS = frozenset(
    {_REQUIRED, _R_DAGGER, _R_DOUBLE_DAGGER, _R_PILCROW, _R_PILCROW_VBAR, _R_REFERENCE_MARK},
)


# The module models a nested field at its container's name only, so a dotted field
# appears in neither REQUIRED_FIELDS nor FORBIDDEN_FIELDS.
def _is_nested(field: str) -> bool:
    return "." in field


def test_the_matrix_still_parses() -> None:
    """A guard that silently matched nothing would pass forever."""

    field_matrix = matrix()

    assert len(field_matrix) >= 25
    assert "rows_scanned" in field_matrix


def test_every_flat_field_agrees_with_the_module() -> None:
    field_matrix = matrix()
    classifications = matrix_classifications()

    for field, verdicts in field_matrix.items():
        if _is_nested(field) or field == "rows_scanned":
            continue

        for classification, verdict in zip(classifications, verdicts, strict=True):
            required = field in REQUIRED_FIELDS[classification]
            forbidden = field in FORBIDDEN_FIELDS[classification]
            where = f"{field!r} x {classification!r} (verdict {verdict!r})"

            assert not (required and forbidden), f"{where}: module marks both"

            if verdict in _REQUIRED_VERDICTS:
                assert required, f"{where}: markdown REQUIREs it, module does not"
            elif verdict == _FORBIDDEN:
                assert forbidden, f"{where}: markdown forbids it, module does not"
            elif verdict == _OPTIONAL:
                assert not required and not forbidden, f"{where}: markdown says OPTIONAL"
            else:
                raise AssertionError(
                    f"{where}: unrecognized verdict - decide its module mapping and "
                    "extend this test before trusting it",
                )


def test_nested_fields_are_modelled_at_the_container_only() -> None:
    """`inferred.*` and `range.span_days` never appear under their own dotted name."""

    field_matrix = matrix()
    classifications = matrix_classifications()
    nested = [field for field in field_matrix if _is_nested(field)]

    assert nested, "no dotted fields found - matrix parsing regressed"

    for field in nested:
        for classification in classifications:
            assert field not in REQUIRED_FIELDS[classification]
            assert field not in FORBIDDEN_FIELDS[classification]


def test_unsupported_forbids_the_whole_inferred_container() -> None:
    """Every `inferred.*` row reads "-" for `unsupported`; the module states this once."""

    field_matrix = matrix()
    classifications = matrix_classifications()
    idx = classifications.index("unsupported")
    inferred_rows = {f: v for f, v in field_matrix.items() if f.startswith("inferred.")}

    assert inferred_rows, "no inferred.* rows found - matrix parsing regressed"
    assert all(verdicts[idx] == _FORBIDDEN for verdicts in inferred_rows.values())
    assert "inferred" in FORBIDDEN_FIELDS["unsupported"]


def test_rows_scanned_is_conditioned_on_the_file_not_a_column() -> None:
    """`R (scoped)` is keyed to `scope` (SPEC 2.2.8), so the module lists it neither way."""

    field_matrix = matrix()
    classifications = matrix_classifications()

    assert set(field_matrix["rows_scanned"]) == {"R (scoped)"}

    for classification in classifications:
        assert "rows_scanned" not in REQUIRED_FIELDS[classification]
        assert "rows_scanned" not in FORBIDDEN_FIELDS[classification]


def test_the_document_has_exactly_one_top_level_heading() -> None:
    """A footnote marker with Markdown significance (`#` at column 0) reads as a second `<h1>`
    on any compliant renderer - the site, GitHub, and the wheel `hatch_build` packages all see it.
    """

    text = SPEC.read_text(encoding="utf-8")

    assert len(_H1_RE.findall(text)) == 1


def test_the_footnote_conditions_reach_exactly_their_known_cells() -> None:
    """A cell gaining or losing a footnote changes both readers - the module's flat REQUIRED
    entry and the validator's procedural subtraction - so the exact set is asserted.
    """

    field_matrix = matrix()
    classifications = matrix_classifications()
    daggered: set[tuple[str, str]] = set()
    double_daggered: set[tuple[str, str]] = set()
    pilcrowed: set[tuple[str, str]] = set()
    pilcrow_vbarred: set[tuple[str, str]] = set()
    reference_marked: set[tuple[str, str]] = set()

    for field, verdicts in field_matrix.items():
        for classification, verdict in zip(classifications, verdicts, strict=True):
            if verdict == _R_DAGGER:
                daggered.add((field, classification))
            elif verdict == _R_DOUBLE_DAGGER:
                double_daggered.add((field, classification))
            elif verdict == _R_PILCROW:
                pilcrowed.add((field, classification))
            elif verdict == _R_PILCROW_VBAR:
                pilcrow_vbarred.add((field, classification))
            elif verdict == _R_REFERENCE_MARK:
                reference_marked.add((field, classification))

    assert daggered == {
        ("range", "temporal"),
        ("range", "numeric"),
        ("range.span_days", "temporal"),
        ("percentiles", "temporal"),
        ("percentiles", "numeric"),
    }
    assert double_daggered == {
        ("values", "text"),
        ("values_coverage", "text"),
        ("distribution", "text"),
    }
    assert pilcrowed == {
        ("mean", "numeric"),
        ("sum", "numeric"),
        ("length", "text"),
    }
    assert pilcrow_vbarred == {
        ("length", "categorical"),
        ("length", "foreign_key_candidate"),
    }
    assert reference_marked == {
        ("quantized_count", "temporal"),
    }
