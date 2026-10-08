"""Predicate parsing + evaluation per ASSERTIONS.md 2.1."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from dbprint.spec.absence import Absence
from dbprint.spec.classification import compute_null_rate
from dbprint.spec.predicate import (
    EnumPredicate,
    MalformedPredicate,
    Outcome,
    PatternPredicate,
    Predicate,
    RangePredicate,
    ScalarPredicate,
    SetPredicate,
    evaluate,
    inapplicable_reason,
    is_assertable_edge_stat,
    is_assertable_stat,
    is_value_bearing_stat,
    parse,
    resolve,
    resolve_edge_stat,
    with_evidence,
)
from dbprint.spec.scope import scope_of


class TestParse:
    def test_scalar_numeric(self) -> None:
        p = parse("null_rate", 0.0)
        assert isinstance(p, ScalarPredicate)
        assert p.expected == 0.0  # noqa: RUF069 - the expected value is an exact literal

    def test_range_min_only(self) -> None:
        p = parse("cardinality_ratio", {"min": 0.999})
        assert isinstance(p, RangePredicate)
        assert p.min == 0.999  # noqa: RUF069 - the expected value is an exact literal
        assert p.max is None

    def test_range_both(self) -> None:
        p = parse("null_rate", {"min": 0.0, "max": 0.01})
        assert isinstance(p, RangePredicate)
        assert (p.min, p.max) == (0.0, 0.01)

    def test_range_unknown_key_malformed(self) -> None:
        p = parse("null_rate", {"avg": 0.5})
        assert isinstance(p, MalformedPredicate)

    def test_enum_classification(self) -> None:
        p = parse("classification", "text")
        assert isinstance(p, EnumPredicate)
        assert p.expected == "text"

    def test_set_accepted_values(self) -> None:
        p = parse("accepted_values", ["a", "b", "c"])
        assert isinstance(p, SetPredicate)
        assert p.expected == ("a", "b", "c")

    def test_set_non_list_malformed(self) -> None:
        p = parse("accepted_values", "a")
        assert isinstance(p, MalformedPredicate)

    def test_pattern_looks_like(self) -> None:
        p = parse("looks_like", "email")
        assert isinstance(p, PatternPredicate)
        assert p.expected == "email"

    def test_pattern_non_string_malformed(self) -> None:
        p = parse("looks_like", 5)
        assert isinstance(p, MalformedPredicate)

    def test_sql_type_is_enum_not_range(self) -> None:
        """ASSERTIONS.md 2.4: sql_type is scalar/enum - a dict value must not fall to range."""

        p = parse("sql_type", "uuid")
        assert isinstance(p, EnumPredicate)
        assert p.expected == "uuid"


class TestEvaluate:
    def test_scalar_pass(self) -> None:
        assert evaluate(ScalarPredicate(0.0), 0.0).passed

    def test_scalar_fail(self) -> None:
        outcome = evaluate(ScalarPredicate(0.0), 0.5)
        assert not outcome.passed
        assert "0.5" in outcome.detail or "0.0" in outcome.detail
        assert not outcome.malformed

    def test_scalar_incompatible_type_is_malformed(self) -> None:
        """ASSERTIONS.md 2.1's own example: a string scalar against a numeric stat."""

        outcome = evaluate(ScalarPredicate("high"), 0.05)
        assert not outcome.passed
        assert outcome.malformed

    def test_scalar_null_actual_is_an_ordinary_mismatch_not_malformed(self) -> None:
        outcome = evaluate(ScalarPredicate(0.0), None)
        assert not outcome.passed
        assert not outcome.malformed

    def test_scalar_bool_against_number_is_malformed(self) -> None:
        """bool is an int subclass, so `nullable: true` against 0/1 must not pass as numeric."""

        outcome = evaluate(ScalarPredicate(True), 0)
        assert not outcome.passed
        assert outcome.malformed

    def test_scalar_bool_matching_bool_is_an_ordinary_mismatch(self) -> None:
        outcome = evaluate(ScalarPredicate(True), False)
        assert not outcome.passed
        assert not outcome.malformed

    def test_range_within(self) -> None:
        assert evaluate(RangePredicate(min=0.0, max=1.0), 0.5).passed

    def test_range_below_min(self) -> None:
        outcome = evaluate(RangePredicate(min=0.5), 0.1)
        assert not outcome.passed
        assert "min" in outcome.detail

    def test_range_above_max(self) -> None:
        outcome = evaluate(RangePredicate(max=0.5), 0.9)
        assert not outcome.passed
        assert "max" in outcome.detail

    def test_range_actual_none(self) -> None:
        outcome = evaluate(RangePredicate(min=0.5), None)
        assert not outcome.passed

    def test_enum_pass(self) -> None:
        assert evaluate(EnumPredicate("text"), "text").passed

    def test_enum_fail(self) -> None:
        assert not evaluate(EnumPredicate("text"), "numeric").passed

    def test_set_subset_passes(self) -> None:
        assert evaluate(SetPredicate(("a", "b", "c")), {"a": 1, "b": 2}).passed

    def test_set_extras_fail(self) -> None:
        outcome = evaluate(SetPredicate(("a", "b")), {"a": 1, "c": 3})
        assert not outcome.passed
        assert "c" in outcome.detail

    def test_set_list_actual_passes(self) -> None:
        assert evaluate(SetPredicate(("a", "b")), ["a"]).passed

    def test_set_none_actual_passes(self) -> None:
        # Nothing to violate.
        assert evaluate(SetPredicate(("a", "b")), None).passed

    def test_pattern_match(self) -> None:
        assert evaluate(PatternPredicate("email"), "email").passed

    def test_pattern_mismatch(self) -> None:
        outcome = evaluate(PatternPredicate("email"), "uuid")
        assert not outcome.passed


