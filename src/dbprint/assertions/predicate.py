"""Predicate parsing + evaluation per ASSERTIONS.md 2.1.

Each form parses raw YAML to a typed predicate, then evaluates it against an actual value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Outcome:
    """Predicate evaluation result.

    `malformed` is an ill-formed predicate, not a value mismatch; `detail` empty on pass.
    """

    passed: bool
    detail: str = ""
    malformed: bool = False


@dataclass(frozen=True)
class ScalarPredicate:
    """`<stat>: <value>` - actual MUST equal value."""

    expected: Any


@dataclass(frozen=True)
class RangePredicate:
    """`<stat>: {min: X, max: Y}` - bounds; either or both."""

    min: Any | None = None
    max: Any | None = None


@dataclass(frozen=True)
class EnumPredicate:
    """`<stat>: <enum_value>` - same shape as scalar; distinguished by stat type."""

    expected: str


@dataclass(frozen=True)
class SetPredicate:
    """`accepted_values: [a, b, c]` - the column's values MUST be a subset."""

    expected: tuple[Any, ...]


@dataclass(frozen=True)
class PatternPredicate:
    """`looks_like: email` - inferred.looks_like MUST equal the value."""

    expected: str


@dataclass(frozen=True)
class MalformedPredicate:
    """Parse-time placeholder for unrecognizable predicate shapes."""

    reason: str


Predicate = (
    ScalarPredicate
    | RangePredicate
    | EnumPredicate
    | SetPredicate
    | PatternPredicate
    | MalformedPredicate
)


# Parsing.


_ENUM_STATS = frozenset({"classification", "distribution", "freshness.classification", "sql_type"})
_SET_STATS = frozenset({"accepted_values"})
_PATTERN_STATS = frozenset({"looks_like"})


def parse(stat: str, raw: Any) -> Predicate:
    """Choose the predicate form from the stat name and raw YAML shape.

    A shape fitting no form yields MalformedPredicate, surfaced as assertion.malformed-predicate.
    """

    if stat in _SET_STATS:
        return _parse_set(raw)
    elif stat in _PATTERN_STATS:
        return _parse_pattern(raw)
    elif stat in _ENUM_STATS:
        return _parse_enum(raw)
    elif isinstance(raw, dict):
        return _parse_range(raw)
    else:
        return _parse_scalar(raw)


def _parse_set(raw: Any) -> Predicate:
    if not isinstance(raw, list):
        return MalformedPredicate("accepted_values requires a list")

    return SetPredicate(expected=tuple(raw))


def _parse_pattern(raw: Any) -> Predicate:
    if not isinstance(raw, str):
        return MalformedPredicate("looks_like requires a string")

    return PatternPredicate(expected=raw)


def _parse_enum(raw: Any) -> Predicate:
    if not isinstance(raw, str):
        return MalformedPredicate("enum predicate requires a string")

    return EnumPredicate(expected=raw)


def _parse_range(raw: dict[str, Any]) -> Predicate:
    keys = set(raw)
    allowed = {"min", "max"}

    if not keys or not keys.issubset(allowed):
        return MalformedPredicate("range predicate accepts only min and/or max keys")

    return RangePredicate(min=raw.get("min"), max=raw.get("max"))


def _parse_scalar(raw: Any) -> Predicate:
    return ScalarPredicate(expected=raw)


# Evaluation.


def evaluate(predicate: Predicate, actual: Any) -> Outcome:
    """Run a typed predicate against an actual value; return Outcome."""

    if isinstance(predicate, MalformedPredicate):
        return Outcome(passed=False, detail=predicate.reason, malformed=True)
    elif isinstance(predicate, ScalarPredicate):
        return _eval_scalar(predicate, actual)
    elif isinstance(predicate, RangePredicate):
        return _eval_range(predicate, actual)
    elif isinstance(predicate, EnumPredicate):
        return _eval_enum(predicate, actual)
    elif isinstance(predicate, SetPredicate):
        return _eval_set(predicate, actual)
    else:
        return _eval_pattern(predicate, actual)


def _eval_scalar(p: ScalarPredicate, actual: Any) -> Outcome:
    if actual == p.expected:
        return Outcome(passed=True)

    if actual is not None and _type_family(actual) != _type_family(p.expected):
        return Outcome(
            passed=False,
            detail=f"expected {p.expected!r}, actual {actual!r} - incompatible types",
            malformed=True,
        )

    return Outcome(passed=False, detail=f"expected {p.expected!r}, actual {actual!r}")


def _type_family(value: Any) -> str:
    """Coarse type grouping for scalar-predicate comparability, per ASSERTIONS.md 2.1.

    bool is checked before int because it subclasses int: `nullable: 1` against `True`
    must read as a mismatch, not as a numeric match.
    """

    if isinstance(value, bool):
        return "bool"
    elif isinstance(value, (int, float)):
        return "number"
    elif isinstance(value, str):
        return "string"
    else:
        return "other"


def _eval_range(p: RangePredicate, actual: Any) -> Outcome:
    if actual is None:
        return Outcome(passed=False, detail="actual value is null; range predicate cannot apply")

    try:
        if p.min is not None and actual < p.min:
            return Outcome(passed=False, detail=f"actual {actual!r} < min {p.min!r}")

        if p.max is not None and actual > p.max:
            return Outcome(passed=False, detail=f"actual {actual!r} > max {p.max!r}")
    except TypeError:
        return Outcome(
            passed=False,
            detail=f"actual {actual!r} not comparable to range bounds",
            malformed=True,
        )

    return Outcome(passed=True)


