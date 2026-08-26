"""Predicate parsing + evaluation per ASSERTIONS.md 2.1."""

from __future__ import annotations

from dbprint.assertions.predicate import (
    EnumPredicate,
    MalformedPredicate,
    PatternPredicate,
    RangePredicate,
    ScalarPredicate,
    SetPredicate,
    evaluate,
    parse,
    resolve,
)
from dbprint.spec.classification import compute_null_rate


class TestParse:
    def test_scalar_numeric(self) -> None:
        p = parse("null_rate", 0.0)
        assert isinstance(p, ScalarPredicate)
        assert p.expected == 0.0

    def test_range_min_only(self) -> None:
        p = parse("cardinality_ratio", {"min": 0.999})
        assert isinstance(p, RangePredicate)
        assert p.min == 0.999
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
        assert ref.found and ref.value == 0.05

    def test_dotted_path(self) -> None:
        ref = resolve({"range": {"min": 0, "max": 100}}, "range.min")
        assert ref.found and ref.value == 0

    def test_missing_returns_not_found(self) -> None:
        ref = resolve({"x": 1}, "y")
        assert not ref.found

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