class TestScalarEqualityAgainstAFlooredOrCeilingedRatio:
    """SPEC 2.2.6's floor/ceiling changes what `null_rate: 0`/`1` mean (ASSERTIONS.md 2.6)."""

    def test_a_nonzero_null_count_fails_an_exact_zero_assertion(self) -> None:
        published = compute_null_rate(1, 10_000_000)  # floored, not the raw 0.0

        assert not evaluate(ScalarPredicate(0.0), published).passed

    def test_a_nonzero_non_null_count_fails_an_exact_one_assertion(self) -> None:
        published = compute_null_rate(9_999_999, 10_000_000)  # ceilinged, not the raw 1.0

        assert not evaluate(ScalarPredicate(1.0), published).passed

    def test_a_range_predicate_tolerates_the_floor(self) -> None:
        """The effectively-zero-tolerance workaround ASSERTIONS.md 2.6 suggests."""

        published = compute_null_rate(1, 10_000_000)

        assert evaluate(RangePredicate(max=0.000001), published).passed


class TestResolve:
    def test_flat_field(self) -> None:
        ref = resolve({"null_rate": 0.05}, "null_rate")
        assert ref.found and ref.value == 0.05  # noqa: RUF069 - the expected value is an exact literal

    def test_dotted_path(self) -> None:
        ref = resolve({"range": {"min": 0, "max": 100}}, "range.min")
        assert ref.found and ref.value == 0

    def test_missing_returns_not_found(self) -> None:
        ref = resolve({"classification": "numeric"}, "mean")
        assert not ref.found

    def test_a_path_naming_no_field_raises(self) -> None:
        with pytest.raises(KeyError):
            resolve({"x": 1}, "y")

    def test_an_absent_key_below_the_threshold_resolves_false(self) -> None:
        ref = resolve({"classification": "categorical", "cardinality_ratio": 0.2}, "candidate_key")
        assert ref.found and ref.value is False

    def test_an_absent_pattern_on_an_eligible_column_resolves_none(self) -> None:
        ref = resolve({"classification": "text", "cardinality": 9}, "looks_like")
        assert ref.found and ref.value is None

    def test_missing_nested_segment(self) -> None:
        ref = resolve({"range": {"min": 0}}, "range.max")
        assert not ref.found

    def test_accepted_values_routes_to_an_exhaustive_value_list(self) -> None:
        values = [{"value": "a", "count": 1}, {"value": "b", "count": 2}]
        ref = resolve({"values": values, "values_coverage": 1.0}, "accepted_values")

        assert ref.found and ref.value == values

    def test_accepted_values_does_not_resolve_against_a_truncated_list(self) -> None:
        """A capped list is the frequent slice of a domain, not the domain."""

        ref = resolve(
            {"values": [{"value": "a", "count": 1}], "values_coverage": 0.4},
            "accepted_values",
        )

        assert not ref.found

    def test_looks_like_routes_to_inferred(self) -> None:
        ref = resolve({"inferred": {"looks_like": "email"}}, "looks_like")
        assert ref.found and ref.value == "email"

    def test_candidate_key_routes_to_inferred(self) -> None:
        ref = resolve({"inferred": {"candidate_key": True}}, "candidate_key")
        assert ref.found and ref.value is True

    def test_percentile_dotted(self) -> None:
        ref = resolve({"percentiles": {"p99": 5000}}, "percentiles.p99")
        assert ref.found and ref.value == 5000


