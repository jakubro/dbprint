"""Predicate parsing + evaluation per ASSERTIONS.md 2.1.

Each form parses raw YAML to a typed predicate, then evaluates it against an actual value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from .absence import Absence, FieldReading, read_column_field, verdict_withheld
from .scope import ScanScope, list_is_complete, scope_line
from .temporal_age import parse_instant
from .temporal_range import is_representable, leading_year
from .value_text import spell_number


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
    if (reading := _temporal(actual)) is not None:
        expected = _temporal(p.expected)

        if expected is None or expected.kind != reading.kind:
            return _not_comparable(p.expected, actual)

        if expected.key == reading.key:
            return Outcome(passed=True)

        return Outcome(
            passed=False,
            detail=f"expected {_shown(p.expected)}, actual {_shown(actual)}",
        )

    if actual is not None and _type_family(actual) != _type_family(p.expected):
        return Outcome(
            passed=False,
            detail=f"expected {_shown(p.expected)}, actual {_shown(actual)} - incompatible types",
            malformed=True,
        )

    if actual == p.expected:
        return Outcome(passed=True)

    return Outcome(passed=False, detail=f"expected {_shown(p.expected)}, actual {_shown(actual)}")


def _type_family(value: Any) -> str:
    """Coarse type grouping for scalar-predicate comparability, per ASSERTIONS.md 2.1.

    bool is checked before int because it subclasses int: `nullable: 1` against `True`
    must read as a mismatch, not as a numeric match.
    """

    if isinstance(value, bool):
        return "bool"
    elif isinstance(value, (int, float, Decimal)):
        return "number"
    elif isinstance(value, str):
        return "string"
    else:
        return "other"


def _eval_range(p: RangePredicate, actual: Any) -> Outcome:
    if actual is None:
        return Outcome(passed=False, detail="actual value is null; range predicate cannot apply")

    if (reading := _temporal(actual)) is not None:
        return _eval_temporal_range(p, actual, reading)

    try:
        if p.min is not None and actual < p.min:
            return Outcome(passed=False, detail=f"actual {_shown(actual)} < min {_shown(p.min)}")

        if p.max is not None and actual > p.max:
            return Outcome(passed=False, detail=f"actual {_shown(actual)} > max {_shown(p.max)}")
    except TypeError:
        return Outcome(
            passed=False,
            detail=f"actual {_shown(actual)} not comparable to range bounds",
            malformed=True,
        )

    return Outcome(passed=True)


def _eval_temporal_range(p: RangePredicate, actual: Any, reading: _Temporal) -> Outcome:
    bounds = {"min": p.min, "max": p.max}
    readings = {name: _temporal(bound) for name, bound in bounds.items() if bound is not None}

    for name, bound in readings.items():
        if bound is None or bound.kind != reading.kind:
            return _not_comparable(bounds[name], actual)

    if (low := readings.get("min")) is not None and reading.key < low.key:
        return Outcome(passed=False, detail=f"actual {_shown(actual)} < min {_shown(p.min)}")

    if (high := readings.get("max")) is not None and reading.key > high.key:
        return Outcome(passed=False, detail=f"actual {_shown(actual)} > max {_shown(p.max)}")

    return Outcome(passed=True)


def _not_comparable(bound: Any, actual: Any) -> Outcome:
    return Outcome(
        passed=False,
        detail=f"bound {_shown(bound)} is not a date, instant or time of day comparable to {_shown(actual)}",
        malformed=True,
    )


@dataclass(frozen=True)
class _Temporal:
    kind: Literal["instant", "clock"]
    key: tuple[int, float]


def _temporal(value: Any) -> _Temporal | None:
    """A date, instant or clock time as one key: naive is UTC, unrepresentable years sort outside."""

    if isinstance(value, (date, datetime)):
        instant = parse_instant(value)

        return None if instant is None else _Temporal("instant", (0, _micros(instant)))

    if not isinstance(value, str):
        return None

    if (instant := parse_instant(value)) is not None:
        return _Temporal("instant", (0, _micros(instant)))

    try:
        clock = time.fromisoformat(value)
    except ValueError:
        clock = None

    if clock is not None:
        on_day = datetime.combine(_CLOCK_DAY, clock)
        on_day = on_day if on_day.tzinfo is not None else on_day.replace(tzinfo=UTC)

        return _Temporal("clock", (0, _micros(on_day)))

    if not is_representable(value) and (year := leading_year(value)) is not None:
        return _Temporal("instant", (-1 if year.year < 1 else 1, year.year))

    return None


def _micros(instant: datetime) -> int:
    return (instant - _EPOCH) // timedelta(microseconds=1)


def _eval_enum(p: EnumPredicate, actual: Any) -> Outcome:
    if actual == p.expected:
        return Outcome(passed=True)

    return Outcome(passed=False, detail=f"expected {_shown(p.expected)}, actual {_shown(actual)}")


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
    """Resolved stat reference per ASSERTIONS.md 2.4 (e.g. `range.min`), with the SPEC 7 reading
    that tells a predicate to evaluate, to skip, or why it skipped.
    """

    value: Any
    reading: FieldReading

    @property
    def found(self) -> bool:
        """Whether a predicate can be evaluated - the value was measured or implied by omission."""

        return self.reading.known


def resolve(stats: dict[str, Any], path: str, scope: ScanScope | None = None) -> StatRef:
    """Resolve an assertable stat against a column's statistics through `spec.absence`.

    `accepted_values` binds only a list that is the column's whole domain: exhaustive and unscoped.
    """

    reading = read_column_field(stats, _COLUMN_FIELD_OF.get(path, path))

    if path == "accepted_values" and reading.state is Absence.PRESENT:
        if not list_is_complete(stats):
            reading = FieldReading(
                Absence.NOT_APPLICABLE,
                None,
                "the published value list is not exhaustive",
                "§2.2.3",
            )
        elif scope is not None:
            reading = FieldReading(
                Absence.NOT_APPLICABLE,
                None,
                "accepted_values needs the column's whole domain; the list is complete over "
                f"the rows scanned only ({scope_line(scope)})",
                "§2.2.8",
            )

    return StatRef(value=reading.value, reading=reading)


def inapplicable_reason(
    stats: dict[str, Any],
    column: str,
    stat: str,
    predicate: Predicate,
    ref: StatRef,
) -> str | None:
    """Why a column predicate cannot be evaluated against `ref`, or None when it can."""

    if ref.reading.state is Absence.UNMEASURED:
        return f"stat {stat!r} is unmeasured for column {column!r}: {ref.reading.cause}"

    if not ref.found:
        return f"stat {stat!r} not emitted for column {column!r}: {ref.reading.cause}"

    if isinstance(predicate, PatternPredicate) and verdict_withheld(
        stats,
        stat,
        predicate.expected,
    ):
        return f"looks_like {predicate.expected!r} is never published on a numeric SQL type"

    return None


def with_evidence(stats: dict[str, Any], stat: str, detail: str) -> str:
    """Append the measurement a failed `candidate_key` or `looks_like` verdict was decided on."""

    if stat == "candidate_key":
        ratio = read_column_field(stats, "cardinality_ratio")

        if ratio.known:
            return f"{detail} (cardinality_ratio {_shown_bare(ratio.value)})"

    if stat == "looks_like":
        candidate = read_column_field(stats, "inferred.looks_like_candidate")
        share = read_column_field(stats, "inferred.looks_like_candidate_share")

        if candidate.known and share.known:
            return f"{detail} (nearest candidate {candidate.value!r} at share {_shown_bare(share.value)})"

    return detail


def resolve_edge_stat(edge: dict[str, Any], path: str) -> StatRef:
    """Resolve a dotted `observed.*` path against a relationship edge (SPEC 2.3.10)."""

    current: Any = edge

    for seg in path.split("."):
        if not isinstance(current, dict) or seg not in current:
            return StatRef(
                value=None,
                reading=FieldReading(Absence.NOT_APPLICABLE, None, "not emitted", "§2.3.10"),
            )

        current = current[seg]

    return StatRef(
        value=current,
        reading=FieldReading(Absence.PRESENT, current, "emitted", "§2.3.10"),
    )


_COLUMN_FIELD_OF = {
    "accepted_values": "values",
    "looks_like": "inferred.looks_like",
    "candidate_key": "inferred.candidate_key",
}


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


_CLOCK_DAY = date(2000, 1, 1)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

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


def _shown_bare(value: Any) -> str:
    numeric = isinstance(value, int | float | Decimal) and not isinstance(value, bool)

    return spell_number(value) if numeric else str(value)


def _shown(value: Any) -> str:
    if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        return spell_number(value)

    return repr(value)
