"""`statistics.schema.json` must forbid exactly what `spec.statistics_matrix.FORBIDDEN_FIELDS`
forbids, per classification - the packaged schema's own binding to the matrix it transcribes,
independent of `test_statistics_matrix_agreement.py`'s binding to the markdown table.

A field landing in the matrix with no matching schema clause, or the reverse, is the defect this
guards; `test_statistics_matrix_agreement.py` binds the matrix to the markdown table instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dbprint.spec.statistics_matrix import FORBIDDEN_FIELDS


_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "dbprint"
    / "spec"
    / "v1"
    / "statistics.schema.json"
)

# Each classification's `oneOf` branch, by its `$defs` name.
_RULES_DEF_BY_CLASSIFICATION = {
    "boolean": "BooleanRules",
    "json": "JsonRules",
    "foreign_key_candidate": "ForeignKeyCandidateRules",
    "categorical": "CategoricalRules",
    "temporal": "TemporalRules",
    "numeric": "NumericRules",
    "text": "TextRules",
    "unsupported": "UnsupportedRules",
}


def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _direct_forbidden_fields(rules_block: dict[str, Any]) -> set[str]:
    """Field names a Rules block forbids via a direct top-level `{"not": {"required": [...]}}` in
    its `allOf` - a `$ref` or a nested constraint carries no matrix-forbidden field and is skipped.
    """

    forbidden: set[str] = set()

    for clause in rules_block.get("allOf", []):
        not_clause = clause.get("not")
        if not_clause is not None and set(not_clause) == {"required"}:
            forbidden.update(not_clause["required"])

    return forbidden


def test_the_rules_defs_still_parse() -> None:
    """A guard that silently matched nothing would pass forever."""

    schema = _schema()

    for def_name in _RULES_DEF_BY_CLASSIFICATION.values():
        assert def_name in schema["$defs"]

    assert len(_direct_forbidden_fields(schema["$defs"]["UnsupportedRules"])) >= 15


def test_every_classification_forbids_exactly_the_matrix_set() -> None:
    schema = _schema()

    for classification, def_name in _RULES_DEF_BY_CLASSIFICATION.items():
        declared = _direct_forbidden_fields(schema["$defs"][def_name])
        expected = FORBIDDEN_FIELDS[classification]

        assert declared == expected, (
            f"{classification!r} ({def_name}): schema forbids {sorted(declared)}, "
            f"matrix forbids {sorted(expected)}"
        )


def test_mean_and_sum_carry_a_type() -> None:
    """A bare `{}` would validate any value, of any type."""

    properties = _schema()["$defs"]["Column"]["properties"]

    assert properties["mean"].get("type") == "number"
    assert properties["sum"].get("type") == "number"


def test_a_field_missing_from_a_rules_block_is_detected() -> None:
    """Proves the comparison actually fires - not just that today's schema happens to pass."""

    fake_rules = {
        "allOf": [
            {"not": {"required": ["distribution"]}},
            {"$ref": "#/$defs/SensitivityOnlyInferred"},
        ],
    }

    assert _direct_forbidden_fields(fake_rules) == {"distribution"}
    assert _direct_forbidden_fields(fake_rules) != FORBIDDEN_FIELDS["boolean"]
