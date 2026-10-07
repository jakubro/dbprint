"""A file read over part of its table is scoped whichever signal says so, and qualifies its claims."""

from __future__ import annotations

import pytest

from dbprint.spec.scope import (
    SCANNED_DOMAIN_STATEMENT,
    WHOLE_DOMAIN_STATEMENT,
    coverage_statement,
    list_is_complete,
    list_is_table_domain,
    qualify,
    reply_scope,
    rows_scanned,
    scope_line,
    scope_of,
)


_LIST = {"classification": "categorical", "values": [{"value": "a", "count": 3}]}


def test_a_scope_block_makes_the_file_scoped() -> None:
    scope = scope_of({"row_count": 1000, "scope": {"rows_scanned": 250, "sample": 0.25}})

    assert scope is not None
    assert (scope.rows_scanned, scope.share, scope.sample) == (250, 0.25, 0.25)


def test_a_malformed_block_is_still_scoped() -> None:
    scope = scope_of({"scope": "rows 1-10", "columns": {}})

    assert scope is not None
    assert scope.block == {}


def test_a_column_echo_without_a_block_is_still_scoped() -> None:
    scope = scope_of({"columns": {"plot": {"rows_scanned": 40}}})

    assert scope is not None
    assert scope.rows_scanned == 40


def test_neither_signal_is_unscoped() -> None:
    assert scope_of({"row_count": 5, "columns": {"plot": {"cardinality": 5}}}) is None
    assert scope_of(None) is None


def test_a_complete_list_is_the_table_domain_only_unscoped() -> None:
    column = {**_LIST, "values_coverage": 1.0}
    scope = scope_of({"scope": {"rows_scanned": 9}})

    assert list_is_complete(column)
    assert list_is_table_domain(column, None)
    assert not list_is_table_domain(column, scope)


def test_a_numeric_list_listing_an_exact_cardinality_is_complete() -> None:
    column = {
        "classification": "numeric",
        "cardinality": 3,
        "cardinality_method": "exact",
        "frequencies": {"listed": 3},
    }

    assert list_is_complete(column)
    assert not list_is_complete({**column, "cardinality_method": "approximate"})


def test_qualify_is_a_no_op_unscoped() -> None:
    scope = scope_of({"scope": {"rows_scanned": 9}})

    assert qualify("2 distinct", None) == "2 distinct"
    assert qualify("2 distinct", scope) == "2 distinct over the rows scanned"


def test_the_coverage_statement_follows_the_scope() -> None:
    scope = scope_of({"scope": {"rows_scanned": 9}})

    assert coverage_statement(1.0, None) == WHOLE_DOMAIN_STATEMENT
    assert coverage_statement(1.0, scope) == SCANNED_DOMAIN_STATEMENT


def test_the_scope_line_names_the_narrowing() -> None:
    filtered = scope_of({"row_count": 1000, "scope": {"rows_scanned": 900, "filter": "id <= 900"}})
    echo_only = scope_of({"columns": {"plot": {"rows_scanned": 40}}})

    assert filtered is not None
    assert echo_only is not None
    assert scope_line(filtered) == "Scanned: 900 of 1000 rows (90%); filtered by `id <= 900`"
    assert scope_line(echo_only) == "Scanned: 40 rows"


def test_a_reply_carries_the_block_verbatim_and_the_row_count() -> None:
    block = {"rows_scanned": 0, "filter": "false"}
    scope = scope_of({"row_count": 12, "scope": block})

    assert reply_scope(scope) == {"scope": block, "row_count": 12}
    assert reply_scope(None) == {}


def test_a_column_missing_its_echo_takes_the_files_population() -> None:
    scope = scope_of({"scope": {"rows_scanned": 9}})

    assert rows_scanned({"classification": "text"}, scope) == 9
    assert rows_scanned({"rows_scanned": 9}, None) is None


def test_a_malformed_scope_field_reads_as_absent() -> None:
    scope = scope_of(
        {"row_count": "10", "scope": {"rows_scanned": 4, "sample": True, "filter": 5}},
    )

    assert scope is not None
    assert (scope.rows_scanned, scope.row_count, scope.sample, scope.filter, scope.share) == (
        4,
        None,
        None,
        None,
        None,
    )


def test_a_numeric_sample_and_a_string_filter_are_kept() -> None:
    scope = scope_of(
        {"row_count": 8, "scope": {"rows_scanned": 4, "sample": 0.5, "filter": "a > 1"}},
    )

    assert scope is not None
    assert (scope.sample, scope.filter, scope.share) == (0.5, "a > 1", 0.5)


def test_a_columns_own_echo_wins_over_the_files() -> None:
    scope = scope_of({"row_count": 20, "scope": {"rows_scanned": 10}})

    assert rows_scanned({"rows_scanned": 7}, scope) == 7
    assert rows_scanned({}, scope) == 10
    assert rows_scanned({"rows_scanned": "7"}, scope) == 10


@pytest.mark.parametrize(
    ("statistics", "expected"),
    [
        ({"scope": {"sample": 0.5}}, "Scanned: part of the table; sampled"),
        ({"row_count": 0, "scope": {"rows_scanned": 4}}, "Scanned: 4 rows"),
        (
            {"row_count": 8, "scope": {"rows_scanned": 4, "filter": "a > 1"}},
            "Scanned: 4 of 8 rows (50%); filtered by `a > 1`",
        ),
        (
            {"row_count": 8, "scope": {"rows_scanned": 4, "filter": "  "}},
            "Scanned: 4 of 8 rows (50%)",
        ),
    ],
)
def test_the_scanned_line(statistics: dict[str, object], expected: str) -> None:
    scope = scope_of(statistics)

    assert scope is not None
    assert scope_line(scope) == expected