class TestTemporalRange:
    """A temporal value and its bounds compare as instants, never as spellings."""

    @pytest.mark.parametrize(
        ("actual", "bounds", "passed"),
        [
            ("2024-03-01T01:00:00", {"min": "2024-03-01 05:00:00"}, False),
            ("2024-03-01T01:00:00Z", {"min": "2024-03-01T00:30:00-01:00"}, False),
            ("2024-03-05T04:00:00Z", {"max": "2024-03-05T05:00:00+02:00"}, False),
            ("2024-03-01T01:00:00Z", {"min": "2024-03-01T02:00:00+01:00"}, True),
            ("2024-03-01T01:00:00", {"min": "2024-03-01T01:00:00Z"}, True),
            ("2024-03-02", {"min": "2024-03-02T12:00:00"}, False),
            ("2024-03-02", {"min": date(2024, 3, 1)}, True),
            ("2024-03-05T04:00:00", {"max": datetime.fromisoformat("2024-03-06T00:00:00")}, True),
            ("08:01:00", {"min": "08:30:00"}, False),
            ("08:01:00", {"min": "08:00:00"}, True),
            ("infinity", {"max": "2030-01-01"}, False),
            ("-infinity", {"min": "1900-01-01"}, False),
            ("0044-03-15 BC", {"max": "0001-01-01"}, True),
            ("9999-12-31T23:59:59.999999", {"max": "9999-12-31T23:59:59.999998"}, False),
        ],
    )
    def test_a_bound_is_read_by_value(self, actual: str, bounds: dict, passed: bool) -> None:
        outcome = evaluate(RangePredicate(**bounds), actual)

        assert (outcome.passed, outcome.malformed) == (passed, False)

    @pytest.mark.parametrize(
        ("actual", "bound"),
        [
            ("2024-03-01T01:00:00Z", "next tuesday"),
            ("2024-03-01T01:00:00Z", 5),
            ("08:01:00", "2024-03-01T00:00:00"),
            ("2024-03-01", "08:00:00"),
        ],
    )
    def test_a_bound_of_another_kind_is_malformed(self, actual: str, bound: object) -> None:
        assert evaluate(RangePredicate(max=bound), actual).malformed

    def test_a_scalar_temporal_is_instant_equality(self) -> None:
        predicate = ScalarPredicate("2024-03-01T01:00:00+00:00")

        assert evaluate(predicate, "2024-03-01T01:00:00Z").passed
        assert not evaluate(predicate, "2024-03-01T01:00:01Z").passed


class TestScalarTypeFamily:
    """The family check runs before equality, so `True == 1` never passes a predicate."""

    @pytest.mark.parametrize(("expected", "actual"), [(1, True), (0, False), (False, 0)])
    def test_bool_against_number_is_malformed(self, expected: object, actual: object) -> None:
        assert evaluate(ScalarPredicate(expected), actual).malformed

    def test_a_decimal_is_a_number(self) -> None:
        assert evaluate(ScalarPredicate(3), Decimal(3)).passed


