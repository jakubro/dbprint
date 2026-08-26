"""Assertions block parser tests per ASSERTIONS.md 1."""

from __future__ import annotations

import pytest

from dbprint.assertions import AssertionSet, ParseError, parse_block


class TestEmptyForms:
    def test_none_returns_empty(self) -> None:
        result = parse_block(None)
        assert isinstance(result, AssertionSet)
        assert result.is_empty

    def test_empty_dict_returns_empty(self) -> None:
        result = parse_block({})
        assert result.is_empty

    def test_explicit_empty_tables_and_queries(self) -> None:
        result = parse_block({"tables": {}, "queries": []})
        assert result.is_empty

    def test_non_dict_raises(self) -> None:
        """The one shape nothing else can be extracted from still aborts (ASSERTIONS.md 1.2)."""

        with pytest.raises(ParseError):
            parse_block("not a dict")


class TestTablesParsing:
    def test_simple_row_count_predicate(self) -> None:
        result = parse_block({"tables": {"seedbank.collector": {"row_count": {"min": 1000}}}})
        assert "seedbank.collector" in result.tables
        assert result.tables["seedbank.collector"].row_count == {"min": 1000}

    def test_per_column_predicates(self) -> None:
        result = parse_block(
            {
                "tables": {
                    "seedbank.collector": {
                        "columns": {
                            "email": {"null_rate": 0.0, "cardinality_ratio": {"min": 0.999}},
                            "country_code": {"accepted_values": ["AU", "CA"]},
                        },
                    },
                },
            },
        )
        cols = result.tables["seedbank.collector"].columns
        assert cols["email"] == {"null_rate": 0.0, "cardinality_ratio": {"min": 0.999}}
        assert cols["country_code"] == {"accepted_values": ["AU", "CA"]}

    def test_non_mapping_body_becomes_a_fault_and_drops_that_table(self) -> None:
        result = parse_block({"tables": {"seedbank.collector": "scalar"}})
        assert "seedbank.collector" not in result.tables
        assert [f.code for f in result.faults] == ["assertion.malformed-block"]
        assert result.faults[0].path == "tables.seedbank.collector"

    def test_non_mapping_columns_becomes_a_fault_and_keeps_the_table(self) -> None:
        """The table's row_count survives even though its columns block is malformed."""

        result = parse_block(
            {"tables": {"seedbank.collector": {"row_count": {"min": 1}, "columns": ["not a map"]}}},
        )
        assert result.tables["seedbank.collector"].row_count == {"min": 1}
        assert result.tables["seedbank.collector"].columns == {}
        assert [f.code for f in result.faults] == ["assertion.malformed-block"]
        assert result.faults[0].path == "tables.seedbank.collector.columns"

    def test_a_malformed_table_does_not_discard_a_well_formed_sibling(self) -> None:
        """A malformed table's fault does not cost a well-formed sibling its own checks."""

        result = parse_block(
            {
                "tables": {
                    "seedbank.collector": "scalar",
                    "seedbank.accession": {"row_count": {"min": 1}},
                },
            },
        )
        assert "seedbank.accession" in result.tables
        assert "seedbank.collector" not in result.tables
        assert len(result.faults) == 1

    def test_tables_itself_non_mapping_becomes_a_fault_not_a_raise(self) -> None:
        """`tables:` at the top level, not a per-table body - the outer shape fault."""

        result = parse_block({"tables": "scalar"})
        assert result.tables == {}
        assert [f.code for f in result.faults] == ["assertion.malformed-block"]
        assert result.faults[0].path == "tables"


class TestQueriesParsing:
    def test_minimal_query(self) -> None:
        result = parse_block(
            {
                "queries": [
                    {"name": "q1", "sql": "SELECT 0", "expect": 0},
                ],
            },
        )
        assert len(result.queries) == 1
        q = result.queries[0]
        assert q.name == "q1"
        assert q.sql == "SELECT 0"
        assert q.expect == "0"
        assert q.severity == "error"  # default
        assert result.faults == ()

    def test_severity_warning(self) -> None:
        result = parse_block(
            {"queries": [{"name": "q1", "sql": "x", "expect": 0, "severity": "warning"}]},
        )
        assert result.queries[0].severity == "warning"

    def test_expect_empty(self) -> None:
        result = parse_block({"queries": [{"name": "q1", "sql": "x", "expect": "empty"}]})
        assert result.queries[0].expect == "empty"

    def test_missing_name_becomes_a_fault(self) -> None:
        result = parse_block({"queries": [{"sql": "x", "expect": 0}]})
        assert result.queries == ()
        assert [f.code for f in result.faults] == ["assertion.malformed-block"]

    def test_missing_sql_becomes_a_fault(self) -> None:
        result = parse_block({"queries": [{"name": "q1", "expect": 0}]})
        assert result.queries == ()
        assert [f.code for f in result.faults] == ["assertion.malformed-block"]
        assert result.faults[0].path == "queries.q1"

    def test_invalid_expect_becomes_a_fault(self) -> None:
        result = parse_block({"queries": [{"name": "q1", "sql": "x", "expect": "anything"}]})
        assert result.queries == ()
        assert [f.code for f in result.faults] == ["assertion.malformed-block"]
        assert result.faults[0].path == "queries.q1"

    def test_queries_itself_non_list_becomes_a_fault_not_a_raise(self) -> None:
        """`queries:` at the top level, not one entry within it - the outer shape fault."""

        result = parse_block({"queries": "scalar"})
        assert result.queries == ()
        assert [f.code for f in result.faults] == ["assertion.malformed-block"]
        assert result.faults[0].path == "queries"

    def test_a_non_mapping_query_entry_becomes_a_fault(self) -> None:
        result = parse_block({"queries": ["not a mapping"]})
        assert result.queries == ()
        assert [f.code for f in result.faults] == ["assertion.malformed-block"]
        assert result.faults[0].path == "queries[0]"

    def test_a_malformed_query_does_not_discard_a_well_formed_sibling(self) -> None:
        """A bad `expect` alongside valid assertions still evaluates them (ASSERTIONS.md 5.4)."""

        result = parse_block(
            {
                "tables": {"seedbank.collector": {"row_count": {"min": 1}}},
                "queries": [
                    {"name": "bad", "sql": "SELECT 0", "expect": "bogus"},
                    {"name": "good", "sql": "SELECT 0", "expect": 0},
                ],
            },
        )
        assert "seedbank.collector" in result.tables
        assert [q.name for q in result.queries] == ["good"]
        assert [f.code for f in result.faults] == ["assertion.malformed-block"]

    def test_duplicate_query_names_keep_the_first_and_fault_the_rest(self) -> None:
        result = parse_block(
            {
                "queries": [
                    {"name": "q1", "sql": "x", "expect": 0},
                    {"name": "q1", "sql": "y", "expect": 0},
                ],
            },
        )
        assert len(result.queries) == 1
        assert result.queries[0].sql == "x"
        assert [f.code for f in result.faults] == ["assertion.duplicate-query-name"]
        assert result.faults[0].path == "queries.q1"

    def test_unknown_severity_defaults_to_warning(self) -> None:
        # ASSERTIONS.md 7.
        result = parse_block(
            {"queries": [{"name": "q1", "sql": "x", "expect": 0, "severity": "critical"}]},
        )
        assert result.queries[0].severity == "warning"
