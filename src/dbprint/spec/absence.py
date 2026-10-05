"""What an absent field of a print means (SPEC 7.2, 7.3), read the same way by every consumer.

A reading has five states, so an omitted verdict never reads as a measurement nobody took.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import copy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .classification import is_numeric_type
from .redaction import FIELD_ROLES, WITHHELD_UNDER_REDACTION, is_redacted
from .statistics_matrix import FORBIDDEN_FIELDS


class Absence(StrEnum):
    """Why a field reads as it does."""

    PRESENT = "present"
    OMITTED = "omitted"
    NOT_APPLICABLE = "not_applicable"
    WITHHELD = "withheld"
    UNMEASURED = "unmeasured"


@dataclass(frozen=True)
class FieldReading:
    """One field read against SPEC 7: `value` is the field when PRESENT, the value SPEC implies
    when OMITTED, and None otherwise; `cause` names the SPEC cause in a few words.
    """

    state: Absence
    value: Any
    cause: str
    spec_ref: str

    @property
    def known(self) -> bool:
        """Whether the reading carries a value a reader may act on - measured or implied."""

        return self.state in {Absence.PRESENT, Absence.OMITTED}


COLUMN_FIELDS: frozenset[str] = frozenset(
    {
        "cardinality",
        "cardinality_ratio",
        "cardinality_method",
        "values",
        "values_coverage",
        "values_coverage_method",
        "distribution",
        "frequencies",
        "range",
        "range.span_days",
        "percentiles",
        "mean",
        "sum",
        "zero_count",
        "negative_count",
        "empty_count",
        "quantized_count",
        "length",
        "normalized_cardinality",
        "freshness",
        "unmeasured",
        "unrepresentable",
        "rows_scanned",
        "physical_name",
        "collation",
        "physical_layout_key",
        "populated",
        "inferred.looks_like",
        "inferred.sampled",
        "inferred.matched",
        "inferred.looks_like_candidate",
        "inferred.looks_like_candidate_share",
        "inferred.sensitivity",
        "inferred.epoch_unit",
        "inferred.candidate_key",
        "inferred.candidate_key_exception",
        "inferred.fk_candidate",
        "redacted",
        "sketch",
        "geometry",
        "extent",
        "dimension",
        "norm",
        "parts",
        "parts_found",
        "size",
        "occurrences",
        "types",
    },
)

TABLE_BLOCKS: frozenset[str] = frozenset(
    {
        "catalog_only",
        "external",
        "row_count",
        "row_count_method",
        "scope",
        "null_patterns",
        "null_patterns.coverage_method",
        "physical_layout",
        "merging",
        "grain",
        "grain.search",
        "dependencies",
        "unmeasured",
        "timeline",
        "depends_on",
    },
)

SAMPLED_CLASSIFICATIONS = frozenset({"categorical", "text", "foreign_key_candidate", "binary"})

# The optional fields a failed sample draw names unmeasured on those classifications (SPEC 2.2.4).
SAMPLE_VERDICTS = frozenset({"inferred.looks_like", "inferred.epoch_unit"})


def sample_verdicts(classification: str | None) -> frozenset[str]:
    """The sample verdicts a failed draw leaves owed on `classification`: none it forbids."""

    if classification not in SAMPLED_CLASSIFICATIONS:
        return frozenset()

    return SAMPLE_VERDICTS - _FORBIDDEN_NESTED.get(classification, frozenset())


_LOOKS_LIKE_FIELDS = frozenset(
    {
        "inferred.looks_like",
        "inferred.sampled",
        "inferred.matched",
        "inferred.looks_like_candidate",
        "inferred.looks_like_candidate_share",
    },
)

# The SPEC 2.2.3 matrix's dotted rows, which `statistics_matrix` mirrors only at the top level.
_FORBIDDEN_NESTED: dict[str, frozenset[str]] = {
    "unsupported": _LOOKS_LIKE_FIELDS
    | {
        "inferred.candidate_key",
        "inferred.candidate_key_exception",
        "inferred.epoch_unit",
        "inferred.fk_candidate",
        "inferred.sensitivity",
        "range.span_days",
    },
    "boolean": _LOOKS_LIKE_FIELDS
    | {"inferred.epoch_unit", "inferred.fk_candidate", "range.span_days"},
    "json": _LOOKS_LIKE_FIELDS
    | {"inferred.epoch_unit", "inferred.fk_candidate", "range.span_days"},
    "foreign_key_candidate": frozenset({"range.span_days"}),
    "categorical": frozenset({"inferred.fk_candidate", "range.span_days"}),
    "temporal": _LOOKS_LIKE_FIELDS | {"inferred.epoch_unit", "inferred.fk_candidate"},
    "numeric": _LOOKS_LIKE_FIELDS | {"inferred.fk_candidate", "range.span_days"},
    "composite": _LOOKS_LIKE_FIELDS
    | {"inferred.epoch_unit", "inferred.fk_candidate", "range.span_days"},
    "spatial": _LOOKS_LIKE_FIELDS
    | {
        "inferred.candidate_key",
        "inferred.candidate_key_exception",
        "inferred.epoch_unit",
        "inferred.fk_candidate",
        "range.span_days",
    },
    "vector": _LOOKS_LIKE_FIELDS
    | {
        "inferred.candidate_key",
        "inferred.candidate_key_exception",
        "inferred.epoch_unit",
        "inferred.fk_candidate",
        "range.span_days",
    },
    "binary": frozenset({"inferred.epoch_unit", "inferred.fk_candidate", "range.span_days"}),
    "text": frozenset({"inferred.fk_candidate", "range.span_days"}),
}

_STATED_BY_ABSENCE: dict[str, tuple[Any, str, str]] = {
    "physical_name": (None, "the map key is the catalog's own spelling", "§2.2.4"),
    "collation": (None, "the connection's default collation applies", "§2.2.4"),
    "physical_layout_key": (None, "not a key of this table's physical layout", "§2.2.11"),
    "inferred.sensitivity": (None, "nothing was detected - never that it is safe", "§4.4.2"),
    "inferred.epoch_unit": (None, "no epoch window matched", "§4.5.1"),
    "inferred.candidate_key_exception": (None, "no exception to state", "§4.2"),
    "redacted": (None, "no redact rule covers the column", "§2.2.9"),
    "unmeasured": ([], "every field the column should carry was measured", "§2.2.4"),
    "unrepresentable": ([], "no emitted bound lies outside years 0001-9999", "§2.2.4"),
    "rows_scanned": (None, "the file carries no scope, so row_count is the population", "§2.2.8"),
}

_QUERIED_BLOCKS = frozenset(
    {
        "row_count",
        "row_count_method",
        "null_patterns",
        "null_patterns.coverage_method",
        "physical_layout",
        "grain.search",
        "dependencies",
        "timeline",
    },
)


def read_column_field(column: Mapping[str, Any], path: str) -> FieldReading:
    """Read one per-column field, or a key beneath one (`range.min`), per SPEC 7.2.

    Raises KeyError for a path naming no column field, so a typo never reads as an absence.
    """

    head = _column_head(path)
    value, present = _lookup(column, path)

    if present:
        return FieldReading(Absence.PRESENT, value, "emitted", "§2.2.3")

    if (
        head in _unmeasured(column)
        or (head == "inferred.candidate_key" and "cardinality_ratio" in _unmeasured(column))
        or (head in _LOOKS_LIKE_FIELDS and "inferred.looks_like" in _unmeasured(column))
    ):
        return FieldReading(
            Absence.UNMEASURED,
            None,
            "the read failed this run",
            "§2.2.4",
        )

    classification = column.get("classification")
    top = head.split(".", 1)[0]

    if isinstance(classification, str) and (
        top in FORBIDDEN_FIELDS.get(classification, ())
        or head in _FORBIDDEN_NESTED.get(classification, ())
    ):
        return FieldReading(
            Absence.NOT_APPLICABLE,
            None,
            f"not carried by {classification}",
            "§2.2.3",
        )

    if is_redacted(column) and (
        top in WITHHELD_UNDER_REDACTION
        or (top in {"range", "percentiles"} and column.get("redacted") == "drop")
    ):
        return FieldReading(Absence.WITHHELD, None, "withheld by the redacted marker", "§2.2.9")

    return _implied(column, head)


def read_table_block(statistics: Mapping[str, Any], name: str) -> FieldReading:
    """Read one file-level field of `statistics.yaml`, or a key beneath one, per SPEC 7.3."""

    head = _table_head(name)
    value, present = _lookup(statistics, name)

    if present:
        return FieldReading(Absence.PRESENT, value, "emitted", "§2.2.1")

    if head in _unmeasured(statistics):
        return FieldReading(
            Absence.UNMEASURED,
            None,
            "the read failed this run",
            "§2.2.1",
        )

    if statistics.get("catalog_only") is True and head in _QUERIED_BLOCKS:
        return FieldReading(Absence.NOT_APPLICABLE, None, "nothing was queried", "§2.2.15")

    return _implied_block(statistics, head)


def verdict_withheld(column: Mapping[str, Any], path: str, value: Any) -> bool:
    """Whether a `looks_like` verdict is one SPEC 4.1.5 never publishes on this column's type."""

    sql_type = column.get("sql_type")

    return (
        path in {"looks_like", "inferred.looks_like"}
        and value == "numeric_string"
        and isinstance(sql_type, str)
        and is_numeric_type(sql_type)
    )