def _eval_enum(p: EnumPredicate, actual: Any) -> Outcome:
    if actual == p.expected:
        return Outcome(passed=True)

    return Outcome(passed=False, detail=f"expected {p.expected!r}, actual {actual!r}")


def _eval_set(p: SetPredicate, actual: Any) -> Outcome:
    """`accepted_values` - actual is the column's value list, or a bare sequence."""

    if isinstance(actual, dict):
        actual_keys = set(actual.keys())
    elif isinstance(actual, (list, tuple, set)):
        actual_keys = {e["value"] if isinstance(e, dict) else e for e in actual}
    elif actual is None:
        return Outcome(passed=True)  # nothing to violate
    else:
        return Outcome(
            passed=False,
            detail=f"actual {actual!r} not a mapping or list; cannot apply accepted_values",
            malformed=True,
        )

    expected_set = set(p.expected)
    extras = actual_keys - expected_set

    if extras:
        return Outcome(
            passed=False,
            detail=f"unexpected values present: {sorted(extras, key=str)!r}",
        )

    return Outcome(passed=True)


def _eval_pattern(p: PatternPredicate, actual: Any) -> Outcome:
    if actual == p.expected:
        return Outcome(passed=True)

    return Outcome(
        passed=False,
        detail=f"expected looks_like={p.expected!r}, actual {actual!r}",
    )


# Helpers used by the statistic / SQL assertion evaluators.


@dataclass(frozen=True)
class StatRef:
    """Resolved stat reference per ASSERTIONS.md 2.4 (e.g. `range.min`)."""

    value: Any
    found: bool


def resolve(stats: dict[str, Any], path: str) -> StatRef:
    """Resolve a dotted path against the column stats dict.

    `found=False` when a segment is missing or its parent is not a dict; `accepted_values`
    resolves to the column's `values` list only when that list is exhaustive.
    """

    if path == "accepted_values":
        # Only an exhaustive list is a domain; a truncated slice would flag unlisted values.
        if "values" in stats and stats.get("values_coverage") == 1.0:
            return StatRef(value=stats.get("values"), found=True)

        return StatRef(value=None, found=False)

    if path == "looks_like":
        inferred = stats.get("inferred") or {}

        if isinstance(inferred, dict) and "looks_like" in inferred:
            return StatRef(value=inferred.get("looks_like"), found=True)

        return StatRef(value=None, found=False)

    if path == "candidate_key":
        inferred = stats.get("inferred") or {}

        if isinstance(inferred, dict) and "candidate_key" in inferred:
            return StatRef(value=inferred.get("candidate_key"), found=True)

        return StatRef(value=None, found=False)

    parts = path.split(".")
    current: Any = stats

    for seg in parts:
        if not isinstance(current, dict) or seg not in current:
            return StatRef(value=None, found=False)

        current = current[seg]

    return StatRef(value=current, found=True)


# Vocabulary - the assertable stat names per ASSERTIONS.md 2.4.

ASSERTABLE_STATS: frozenset[str] = frozenset(
    {
        "sql_type",
        "nullable",
        "null_count",
        "null_rate",
        "cardinality",
        "cardinality_ratio",
        "classification",
        "distribution",
        "accepted_values",
        "looks_like",
        "candidate_key",
        "range.min",
        "range.max",
        "freshness.classification",
        "freshness.max_age_days",
    },
)


def is_assertable_stat(name: str) -> bool:
    """Allow vocabulary stats + dotted percentiles.* paths."""

    return name in ASSERTABLE_STATS or name.startswith("percentiles.")


# Edge-claim vocabulary (SPEC 2.7.2) - the `RefersTo.observed` block (SPEC 2.3.10), never
# merged into ASSERTABLE_STATS: `assertions/statistic.py` reuses that set for the
# `.dbprint.yaml` DSL, which ASSERTIONS.md 0.2 scopes to columns only.
EDGE_ASSERTABLE_STATS: frozenset[str] = frozenset(
    {
        "observed.fanout_avg",
        "observed.fanout_max",
        "observed.target_coverage",
        "observed.containment",
        "observed.coherent",
        "observed.scope_compatible",
        "observed.answerable_count",
    },
)


def is_assertable_edge_stat(name: str) -> bool:
    """Allow the edge-claim vocabulary (SPEC 2.7.2)."""

    return name in EDGE_ASSERTABLE_STATS


# Redaction substitutes cell values, not measurements (SPEC 2.2.9); these mark what a
# predicate cannot be answered against on a redacted column.
_VALUE_BEARING_PREFIXES = ("range.", "percentiles.")

# `range` and `percentiles` are not assertable alone (-> assertion.unknown-stat) but resolve
# here, so a redacted column refuses them as a warning rather than an error (SPEC 2.2.9).
# Redaction coarsens `freshness.max_age_days`; `classification` is derived from the true age.
_VALUE_BEARING_NAMES = frozenset(
    {"accepted_values", "range", "percentiles", "freshness.max_age_days"},
)


def is_value_bearing_stat(name: str) -> bool:
    """True when a predicate's subject is a cell value rather than a measurement."""

    return name in _VALUE_BEARING_NAMES or name.startswith(_VALUE_BEARING_PREFIXES)
