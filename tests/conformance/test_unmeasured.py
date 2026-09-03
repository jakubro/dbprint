"""SPEC 2.2.4 / 2.2.1's `unmeasured` marker - the absence SPEC 7.2 cannot otherwise explain.

Every other cause SPEC 7.2 lists is a property of the column; a query issued and unanswered is not.
"""

from __future__ import annotations

from typing import Any

from dbprint.conformance.schema_validation import check_statistics
from dbprint.conformance.statistics import check


def _column(**overrides: Any) -> dict[str, Any]:
    """A conformant `temporal` column - the classification with the most REQUIRED fields."""

    col: dict[str, Any] = {
        "sql_type": "TIMESTAMP",
        "nullable": True,
        "classification": "temporal",
        "null_count": 0,
        "null_rate": 0.0,
        "cardinality": 30,
        "cardinality_ratio": 0.5,
        "cardinality_method": "exact",
        "values": [{"value": "2025-12-31T00:00:00Z", "count": 40}],
        "distribution": "uniform",
        "frequencies": {"top": 40, "bottom": 40, "listed": 1, "total": 40},
        "range": {"min": "2025-11-01T00:00:00Z", "max": "2025-12-31T00:00:00Z", "span_days": 60},
        "percentiles": {"p50": "2025-12-01T00:00:00Z"},
        "quantized_count": 60,
        "freshness": {"max_age_days": 1, "classification": "live"},
    }
    col.update(overrides)

    return col


def _file(col: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "format_version": 1,
        "table": "seedbank.accession",
        "type": "table",
        "profiled_at": "2026-01-01T00:00:00Z",
        "row_count": 60,
        "row_count_method": "exact",
        "grain": {"keys": []},
        "columns": {"logged_at": col},
    }
    data.update(overrides)

    return data


def _codes(data: dict[str, Any]) -> list[str]:
    return [i.code for i in check(data, "statistics.yaml", "seedbank.accession")]


def _schema_codes(data: dict[str, Any]) -> list[str]:
    return [i.code for i in check_statistics(data, "statistics.yaml")]


_LOST = [
    "cardinality_method",
    "distribution",
    "frequencies",
    "freshness",
    "percentiles",
    "quantized_count",
    "range",
    "values",
]


class TestADegradedColumnValidates:
    def test_naming_what_a_failed_query_cost_replaces_eight_errors_with_none(self) -> None:
        """Without the marker, eight required-field errors on a print whose producer did nothing."""

        stripped = {k: v for k, v in _column().items() if k not in _LOST}

        assert (
            sorted(_codes(_file(stripped)))
            == ["stats.missing-required-field-for-classification"] * 8
        )

        assert _codes(_file({**stripped, "unmeasured": _LOST})) == []

    def test_a_column_that_measured_everything_carries_no_marker_and_still_validates(self) -> None:
        assert _codes(_file(_column())) == []


class TestTheMarkerCannotBeUsedToLie:
    def test_naming_a_field_the_column_also_emits_is_an_error(self) -> None:
        """A measurement and its own absence are contradictory claims."""

        codes = _codes(_file(_column(unmeasured=["distribution"])))

        assert "stats.unmeasured-names-emitted-field" in codes

    def test_naming_a_field_the_classification_never_required_is_an_error(self) -> None:
        """`mean` is forbidden outside `numeric`, so its absence is already structural - naming it
        would turn the marker into a place to dump every absent field.
        """

        codes = _codes(_file(_column(unmeasured=["mean"])))

        assert "stats.unmeasured-names-unrequired-field" in codes

    def test_a_forbidden_field_stays_forbidden_when_named(self) -> None:
        """Naming it must not smuggle it past the forbidden half of the matrix."""

        codes = _codes(_file(_column(mean=1.0, unmeasured=["mean"])))

        assert "stats.unmeasured-names-emitted-field" in codes


class TestTheTableLevelTwin:
    def test_naming_a_block_the_file_also_emits_is_an_error(self) -> None:
        data = _file(
            _column(),
            physical_layout={"mechanism": "partition", "keys": [{"expression": "logged_at"}]},
            unmeasured=["physical_layout"],
        )

        assert "stats.unmeasured-names-emitted-block" in _codes(data)

    def test_naming_an_absent_block_validates(self) -> None:
        assert _codes(_file(_column(), unmeasured=["physical_layout"])) == []

    def test_a_failed_null_census_validates_where_an_unmarked_absence_would_not(self) -> None:
        """SPEC 2.2.10 reads an absent block as "no column carries a null", so a file whose
        census failed needs the marker to say otherwise - and gets the same error without it.
        """

        with_nulls = _file(_column(null_count=3, null_rate=0.05, quantized_count=57))

        assert _codes(with_nulls) == ["stats.null-patterns-absent-with-nulls"]
        assert _codes({**with_nulls, "unmeasured": ["null_patterns"]}) == []

    def test_an_unknown_classification_does_not_crash_the_checker(self) -> None:
        """A classification this version does not know is a forward-compat warning (SPEC 5), so
        the marker beside it goes unjudged rather than taking the validator down with it.
        """

        col = {k: v for k, v in _column().items() if k != "distribution"}
        data = _file({**col, "classification": "geospatial", "unmeasured": ["distribution"]})

        assert _codes(data) == []


class TestBothValidatorsAgree:
    """The JSON Schema and the semantic checks run over the same file in one `dbprint check`, so
    a degraded column either passes both or the producer has no shape it can legally write.
    """

    def test_a_degraded_column_passes_the_schema_and_the_semantic_checks(self) -> None:
        stripped = {k: v for k, v in _column().items() if k not in _LOST}
        degraded = _file({**stripped, "unmeasured": _LOST})

        assert _schema_codes(degraded) == []
        assert _codes(degraded) == []

    def test_the_healthy_column_this_file_measures_against_is_itself_conformant(self) -> None:
        assert _schema_codes(_file(_column())) == []

    def test_a_field_absent_without_being_named_is_still_caught(self) -> None:
        """The schema relaxes the whole classification list wherever the marker is present, since
        no keyword can read a name list - so the semantic half is what still catches this.
        """

        stripped = {k: v for k, v in _column().items() if k not in ("range", "distribution")}
        data = _file({**stripped, "unmeasured": ["distribution"]})

        assert _schema_codes(data) == []
        assert _codes(data) == ["stats.missing-required-field-for-classification"]

    def test_a_file_naming_a_table_level_block_passes_both(self) -> None:
        data = _file(
            _column(null_count=3, null_rate=0.05, quantized_count=57),
            unmeasured=["null_patterns"],
        )

        assert _schema_codes(data) == []
        assert _codes(data) == []