def column_value(column: Mapping[str, Any], path: str) -> Any:
    """The value a reader may act on - measured or implied by omission - else None."""

    reading = read_column_field(column, path)

    return reading.value if reading.known else None


def emits(column: Mapping[str, Any], path: str) -> bool:
    """Whether the column carries this field, or this key beneath one (`inferred.looks_like`)."""

    return _lookup(column, path)[1]


def block_value(statistics: Mapping[str, Any], name: str) -> Any:
    """The file-level counterpart of `column_value`."""

    reading = read_table_block(statistics, name)

    return reading.value if reading.known else None


def _implied(column: Mapping[str, Any], head: str) -> FieldReading:
    if head == "inferred.candidate_key":
        ratio = column.get("cardinality_ratio")

        if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
            return FieldReading(Absence.OMITTED, False, "below the candidate-key threshold", "§4.2")

        return FieldReading(Absence.NOT_APPLICABLE, None, "no cardinality_ratio measured", "§4.2")

    if head == "inferred.looks_like":
        cardinality = column.get("cardinality")

        if (
            column.get("classification") in SAMPLED_CLASSIFICATIONS
            and isinstance(cardinality, int)
            and not isinstance(cardinality, bool)
            and cardinality > 0
        ):
            return FieldReading(Absence.OMITTED, None, "no pattern reached the threshold", "§4.1.2")

        return FieldReading(Absence.NOT_APPLICABLE, None, "detection does not run here", "§4.1.5")

    if head == "types":
        return FieldReading(
            Absence.NOT_APPLICABLE,
            None,
            "catalog-only, or the engine names no per-value type",
            "§7.2",
        )

    if head in _STATED_BY_ABSENCE:
        value, cause, spec_ref = _STATED_BY_ABSENCE[head]

        return FieldReading(Absence.OMITTED, copy(value), cause, spec_ref)

    return FieldReading(Absence.NOT_APPLICABLE, None, "not emitted for this column", "§7.2")