class TestDetailSpelling:
    def test_a_violated_bound_is_spelled_as_it_was_written(self) -> None:
        outcome = evaluate(RangePredicate(max=0.00000004), 0.000000049)

        assert outcome.detail == "actual 0.000000049 > max 0.00000004"

    def test_the_evidence_a_verdict_rests_on_is_spelled_as_written(self) -> None:
        stats = {
            "cardinality_ratio": 0.00003,
            "inferred": {"looks_like_candidate": "email", "looks_like_candidate_share": 0.00004},
        }

        assert with_evidence(stats, "candidate_key", "x") == "x (cardinality_ratio 0.00003)"
        assert with_evidence(stats, "looks_like", "x").endswith("at share 0.00004)")

    def test_a_non_number_keeps_its_repr(self) -> None:
        outcome = evaluate(ScalarPredicate("high"), 0.05)

        assert "'high'" in (outcome.detail or "")


_MALFORMED_SHAPES = [
    ("accepted_values", "a", MalformedPredicate("accepted_values requires a list")),
    ("looks_like", 3, MalformedPredicate("looks_like requires a string")),
    ("distribution", 3, MalformedPredicate("enum predicate requires a string")),
    ("null_rate", {}, MalformedPredicate("range predicate accepts only min and/or max keys")),
    (
        "null_rate",
        {"min": 0, "limit": 1},
        MalformedPredicate("range predicate accepts only min and/or max keys"),
    ),
]


@pytest.mark.parametrize(("stat", "raw", "expected"), _MALFORMED_SHAPES)
def test_a_malformed_shape_says_what_its_form_requires(
    stat: str,
    raw: object,
    expected: MalformedPredicate,
) -> None:
    assert parse(stat, raw) == expected


_OUTCOMES = [
    (MalformedPredicate("why"), 1, Outcome(passed=False, detail="why", malformed=True)),
    (ScalarPredicate(0.5), 0.5, Outcome(passed=True)),
    (ScalarPredicate(0.5), 0.25, Outcome(passed=False, detail="expected 0.5, actual 0.25")),
    (ScalarPredicate(2), None, Outcome(passed=False, detail="expected 2, actual None")),
    (
        ScalarPredicate("x"),
        2,
        Outcome(passed=False, detail="expected 'x', actual 2 - incompatible types", malformed=True),
    ),
    (
        ScalarPredicate(None),
        "y",
        Outcome(
            passed=False,
            detail="expected None, actual 'y' - incompatible types",
            malformed=True,
        ),
    ),
    (ScalarPredicate("2024-03-01"), "2024-03-01", Outcome(passed=True)),
    (
        ScalarPredicate("2024-03-01"),
        "2024-03-02",
        Outcome(passed=False, detail="expected '2024-03-01', actual '2024-03-02'"),
    ),
    (
        ScalarPredicate(5),
        "2024-03-02",
        Outcome(
            passed=False,
            detail="bound 5 is not a date, instant or time of day comparable to '2024-03-02'",
            malformed=True,
        ),
    ),
    (
        RangePredicate(min=1),
        None,
        Outcome(passed=False, detail="actual value is null; range predicate cannot apply"),
    ),
    (RangePredicate(min=1), 1, Outcome(passed=True)),
    (RangePredicate(max=1), 1, Outcome(passed=True)),
    (RangePredicate(min=1), 0, Outcome(passed=False, detail="actual 0 < min 1")),
    (RangePredicate(max=1), 2, Outcome(passed=False, detail="actual 2 > max 1")),
    (
        RangePredicate(min=1),
        "a",
        Outcome(passed=False, detail="actual 'a' not comparable to range bounds", malformed=True),
    ),
    (RangePredicate(min="2024-01-01"), "2024-01-01", Outcome(passed=True)),
    (RangePredicate(max="2024-01-01"), "2024-01-01", Outcome(passed=True)),
    (
        RangePredicate(min="2024-01-02"),
        "2024-01-01",
        Outcome(passed=False, detail="actual '2024-01-01' < min '2024-01-02'"),
    ),
    (
        RangePredicate(max="2024-01-01"),
        "2024-01-02",
        Outcome(passed=False, detail="actual '2024-01-02' > max '2024-01-01'"),
    ),
    (
        RangePredicate(max="12:00:00"),
        "2024-01-02",
        Outcome(
            passed=False,
            detail=(
                "bound '12:00:00' is not a date, instant or time of day comparable to '2024-01-02'"
            ),
            malformed=True,
        ),
    ),
    (EnumPredicate("uniform"), "uniform", Outcome(passed=True)),
    (
        EnumPredicate("uniform"),
        "skewed",
        Outcome(passed=False, detail="expected 'uniform', actual 'skewed'"),
    ),
    (SetPredicate(("a", "b")), {"a": 1}, Outcome(passed=True)),
    (SetPredicate(("a",)), [{"value": "a", "count": 2}], Outcome(passed=True)),
    (
        SetPredicate(("a",)),
        [{"value": "c", "count": 2}, "b"],
        Outcome(passed=False, detail="unexpected values present: ['b', 'c']"),
    ),
    (
        SetPredicate(("a",)),
        7,
        Outcome(
            passed=False,
            detail="actual 7 not a mapping or list; cannot apply accepted_values",
            malformed=True,
        ),
    ),
    (
        SetPredicate(("a",)),
        ["a", 1, "b"],
        Outcome(passed=False, detail="unexpected values present: [1, 'b']"),
    ),
    (RangePredicate(max="12:00:00+00:00"), "13:00:00+02:00", Outcome(passed=True)),
    (PatternPredicate("email"), "email", Outcome(passed=True)),
    (
        PatternPredicate("email"),
        "url",
        Outcome(passed=False, detail="expected looks_like='email', actual 'url'"),
    ),
]


