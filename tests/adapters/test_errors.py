"""QueryFailed - one-line form, detail block, and truncation budgets."""

from __future__ import annotations

from dbprint.adapters.errors import QueryFailed


class TestOneLineForm:
    def test_str_names_the_cause_type(self) -> None:
        exc = QueryFailed(TypeError("not all arguments converted"), "SELECT 1")

        assert str(exc) == "TypeError: not all arguments converted"

    def test_cause_and_statement_are_retained(self) -> None:
        cause = ValueError("boom")
        exc = QueryFailed(cause, "SELECT 1", ("a",))

        assert exc.cause is cause
        assert exc.sql == "SELECT 1"
        assert exc.params == ("a",)


class TestDetailBlock:
    def test_statement_and_params_rendered(self) -> None:
        exc = QueryFailed(TypeError("x"), "SELECT a\nFROM t\nWHERE b = ?", ("v",))
        detail = exc.detail()

        assert "statement:" in detail
        assert "WHERE b = ?" in detail
        assert "params: ('v',)" in detail

    def test_params_omitted_when_absent(self) -> None:
        detail = QueryFailed(TypeError("x"), "SELECT 1").detail()

        assert "params:" not in detail

    def test_leading_indentation_stripped_from_statement(self) -> None:
        exc = QueryFailed(TypeError("x"), "\n        SELECT a\n        FROM t\n        ")

        assert "    SELECT a" in exc.detail()
        assert "            SELECT a" not in exc.detail()

    def test_long_statement_truncated(self) -> None:
        detail = QueryFailed(TypeError("x"), "\n".join(f"line {i}" for i in range(40))).detail()

        assert "truncated" in detail
        assert "line 39" not in detail

    def test_long_params_truncated(self) -> None:
        detail = QueryFailed(TypeError("x"), "SELECT 1", ("v" * 500,)).detail()

        assert "truncated" in detail
        assert len(detail) < 500
