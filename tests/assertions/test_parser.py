"""Assertions block parser tests per ASSERTIONS.md 1."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dbprint.assertions import (
    AssertionSet,
    ParseError,
    ParseFault,
    QueryAssertion,
    TablePredicates,
    parse_block,
)


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


_YAML_SCALARS = st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=8)
_YAML_VALUES = st.recursive(
    _YAML_SCALARS,
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.text(max_size=8) | st.integers(), inner, max_size=3)
    ),
    max_leaves=15,
)
_ASSERTION_KEYS = st.sampled_from(["tables", "queries"]) | st.text(max_size=8)


class TestParseProperties:
    @given(st.dictionaries(_ASSERTION_KEYS, _YAML_VALUES, max_size=3))
    def test_any_mapping_parses_without_raising(self, raw: dict[object, object]) -> None:
        assert isinstance(parse_block(raw), AssertionSet)

    @given(_YAML_VALUES.filter(lambda value: value is not None and not isinstance(value, dict)))
    def test_a_non_mapping_raises_parse_error_alone(self, raw: object) -> None:
        with pytest.raises(ParseError):
            parse_block(raw)


def _fault(path: str, detail: str, code: str = "assertion.malformed-block") -> ParseFault:
    spec_ref = (
        "ASSERTIONS.md §3.5" if code == "assertion.duplicate-query-name" else "ASSERTIONS.md §1.2"
    )

    return ParseFault(path=path, code=code, detail=detail, spec_ref=spec_ref)


class TestEveryFaultNamesItsPathCodeDetailAndSection:
    """ASSERTIONS.md 1.2 and 3.5: a malformed entry is reported where it sits, never fatally."""

    def test_the_root_that_is_not_a_mapping_names_its_type(self) -> None:
        with pytest.raises(ParseError, match=r"^assertions: must be a mapping, got list$"):
            parse_block([])

    def test_tables_that_are_not_a_mapping(self) -> None:
        assert parse_block({"tables": [1]}).faults == (
            _fault("tables", "assertions.tables: must be a mapping, got list"),
        )

    def test_every_malformed_table_is_reported_not_only_the_first(self) -> None:
        result = parse_block({"tables": {"s.a": 1, "s.b": "x", "s.c": {"row_count": 3}}})

        assert result.faults == (
            _fault("tables.s.a", "assertions.tables.s.a: must be a mapping, got int"),
            _fault("tables.s.b", "assertions.tables.s.b: must be a mapping, got str"),
        )
        assert result.tables == {"s.c": TablePredicates(fqn="s.c", row_count=3)}

    def test_a_table_with_no_body_is_an_empty_table(self) -> None:
        result = parse_block({"tables": {"s.a": None}})

        assert result.faults == ()
        assert result.tables == {"s.a": TablePredicates(fqn="s.a")}

    def test_columns_that_are_not_a_mapping(self) -> None:
        assert parse_block({"tables": {"s.a": {"columns": [1]}}}).faults == (
            _fault(
                "tables.s.a.columns",
                "assertions.tables.s.a.columns: must be a mapping, got list",
            ),
        )

    def test_every_malformed_column_is_reported_and_an_empty_one_kept(self) -> None:
        result = parse_block(
            {"tables": {"s.a": {"columns": {"x": 1, "y": None, "z": [2], "w": {"null_count": 0}}}}},
        )

        assert result.faults == (
            _fault(
                "tables.s.a.columns.x",
                "assertions.tables.s.a.columns.x: must be a mapping, got int",
            ),
            _fault(
                "tables.s.a.columns.z",
                "assertions.tables.s.a.columns.z: must be a mapping, got list",
            ),
        )
        assert result.tables["s.a"].columns == {"y": {}, "w": {"null_count": 0}}

    def test_queries_that_are_not_a_list(self) -> None:
        assert parse_block({"queries": {"q": 1}}).faults == (
            _fault("queries", "assertions.queries: must be a list, got dict"),
        )

    def test_a_query_that_is_not_a_mapping(self) -> None:
        assert parse_block({"queries": ["SELECT 1"]}).faults == (
            _fault("queries[0]", "assertions.queries[0]: must be a mapping, got str"),
        )

    def test_a_query_with_an_empty_name(self) -> None:
        assert parse_block({"queries": [{"name": "", "sql": "SELECT 0", "expect": 0}]}).faults == (
            _fault("queries[0]", "assertions.queries[0]: `name` is required and must be a string"),
        )

    def test_a_query_with_no_sql(self) -> None:
        assert parse_block({"queries": [{"name": "q", "sql": " ", "expect": 0}]}).faults == (
            _fault("queries.q", "assertions.queries[0] 'q': `sql` is required"),
        )

    def test_a_query_with_an_unknown_expectation(self) -> None:
        assert parse_block({"queries": [{"name": "q", "sql": "SELECT 0", "expect": 1}]}).faults == (
            _fault("queries.q", "assertions.queries[0] 'q': `expect` must be 0 or empty"),
        )

    def test_a_duplicate_query_name(self) -> None:
        entry = {"name": "q", "sql": "SELECT 0", "expect": 0}

        assert parse_block({"queries": [entry, entry]}).faults == (
            _fault(
                "queries.q",
                "assertions.queries: duplicate name 'q' - names must be unique",
                code="assertion.duplicate-query-name",
            ),
        )


class TestQueryFieldsAreNormalized:
    def test_expect_zero_written_as_a_string_is_zero(self) -> None:
        result = parse_block({"queries": [{"name": "q", "sql": "SELECT 0", "expect": "0"}]})

        assert result.queries == (QueryAssertion(name="q", sql="SELECT 0", expect="0"),)

    def test_expect_empty_is_kept(self) -> None:
        result = parse_block({"queries": [{"name": "q", "sql": "SELECT 1", "expect": "empty"}]})

        assert result.queries[0].expect == "empty"

    def test_an_explicit_warning_severity_is_kept(self) -> None:
        result = parse_block(
            {"queries": [{"name": "q", "sql": "SELECT 0", "expect": 0, "severity": "warning"}]},
        )

        assert result.queries[0].severity == "warning"

    def test_an_explicit_error_severity_is_kept(self) -> None:
        result = parse_block(
            {"queries": [{"name": "q", "sql": "SELECT 0", "expect": 0, "severity": "error"}]},
        )

        assert result.queries[0].severity == "error"


class TestADuplicateNameDropsOnlyTheDuplicate:
    def test_queries_after_a_duplicate_are_kept(self) -> None:
        first = {"name": "q", "sql": "SELECT 0", "expect": 0}
        later = {"name": "r", "sql": "SELECT 1", "expect": "empty"}

        result = parse_block({"queries": [first, first, later]})

        assert [query.name for query in result.queries] == ["q", "r"]