@pytest.mark.parametrize(("predicate", "actual", "expected"), _OUTCOMES)
def test_an_outcome_carries_its_verdict_detail_and_malformed_flag(
    predicate: Predicate,
    actual: object,
    expected: Outcome,
) -> None:
    assert evaluate(predicate, actual) == expected


def test_a_bool_is_its_own_family_before_a_number() -> None:
    assert evaluate(ScalarPredicate(1), True).malformed is True
    assert evaluate(ScalarPredicate(True), 1).malformed is True
    assert evaluate(ScalarPredicate(1.5), Decimal("1.5")) == Outcome(passed=True)
    assert evaluate(ScalarPredicate([1]), [1]) == Outcome(passed=True)


def test_accepted_values_over_a_scoped_complete_list_is_not_applicable() -> None:
    stats = {"classification": "categorical", "values": [{"value": "a", "count": 1}]}
    stats["values_coverage"] = 1.0
    scope = scope_of({"row_count": 10, "scope": {"rows_scanned": 4, "sample": 0.4}})

    ref = resolve(stats, "accepted_values", scope)

    assert (ref.found, ref.reading.state, ref.reading.spec_ref) == (
        False,
        Absence.NOT_APPLICABLE,
        "§2.2.8",
    )
    assert ref.reading.cause.startswith("accepted_values needs the column's whole domain;")


def test_accepted_values_over_a_truncated_list_names_why() -> None:
    stats = {"classification": "categorical", "values": [{"value": "a", "count": 1}]}
    stats["values_coverage"] = 0.5

    got = resolve(stats, "accepted_values").reading

    assert (got.state, got.cause, got.spec_ref) == (
        Absence.NOT_APPLICABLE,
        "the published value list is not exhaustive",
        "§2.2.3",
    )


