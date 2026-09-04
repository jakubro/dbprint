"""Render `spec/statistics_matrix.py`'s per-classification matrix into
docs/reference/statistics-matrix.md - generated so it cannot drift from the module.
"""

from __future__ import annotations

from pathlib import Path

from dbprint.spec.statistics_matrix import FORBIDDEN_FIELDS, REQUIRED_FIELDS


ROOT = Path(__file__).resolve().parents[1]
DOCS_PATH = ROOT / "docs" / "reference" / "statistics-matrix.md"

# Present on every classification's REQUIRED_FIELDS; compressed into one line rather than
# repeated eight times.
_BASE_FIELDS = frozenset(
    {
        "sql_type",
        "nullable",
        "null_count",
        "null_rate",
        "cardinality",
        "cardinality_ratio",
        "cardinality_method",
        "classification",
    },
)

# REQUIRED_FIELDS/FORBIDDEN_FIELDS iteration order is insertion order (SPEC 2.2.3's own table
# order); listing classifications explicitly keeps the page's row order independent of it.
_CLASSIFICATIONS = (
    "boolean",
    "json",
    "foreign_key_candidate",
    "categorical",
    "temporal",
    "numeric",
    "text",
    "unsupported",
)

_HEADER = """\
# Statistics required-field matrix

Generated from `spec/statistics_matrix.py` - do not edit by hand. Run `just docs` to
regenerate. [SPEC 2.2.3](../format/v1/SPEC.md#223-required--optional--forbidden-field-matrix-per-classification) is the
normative table this page mirrors; this page exists so a third-party producer can diff its
own emission logic against something checked cell for cell, rather than reconstructing the
matrix from prose that can silently move underneath it.

Every classification but `unsupported` requires eight base fields, listed once here rather
than in every row: `sql_type`, `nullable`, `null_count`, `null_rate`, `cardinality`,
`cardinality_ratio`, `cardinality_method`, `classification`. `unsupported` requires only the
first four - `cardinality`, `cardinality_ratio` and `cardinality_method` are FORBIDDEN on it
instead of required, so its row lists its actual required set in full rather than a diff
against the base. `rows_scanned` appears in neither column below - it is conditioned on the
file's own `scope` block, not on a classification.

A field absent from both columns for a classification is a footnoted case: `SPEC 2.2.3`'s own
footnotes (marked with a symbol) subtract a required field or add an exception under a stated
condition (an all-null column, redaction, a `sql_type` without day granularity). This page
carries only the unconditional rows; read the footnote text in SPEC for the conditional ones.
"""


def build_document() -> str:
    """Return the full text of the generated statistics required-field matrix page."""

    rows = "\n".join(_row(classification) for classification in _CLASSIFICATIONS)

    return f"{_HEADER}\n| Classification | Required beyond the base 8 | Forbidden |\n|---|---|---|\n{rows}\n"


def _row(classification: str) -> str:
    """One table row. `unsupported` requires fewer than the base 8 (SPEC 2.2.3), so its cell
    lists the full set rather than a diff, which would misread as "nothing extra" instead.
    """

    fields = REQUIRED_FIELDS[classification]
    forbidden_cell = ", ".join(f"`{name}`" for name in sorted(FORBIDDEN_FIELDS[classification]))
    forbidden_cell = forbidden_cell or "-"

    if _BASE_FIELDS <= fields:
        added = sorted(fields - _BASE_FIELDS)
        required_cell = ", ".join(f"`{name}`" for name in added) or "-"
    else:
        full = ", ".join(f"`{name}`" for name in sorted(fields))
        required_cell = f"{full} (fewer than the base 8)"

    return f"| `{classification}` | {required_cell} | {forbidden_cell} |"


if __name__ == "__main__":
    DOCS_PATH.write_text(build_document())
    print(f"wrote {DOCS_PATH}")
