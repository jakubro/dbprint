"""The adapter/engine convergence contract: mismatched type spellings produce non-conformant
prints, and an unnamed type still classifies by what the adapter measured (SPEC 3.1).
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dbprint.spec.classification import (
    base_type,
    classify,
    compute_candidate_key_exception,
    compute_cardinality_ratio,
    compute_fanout_avg,
    has_calendar_component,
    has_day_resolution,
    is_array_type,
    is_boolean_type,
    is_candidate_key,
    is_integer_type,
    is_nullable_type,
    is_recognised_type,
    is_string_like_type,
    is_temporal_type,
    map_types,
    record_members,
)


_THRESHOLD = 50


def test_datetime_mid_cardinality_is_temporal() -> None:
    result = classify(
        "datetime",
        cardinality=60,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "temporal"


def test_year_mid_cardinality_is_temporal() -> None:
    result = classify(
        "year",
        cardinality=60,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "temporal"


def test_datetime_full_cardinality_is_temporal() -> None:
    """Uniqueness is not a classification (SPEC 4.2) - a unique datetime stays temporal."""

    result = classify(
        "datetime",
        cardinality=1000,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "temporal"


def test_datetime_low_cardinality_is_categorical() -> None:
    result = classify(
        "datetime",
        cardinality=5,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "categorical"


def test_postgres_timestamp_unaffected() -> None:
    result = classify(
        "timestamp without time zone",
        cardinality=60,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "temporal"


def test_mysql_unsigned_integer_is_numeric() -> None:
    """MySQL 8.0.19+ reports `column_type` with no display width: `bigint unsigned`."""

    result = classify(
        "bigint unsigned",
        cardinality=1000,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "numeric"


def test_mysql_unsigned_zerofill_integer_is_numeric() -> None:
    result = classify(
        "int unsigned zerofill",
        cardinality=1000,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "numeric"


def test_duckdbs_unsigned_and_hugeint_family_is_numeric() -> None:
    for sql_type in ("hugeint", "ubigint", "uinteger", "usmallint", "utinyint"):
        result = classify(sql_type, 1000, False, _THRESHOLD)
        assert result == "numeric", (sql_type, result)


def test_bigquerys_bignumeric_is_numeric() -> None:
    result = classify("BIGNUMERIC", 1000, False, _THRESHOLD)
    assert result == "numeric"


def test_redshifts_super_is_json() -> None:
    result = classify("super", 1000, False, _THRESHOLD)
    assert result == "json"


def test_mysqls_dec_and_fixed_synonyms_are_numeric() -> None:
    """Both are pure DDL synonyms for DECIMAL; MariaDB's information_schema normalizes them
    away before an adapter ever sees the string, but the adapter's own tuple names them.
    """

    for sql_type in ("dec", "fixed"):
        result = classify(sql_type, 1000, False, _THRESHOLD)
        assert result == "numeric", (sql_type, result)


def test_a_measured_column_of_an_unnamed_type_is_text() -> None:
    """A Postgres `inet` column: no rule names it, but the adapter measured it."""

    result = classify(
        "inet",
        cardinality=1000,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "text"


def test_an_unmeasured_column_of_an_unnamed_type_is_unsupported() -> None:
    """The same type name, with no cardinality: the adapter declined to profile it."""

    result = classify(
        "inet",
        cardinality=None,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "unsupported"


def test_a_genuinely_unsupported_type_stays_unsupported_even_if_measured() -> None:
    """`map` is on the format's own list; a stray cardinality does not rescue it."""

    result = classify(
        "map(varchar, integer)",
        cardinality=1000,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "unsupported"


def test_an_unqueried_column_of_an_unnamed_type_is_text() -> None:
    """SPEC 3.3: under `catalog_only`, an ordinary type never falls to `unsupported`."""

    result = classify(
        "inet",
        cardinality=None,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
        catalog_only=True,
    )
    assert result == "text"


def test_an_unqueried_composite_column_still_reaches_unsupported() -> None:
    """The genuinely unsupported set - array, composite - is matched first either way."""

    result = classify(
        "struct(a integer)",
        cardinality=None,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
        catalog_only=True,
    )
    assert result == "unsupported"


def test_an_unqueried_declared_fk_column_is_foreign_key_candidate() -> None:
    """A declared or naming-inferred FK is a catalog-derived fact, unaffected by the marker."""

    result = classify(
        "bigint",
        cardinality=None,
        has_declared_fk=True,
        enumeration_threshold=_THRESHOLD,
        catalog_only=True,
    )
    assert result == "foreign_key_candidate"


def test_an_unqueried_column_never_classifies_categorical() -> None:
    """`categorical` needs a `cardinality` the marker forbids - unreachable, not just unlikely."""

    result = classify(
        "integer",
        cardinality=None,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
        catalog_only=True,
    )
    assert result != "categorical"


def test_base_type_strips_mysql_unsigned() -> None:
    assert base_type("bigint unsigned") == "bigint"


def test_base_type_strips_mysql_unsigned_zerofill() -> None:
    assert base_type("int(10) unsigned zerofill") == "int"


def test_base_type_strips_precision_wherever_it_falls() -> None:
    """`timestamp(3) with time zone`: the qualifier follows the precision group."""

    assert base_type("timestamp(3) with time zone") == "timestamp with time zone"


def test_base_type_strips_trailing_precision() -> None:
    assert base_type("numeric(10,2)") == "numeric"


def test_is_nullable_type_matches_a_bare_wrapper() -> None:
    assert is_nullable_type("Nullable(Int32)") is True


def test_is_nullable_type_matches_nullable_nested_under_another_wrapper() -> None:
    """ClickHouse's canonical nullable-low-cardinality spelling nests `Nullable` inside
    `LowCardinality`, which an anchored `^Nullable\\(...\\)$` test never reaches.
    """

    assert is_nullable_type("LowCardinality(Nullable(String))") is True


def test_is_nullable_type_is_false_for_a_non_nullable_wrapped_type() -> None:
    assert is_nullable_type("LowCardinality(String)") is False


def test_is_nullable_type_is_false_for_an_unwrapped_type() -> None:
    assert is_nullable_type("String") is False


def test_base_type_does_not_confuse_precision_for_a_qualifier() -> None:
    """`double precision` carries no MySQL qualifier substring collision."""

    assert base_type("double precision") == "double precision"


_COMPOSITE_SPELLINGS = (
    "STRUCT(email VARCHAR, address STRUCT(city VARCHAR))",
    "STRUCT(a DECIMAL(10,2))",
    "STRUCT(tier VARCHAR, box STRUCT(n INTEGER))",
    "MAP(VARCHAR, DECIMAL(10,2))",
    "FLOAT[3]",
    "DECIMAL(10,2)[2]",
    "INTEGER[2][2]",
    "INTEGER[][2]",
    "UNION(n INTEGER, s VARCHAR)",
    "Array(Nullable(String))",
    "Array(Decimal(10, 2))",
    "Map(String, Array(String))",
    "Tuple(String, Nullable(UInt8))",
    "Nested(a Nullable(String))",
    "AggregateFunction(quantiles(0.5, 0.9), UInt64)",
    "array<string>",
    "map<string,int>",
    "struct<a:int>",
    "array<decimal(10,2)>",
    "ARRAY<STRING>",
    "STRUCT<a INT64, b STRUCT<c STRING>>",
    'STRUCT("a(b" INTEGER)',
)

_UNCHANGED = {
    "STRUCT(a INTEGER)": "unsupported",
    "INTEGER[]": "unsupported",
    "MAP(VARCHAR, INTEGER)": "unsupported",
    "DECIMAL(10,2)": "numeric",
    "TIMESTAMP WITH TIME ZONE": "temporal",
    "LowCardinality(Nullable(String))": "text",
    "DateTime64(3, 'UTC')": "temporal",
    "character varying(20)": "text",
    "timestamp(3) with time zone": "temporal",
    "bigint unsigned": "numeric",
}


def test_a_composite_whatever_its_nesting_is_unsupported_when_measured() -> None:
    for spelling in _COMPOSITE_SPELLINGS:
        assert classify(spelling, 3, False, _THRESHOLD) == "unsupported", spelling
        assert classify(spelling, 500, False, _THRESHOLD) == "unsupported", spelling


def test_a_composite_whatever_its_nesting_is_unsupported_from_the_catalog_alone() -> None:
    for spelling in _COMPOSITE_SPELLINGS:
        assert classify(spelling, None, False, _THRESHOLD, catalog_only=True) == "unsupported", (
            spelling
        )


def test_scalar_spellings_classify_as_before() -> None:
    for spelling, expected in _UNCHANGED.items():
        assert classify(spelling, 500, False, _THRESHOLD) == expected, spelling


def test_base_type_strips_every_nesting_level_and_keeps_the_qualifier() -> None:
    assert base_type("STRUCT(a STRUCT(b INTEGER))") == "struct"
    assert base_type("array<struct<a:int>>") == "array"
    assert base_type("timestamp(3) with time zone") == "timestamp with time zone"


def test_an_array_suffix_is_read_at_depth_zero_only() -> None:
    assert is_array_type("INTEGER[2][2]") is True
    assert is_array_type("character varying(20)[]") is True
    assert is_array_type("STRUCT(a INTEGER[3])") is False
    assert is_array_type("INTEGER") is False


def test_mysqls_one_digit_tinyint_is_the_only_boolean_width() -> None:
    assert (is_boolean_type("tinyint(1)"), is_boolean_type("tinyint(4)")) == (True, False)


def test_a_spelling_no_shared_table_names_is_not_recognised() -> None:
    assert not is_recognised_type("seedtag")
    assert is_recognised_type("STRUCT(a INTEGER)")


def test_nanosecond_clock_types_take_the_clock_path() -> None:
    assert is_temporal_type("TIME_NS") and not has_calendar_component("TIME_NS")
    assert not has_day_resolution("Time64(3)")


class TestCandidateKeyEdges:
    def test_no_rows_scanned_is_a_zero_ratio(self) -> None:
        assert compute_cardinality_ratio(0, 0) == 0.0

    def test_a_single_distinct_value_can_clear_the_threshold(self) -> None:
        assert is_candidate_key(1, 1.0) is True
        assert is_candidate_key(0, 1.0) is False

    @pytest.mark.parametrize(
        ("cardinality", "ratio", "method", "rows_scanned", "null_count", "expected"),
        [
            (9, 1.0, "exact", 10, 0, None),
            (5, 1.0, "estimated", 5, 0, None),
            (9, 0.99995, "exact", 10, 1, None),
            (9, 0.99995, "exact", 10, 0, "measured_duplicates"),
            (9, 0.99995, "estimated", 10, 1, "estimated"),
        ],
    )
    def test_the_exception_marker(
        self,
        cardinality: int,
        ratio: float,
        method: str,
        rows_scanned: int,
        null_count: int,
        expected: str | None,
    ) -> None:
        got = compute_candidate_key_exception(cardinality, ratio, method, rows_scanned, null_count)

        assert got == expected


class TestTypeFamilies:
    @pytest.mark.parametrize(
        ("sql_type", "cardinality", "catalog_only", "expected"),
        [
            ("BOOLEAN", 2, False, "boolean"),
            ("VARCHAR", 50, False, "categorical"),
            ("VARCHAR", 51, False, "text"),
            ("VARCHAR", None, False, "text"),
            ("mystery_type", None, True, "text"),
            ("mystery_type", None, False, "unsupported"),
        ],
    )
    def test_classify_at_its_boundaries(
        self,
        sql_type: str,
        cardinality: int | None,
        catalog_only: bool,
        expected: str,
    ) -> None:
        assert classify(sql_type, cardinality, False, 50, catalog_only=catalog_only) == expected

    @pytest.mark.parametrize(
        ("sql_type", "day", "calendar"),
        [
            ("timestamp", True, True),
            ("date", False, True),
            ("time", False, False),
            ("text", False, False),
        ],
    )
    def test_day_and_calendar_components(self, sql_type: str, day: bool, calendar: bool) -> None:
        assert (has_day_resolution(sql_type), has_calendar_component(sql_type)) == (day, calendar)

    @pytest.mark.parametrize(
        ("sql_type", "string_like"),
        [
            ("VARCHAR(20)", True),
            ("mystery_type", True),
            ("INTEGER", False),
            ("BOOLEAN", False),
            ("tinyint(1)", False),
            ("JSONB", False),
            ("TIMESTAMP", False),
            ("TEXT[]", False),
            ("BYTEA", False),
        ],
    )
    def test_string_like_is_decided_by_elimination(self, sql_type: str, string_like: bool) -> None:
        assert is_string_like_type(sql_type) is string_like

    @pytest.mark.parametrize(
        ("sql_type", "recognised"),
        [("BOOLEAN", True), ("tinyint(1)", True), ("TEXT[]", True), ("mystery_type", False)],
    )
    def test_recognised_by_some_table(self, sql_type: str, recognised: bool) -> None:
        assert is_recognised_type(sql_type) is recognised
        assert is_boolean_type("BOOLEAN") is True

    @pytest.mark.parametrize(
        ("sql_type", "array"),
        [
            ("TEXT[]", True),
            ("integer[] ", True),
            ('ROW("a(b" INT)[]', True),
            ("STRUCT<x INT[]>", False),
        ],
    )
    def test_an_array_suffix_is_read_at_depth_zero(self, sql_type: str, array: bool) -> None:
        assert is_array_type(sql_type) is array


@pytest.mark.parametrize(
    ("sql_type", "cardinality", "has_declared_fk", "expected"),
    [
        ("bytea", 1000, False, "binary"),
        ("BINARY(16)", 1000, True, "foreign_key_candidate"),
        ("longblob", 3, False, "categorical"),
        ("VARBYTE(64)", None, False, "binary"),
    ],
)
def test_a_binary_type_is_measured_and_lands_on_its_own_branch(
    sql_type: str,
    cardinality: int | None,
    has_declared_fk: bool,
    expected: str,
) -> None:
    result = classify(
        sql_type,
        cardinality=cardinality,
        has_declared_fk=has_declared_fk,
        enumeration_threshold=_THRESHOLD,
        catalog_only=cardinality is None,
    )

    assert result == expected


def test_a_binary_type_is_not_string_like() -> None:
    assert not is_string_like_type("bytea")


@pytest.mark.parametrize(
    ("sql_type", "expected"),
    [
        ("SimpleAggregateFunction(sum, UInt64)", "uint64"),
        ("SimpleAggregateFunction(anyLast, LowCardinality(Nullable(String)))", "string"),
        ("SimpleAggregateFunction(max, DateTime64(3, 'UTC'))", "datetime64"),
        ("SimpleAggregateFunction(sumMap, Array(UInt8), Array(UInt64))", "array"),
        ("SimpleAggregateFunction(sum)", "simpleaggregatefunction"),
        ("AggregateFunction(uniq, UInt64)", "aggregatefunction"),
    ],
)
def test_a_simple_aggregate_function_reads_as_the_type_it_stores(
    sql_type: str,
    expected: str,
) -> None:
    assert base_type(sql_type) == expected


@pytest.mark.parametrize(
    ("sql_type", "nullable"),
    [
        ("SimpleAggregateFunction(anyLast, Nullable(String))", True),
        ("SimpleAggregateFunction(anyLast, LowCardinality(Nullable(String)))", True),
        ("SimpleAggregateFunction(anyLast, String)", False),
        ("AggregateFunction(anyLast, Nullable(String))", False),
    ],
)
def test_nullability_follows_the_stored_type_not_an_aggregate_state(
    sql_type: str,
    nullable: bool,
) -> None:
    assert is_nullable_type(sql_type) is nullable


@pytest.mark.parametrize(
    ("sql_type", "expected"),
    [
        (
            'STRUCT(a VARCHAR, "b c" STRUCT(d INTEGER, e DECIMAL(9, 2)))',
            ("struct", [("a", "VARCHAR"), ("b c", "STRUCT(d INTEGER, e DECIMAL(9, 2))")]),
        ),
        ("UNION(n INTEGER, s VARCHAR)", ("union", [("n", "INTEGER"), ("s", "VARCHAR")])),
        ("STRUCT<a STRING, b ARRAY<INT64>>", ("struct", [("a", "STRING"), ("b", "ARRAY<INT64>")])),
        (
            "struct<a:string,b:map<string,int>>",
            ("struct", [("a", "string"), ("b", "map<string,int>")]),
        ),
        ("Nullable(Tuple(a String, b UInt8))", ("struct", [("a", "String"), ("b", "UInt8")])),
        ("Tuple(String, Array(UInt8))", ("struct", [("1", "String"), ("2", "Array(UInt8)")])),
        (
            "Variant(String, Array(UInt64))",
            ("union", [("String", "String"), ("Array(UInt64)", "Array(UInt64)")]),
        ),
        ("STRUCT(a INTEGER)[]", None),
        ("VARCHAR", None),
    ],
)
def test_a_declared_record_names_its_members_in_its_own_spelling(
    sql_type: str,
    expected: tuple[str, list[tuple[str, str]]] | None,
) -> None:
    assert record_members(sql_type) == expected


@pytest.mark.parametrize(
    ("sql_type", "expected"),
    [
        ("MAP(VARCHAR, MAP(VARCHAR, INTEGER))", ("VARCHAR", "MAP(VARCHAR, INTEGER)")),
        ("map<string,array<int>>", ("string", "array<int>")),
        ("Map(String, Tuple(a UInt8, b String))", ("String", "Tuple(a UInt8, b String)")),
        ("map(VARCHAR(16777216), NUMBER(38,0))", ("VARCHAR(16777216)", "NUMBER(38,0)")),
        ("MAP(VARCHAR, INTEGER)[]", None),
        ("MAP", None),
        ("hstore", None),
    ],
)
def test_a_declared_map_names_its_key_and_value_types(
    sql_type: str,
    expected: tuple[str, str] | None,
) -> None:
    assert map_types(sql_type) == expected


class TestComputeFanoutAvg:
    """Rows per distinct key among the referencing rows that carry one (SPEC 2.3.10)."""

    def test_null_referencing_rows_are_not_counted(self) -> None:
        assert compute_fanout_avg(1000, 990, 2) == 5.0

    def test_it_rounds_to_six_places(self) -> None:
        assert compute_fanout_avg(300, 4, 12) == 24.666667

    def test_an_estimated_row_count_below_the_keys_floors_at_one(self) -> None:
        assert compute_fanout_avg(10, 4, 8) == 1.0

    @given(st.integers(1, 10**9), st.integers(0, 10**9), st.integers(1, 10**6))
    def test_it_never_falls_below_one_row_per_key(
        self,
        row_count: int,
        null_count: int,
        cardinality: int,
    ) -> None:
        assert compute_fanout_avg(row_count, null_count, cardinality) >= 1.0

    @given(st.integers(1, 10**6), st.integers(0, 10**9), st.integers(0, 10**9))
    def test_it_never_exceeds_the_ratio_that_counts_every_row(
        self,
        cardinality: int,
        keyed: int,
        nulls: int,
    ) -> None:
        row_count = cardinality + keyed + nulls

        assert compute_fanout_avg(row_count, nulls, cardinality) <= round(
            row_count / cardinality,
            6,
        )


class TestIsIntegerType:
    @pytest.mark.parametrize(
        "sql_type",
        [
            "integer",
            "BIGINT",
            "int(11) unsigned",
            "Nullable(Int64)",
            "UInt8",
            "HUGEINT",
            "NUMBER(38,0)",
            "numeric(10)",
            "DECIMAL(12, 0)",
            "Decimal32(0)",
        ],
    )
    def test_a_type_holding_only_whole_numbers(self, sql_type: str) -> None:
        assert is_integer_type(sql_type)

    @pytest.mark.parametrize(
        "sql_type",
        [
            "decimal",
            "numeric",
            "NUMBER",
            "NUMBER(38,2)",
            "Decimal32(2)",
            "double precision",
            "real",
        ],
    )
    def test_a_type_that_can_hold_a_fraction(self, sql_type: str) -> None:
        assert not is_integer_type(sql_type)