class TestInapplicableReason:
    def test_an_unmeasured_stat_names_the_column_and_cause(self) -> None:
        stats = {"classification": "numeric", "unmeasured": ["range"]}
        ref = resolve(stats, "range.min")

        assert inapplicable_reason(stats, "n", "range.min", ScalarPredicate(1), ref) == (
            "stat 'range.min' is unmeasured for column 'n': the read failed this run"
        )

    def test_an_absent_stat_names_the_column_and_cause(self) -> None:
        stats = {"classification": "boolean"}
        ref = resolve(stats, "range.min")

        assert inapplicable_reason(stats, "b", "range.min", ScalarPredicate(1), ref) == (
            "stat 'range.min' not emitted for column 'b': not carried by boolean"
        )

    def test_numeric_string_on_a_numeric_type_is_never_published(self) -> None:
        stats = {"classification": "categorical", "sql_type": "INTEGER", "cardinality": 4}
        predicate = PatternPredicate("numeric_string")

        reason = inapplicable_reason(
            stats,
            "n",
            "looks_like",
            predicate,
            resolve(stats, "looks_like"),
        )

        assert reason == "looks_like 'numeric_string' is never published on a numeric SQL type"

    def test_an_evaluable_stat_has_no_reason(self) -> None:
        stats = {"classification": "numeric", "range": {"min": 1}}
        ref = resolve(stats, "range.min")

        assert inapplicable_reason(stats, "n", "range.min", ScalarPredicate(1), ref) is None


class TestWithEvidence:
    def test_a_candidate_key_failure_carries_its_ratio(self) -> None:
        stats = {"classification": "categorical", "cardinality_ratio": 0.25}

        assert with_evidence(stats, "candidate_key", "failed") == "failed (cardinality_ratio 0.25)"

    def test_a_looks_like_failure_carries_the_nearest_candidate(self) -> None:
        stats = {
            "classification": "text",
            "inferred": {"looks_like_candidate": "email", "looks_like_candidate_share": 0.5},
        }

        assert with_evidence(stats, "looks_like", "failed") == (
            "failed (nearest candidate 'email' at share 0.5)"
        )

    def test_a_non_numeric_ratio_is_shown_as_written(self) -> None:
        stats = {"classification": "categorical", "cardinality_ratio": True}

        assert with_evidence(stats, "candidate_key", "failed") == "failed (cardinality_ratio True)"

    def test_without_evidence_the_detail_stands(self) -> None:
        stats = {"classification": "text", "inferred": {"looks_like_candidate": "email"}}

        assert with_evidence(stats, "looks_like", "failed") == "failed"
        assert with_evidence({"classification": "json"}, "candidate_key", "failed") == "failed"
        assert with_evidence(stats, "null_rate", "failed") == "failed"


class TestResolveEdgeStat:
    def test_a_present_path_is_emitted(self) -> None:
        ref = resolve_edge_stat({"observed": {"fanout_max": 3}}, "observed.fanout_max")

        assert (ref.value, ref.reading.value, ref.found, ref.reading.cause) == (
            3,
            3,
            True,
            "emitted",
        )
        assert ref.reading.spec_ref == "§2.3.10"

    @pytest.mark.parametrize("edge", [{}, {"observed": 4}, {"observed": {"fanout_avg": 1}}])
    def test_an_absent_path_is_not_applicable(self, edge: dict[str, object]) -> None:
        ref = resolve_edge_stat(edge, "observed.fanout_max")

        assert (ref.value, ref.found, ref.reading.state, ref.reading.cause) == (
            None,
            False,
            Absence.NOT_APPLICABLE,
            "not emitted",
        )
        assert ref.reading.spec_ref == "§2.3.10"


@pytest.mark.parametrize(
    ("name", "column", "edge", "value_bearing"),
    [
        ("null_rate", True, False, False),
        ("percentiles.p50", True, False, True),
        ("range.min", True, False, True),
        ("range", False, False, True),
        ("accepted_values", True, False, True),
        ("freshness.max_age_days", True, False, True),
        ("freshness.classification", True, False, False),
        ("observed.containment", False, True, False),
        ("observed", False, False, False),
    ],
)
def test_the_assertable_vocabulary(
    name: str,
    column: bool,
    edge: bool,
    value_bearing: bool,
) -> None:
    assert (
        is_assertable_stat(name),
        is_assertable_edge_stat(name),
        is_value_bearing_stat(name),
    ) == (column, edge, value_bearing)