def _implied_block(statistics: Mapping[str, Any], head: str) -> FieldReading:
    if head == "catalog_only":
        return FieldReading(Absence.OMITTED, False, "the object was queried", "§2.2.15")

    if head == "external":
        return FieldReading(Absence.OMITTED, False, "the rows are stored locally", "§2.2.20")

    if head == "scope":
        return FieldReading(Absence.OMITTED, None, "every row was read", "§2.2.8")

    if head == "null_patterns":
        return FieldReading(Absence.OMITTED, None, "no column carries a null", "§2.2.10")

    if head == "physical_layout":
        return FieldReading(Absence.OMITTED, None, "the table declares no layout", "§2.2.11")

    if head == "merging":
        return FieldReading(Absence.OMITTED, None, "no merging engine", "§2.2.19")

    if head == "unmeasured":
        return FieldReading(
            Absence.OMITTED,
            [],
            "every block the file should carry was measured",
            "§2.2.1",
        )

    if head == "timeline":
        return FieldReading(
            Absence.OMITTED,
            None,
            "no eligible anchor, a scoped or empty table, or disabled - not distinguishable",
            "§2.2.16",
        )

    if head == "depends_on":
        if statistics.get("type") in {"view", "matview"}:
            return FieldReading(
                Absence.UNMEASURED,
                None,
                "the dependency read did not happen",
                "§2.2.17",
            )

        return FieldReading(Absence.NOT_APPLICABLE, None, "a table has no dependencies", "§2.2.17")

    return FieldReading(Absence.NOT_APPLICABLE, None, "not emitted for this file", "§7.3")


def _column_head(path: str) -> str:
    if path in COLUMN_FIELDS:
        return path

    if path.startswith("inferred."):
        head = ".".join(path.split(".")[:2])
    else:
        head = path.split(".", 1)[0]

    if head in COLUMN_FIELDS or head in FIELD_ROLES:
        return head

    raise KeyError(path)


def _table_head(name: str) -> str:
    if name in TABLE_BLOCKS:
        return name

    head = name.split(".", 1)[0]

    if head in TABLE_BLOCKS:
        return head

    raise KeyError(name)


def _lookup(mapping: Mapping[str, Any], path: str) -> tuple[Any, bool]:
    current: Any = mapping

    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None, False

        current = current[segment]

    return current, True


def _unmeasured(mapping: Mapping[str, Any]) -> frozenset[str]:
    named = mapping.get("unmeasured")

    return (
        frozenset(n for n in named if isinstance(n, str))
        if isinstance(named, list)
        else frozenset()
    )
