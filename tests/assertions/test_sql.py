"""SQL assertion evaluator tests per ASSERTIONS.md 3."""

from __future__ import annotations

from typing import Any

from dbprint.assertions import AssertionSet, QueryAssertion, evaluate_sql_assertions


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
