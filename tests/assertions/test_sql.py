"""SQL assertion evaluator tests per ASSERTIONS.md 3."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from dbprint.assertions import AssertionSet, QueryAssertion, evaluate_sql_assertions
from dbprint.assertions import sql as sql_module
from dbprint.conformance.issue import Issue


class _FakeAdapter:
    """Minimal cursor surface used by the SQL assertion evaluator."""

    def __init__(self, results: dict[str, list[tuple[Any, ...]] | Exception]) -> None:
        self._results = results

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        if sql not in self._results:
            raise RuntimeError(f"missing fixture for {sql!r}")

        result = self._results[sql]

        if isinstance(result, Exception):
            raise result

        return result


def _set(queries: tuple[QueryAssertion, ...]) -> AssertionSet:
    return AssertionSet(queries=queries)


class TestExpectZero:
    def test_zero_passes(self) -> None:
        adapter = _FakeAdapter({"SELECT 0": [(0,)]})
        aset = _set((QueryAssertion(name="q1", sql="SELECT 0", expect="0"),))
        assert evaluate_sql_assertions(aset, "primary", adapter) == []

    def test_non_zero_fails(self) -> None:
        adapter = _FakeAdapter({"SELECT 7": [(7,)]})
        aset = _set((QueryAssertion(name="q1", sql="SELECT 7", expect="0"),))
        issues = evaluate_sql_assertions(aset, "primary", adapter)
        assert len(issues) == 1
        assert issues[0].code == "assertion.sql-non-zero"
        assert "7" in issues[0].detail

    def test_null_fails_as_non_zero(self) -> None:
        adapter = _FakeAdapter({"SELECT NULL": [(None,)]})
        aset = _set((QueryAssertion(name="q1", sql="SELECT NULL", expect="0"),))
        issues = evaluate_sql_assertions(aset, "primary", adapter)
        assert issues[0].code == "assertion.sql-non-zero"
        assert "null" in issues[0].detail.lower()

    def test_empty_result_fails_with_sql_empty_result(self) -> None:
        adapter = _FakeAdapter({"SELECT WHERE FALSE": []})
        aset = _set((QueryAssertion(name="q1", sql="SELECT WHERE FALSE", expect="0"),))
        issues = evaluate_sql_assertions(aset, "primary", adapter)
        assert issues[0].code == "assertion.sql-empty-result"

    def test_non_numeric_fails_type_mismatch(self) -> None:
        adapter = _FakeAdapter({"SELECT 'x'": [("x",)]})
        aset = _set((QueryAssertion(name="q1", sql="SELECT 'x'", expect="0"),))
        issues = evaluate_sql_assertions(aset, "primary", adapter)
        assert issues[0].code == "assertion.sql-type-mismatch"

    def test_a_decimal_zero_passes(self) -> None:
        """Every driver here returns DECIMAL, NUMERIC, SUM and AVG as `decimal.Decimal`."""

        adapter = _FakeAdapter({"SELECT SUM(x)": [(Decimal("0.00"),)]})
        aset = _set((QueryAssertion(name="q1", sql="SELECT SUM(x)", expect="0"),))

        assert evaluate_sql_assertions(aset, "primary", adapter) == []

    def test_a_non_zero_decimal_reports_the_count_without_its_scale(self) -> None:
        adapter = _FakeAdapter({"SELECT SUM(x)": [(Decimal("5.00"),)]})
        aset = _set((QueryAssertion(name="q1", sql="SELECT SUM(x)", expect="0"),))
        issues = evaluate_sql_assertions(aset, "primary", adapter)

        assert issues[0].code == "assertion.sql-non-zero"
        assert "actual: 5 " in issues[0].detail

    def test_a_fractional_decimal_keeps_its_spelling(self) -> None:
        adapter = _FakeAdapter({"SELECT AVG(x)": [(Decimal("0.5"),)]})
        aset = _set((QueryAssertion(name="q1", sql="SELECT AVG(x)", expect="0"),))
        issues = evaluate_sql_assertions(aset, "primary", adapter)

        assert issues[0].code == "assertion.sql-non-zero"
        assert "actual: 0.5 " in issues[0].detail

    def test_a_boolean_is_still_refused(self) -> None:
        """`True == 1`, so admitting it would pass an assertion whose query returned a flag."""

        adapter = _FakeAdapter({"SELECT true": [(True,)]})
        aset = _set((QueryAssertion(name="q1", sql="SELECT true", expect="0"),))
        issues = evaluate_sql_assertions(aset, "primary", adapter)

        assert issues[0].code == "assertion.sql-type-mismatch"

    def test_a_non_finite_value_is_a_type_fault(self) -> None:
        for sql, value in (
            ("SELECT nan", Decimal("NaN")),
            ("SELECT inf", float("inf")),
            ("SELECT fnan", float("nan")),
        ):
            adapter = _FakeAdapter({sql: [(value,)]})
            aset = _set((QueryAssertion(name="q1", sql=sql, expect="0"),))
            issues = evaluate_sql_assertions(aset, "primary", adapter)

            assert issues[0].code == "assertion.sql-type-mismatch", value

    def test_an_integer_and_a_float_report_exactly_as_before(self) -> None:
        """The detail string of the common case is what an operator already reads."""

        adapter = _FakeAdapter({"SELECT 7": [(7,)], "SELECT 7.5": [(7.5,)]})
        aset = _set(
            (
                QueryAssertion(name="q1", sql="SELECT 7", expect="0"),
                QueryAssertion(name="q2", sql="SELECT 7.5", expect="0"),
            ),
        )
        details = sorted(i.detail for i in evaluate_sql_assertions(aset, "primary", adapter))

        assert details == ["actual: 7 (expected: 0)", "actual: 7.5 (expected: 0)"]


class TestExpectEmpty:
    def test_zero_rows_passes(self) -> None:
        adapter = _FakeAdapter({"SELECT FROM empty": []})
        aset = _set((QueryAssertion(name="q1", sql="SELECT FROM empty", expect="empty"),))
        assert evaluate_sql_assertions(aset, "primary", adapter) == []

    def test_rows_fail(self) -> None:
        adapter = _FakeAdapter({"SELECT 1": [(1,), (2,), (3,)]})
        aset = _set((QueryAssertion(name="q1", sql="SELECT 1", expect="empty"),))
        issues = evaluate_sql_assertions(aset, "primary", adapter)
        assert issues[0].code == "assertion.sql-non-empty"
        assert "3" in issues[0].detail


class TestSeverity:
    def test_warning_downgrade(self) -> None:
        adapter = _FakeAdapter({"SELECT 5": [(5,)]})
        aset = _set((QueryAssertion(name="q1", sql="SELECT 5", expect="0", severity="warning"),))
        issues = evaluate_sql_assertions(aset, "primary", adapter)
        assert issues[0].severity == "warning"


class TestExecutionError:
    def test_db_error_becomes_sql_execution_error(self) -> None:
        class _Failing:
            def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
                raise RuntimeError("relation does not exist")

        aset = _set((QueryAssertion(name="q1", sql="SELECT * FROM nope", expect="0"),))
        issues = evaluate_sql_assertions(aset, "primary", _Failing())
        assert issues[0].code == "assertion.sql-execution-error"
        assert "does not exist" in issues[0].detail

    def test_a_failing_query_does_not_stop_the_others_from_being_evaluated(self) -> None:
        """Run-all-then-report: one query's adapter error must not swallow its siblings'."""

        adapter = _FakeAdapter(
            {
                "SELECT bad": RuntimeError("relation does not exist"),
                "SELECT 0": [(0,)],
                "SELECT 7": [(7,)],
            },
        )
        aset = _set(
            (
                QueryAssertion(name="broken", sql="SELECT bad", expect="0"),
                QueryAssertion(name="clean", sql="SELECT 0", expect="0"),
                QueryAssertion(name="dirty", sql="SELECT 7", expect="0"),
            ),
        )
        issues = evaluate_sql_assertions(aset, "primary", adapter)
        codes = {i.code for i in issues}

        assert codes == {"assertion.sql-execution-error", "assertion.sql-non-zero"}
        assert len(issues) == 2


class TestDeterministicOrdering:
    def test_issues_sorted_by_path(self) -> None:
        adapter = _FakeAdapter(
            {
                "SELECT 1": [(1,)],
                "SELECT 2": [(2,)],
                "SELECT 3": [(3,)],
            },
        )
        aset = _set(
            (
                QueryAssertion(name="zebra", sql="SELECT 3", expect="0"),
                QueryAssertion(name="alpha", sql="SELECT 1", expect="0"),
                QueryAssertion(name="middle", sql="SELECT 2", expect="0"),
            ),
        )
        issues = evaluate_sql_assertions(aset, "primary", adapter)
        paths = [i.path for i in issues]
        assert paths == sorted(paths)


def _one(
    result: list[tuple[Any, ...]] | Exception,
    expect: Literal["0", "empty"] = "0",
) -> list[Issue]:
    query = QueryAssertion(name="q", sql="SELECT x", expect=expect, severity="warning")

    return evaluate_sql_assertions(_set((query,)), "conn", _FakeAdapter({"SELECT x": result}))


def _issue(code: str, detail: str) -> Issue:
    return Issue(
        path="assertions.conn.queries.q",
        code=code,
        severity="warning",
        detail=detail,
        spec_ref="ASSERTIONS.md §3",
    )


class TestEveryIssueCarriesItsPathSeverityAndSection:
    """ASSERTIONS.md 3: each outcome is addressed by the query's path, at the query's severity."""

    def test_a_query_the_database_refuses(self) -> None:
        assert _one(RuntimeError("relation missing")) == [
            _issue("assertion.sql-execution-error", "DB error: relation missing"),
        ]

    def test_no_rows_where_a_count_was_expected(self) -> None:
        assert _one([]) == [
            _issue(
                "assertion.sql-empty-result",
                "query returned zero rows; expect: 0 requires a scalar result",
            ),
        ]

    def test_a_row_with_no_columns(self) -> None:
        assert _one([()]) == [
            _issue("assertion.sql-empty-result", "query returned a row with no columns"),
        ]

    def test_a_null_count(self) -> None:
        assert _one([(None,)]) == [_issue("assertion.sql-non-zero", "actual: null (expected: 0)")]

    def test_a_count_that_is_not_a_number(self) -> None:
        assert _one([("three",)]) == [
            _issue("assertion.sql-type-mismatch", "actual value 'three' not coercible to integer"),
        ]

    def test_a_fractional_count_is_spelled_positionally(self) -> None:
        assert _one([(1e-07,)]) == [
            _issue("assertion.sql-non-zero", "actual: 0.0000001 (expected: 0)"),
        ]

    def test_ten_rows_are_listed_whole(self) -> None:
        rows = [(n,) for n in range(10)]

        assert _one(rows, expect="empty") == [
            _issue(
                "assertion.sql-non-empty",
                "returned 10 row(s); first 10: [(0,), (1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,)]",
            ),
        ]

    def test_rows_past_the_tenth_are_counted_not_listed(self) -> None:
        rows = [(n,) for n in range(12)]

        assert _one(rows, expect="empty") == [
            _issue(
                "assertion.sql-non-empty",
                "returned 12 row(s); first 10: [(0,), (1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,)] (... 2 more)",
            ),
        ]


class TestACountIsSpelledAsTheNumberItIs:
    def test_an_integral_decimal_sheds_its_scale(self) -> None:
        assert sql_module._spell_count(Decimal("3.00")) == "3"

    def test_a_value_that_is_not_a_number_is_spelled_as_its_text(self) -> None:
        assert sql_module._spell_count("n/a") == "n/a"
