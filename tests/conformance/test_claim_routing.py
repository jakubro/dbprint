"""A claim malformed only once evaluated is unassertable, never a contradiction of the data."""

from __future__ import annotations

from dbprint.conformance.column_annotations import _check_claim
from dbprint.conformance.relationship_annotations import _check_claim as _check_edge_claim


_SOWN_AT = {
    "sql_type": "TIMESTAMP WITH TIME ZONE",
    "nullable": True,
    "classification": "temporal",
    "range": {"min": "2024-03-01T01:00:00Z", "max": "2024-03-05T04:00:00Z"},
}


def _codes(stat: str, raw: object, column: dict) -> list[str]:
    return [i.code for i in _check_claim("t/statistics.annotations.yaml", "c", stat, raw, column)]


def test_a_number_bound_on_a_timestamp_is_unassertable() -> None:
    assert _codes("range.min", {"min": 5}, _SOWN_AT) == ["annotations.claim-unassertable"]


def test_a_number_against_a_bool_is_unassertable() -> None:
    assert _codes("nullable", 1, _SOWN_AT) == ["annotations.claim-unassertable"]


def test_an_offset_bound_the_data_violates_contradicts() -> None:
    codes = _codes("range.max", {"max": "2024-03-05T05:00:00+02:00"}, _SOWN_AT)

    assert codes == ["annotations.claim-contradicts-statistic"]


def test_an_edge_claim_of_the_wrong_type_is_unassertable() -> None:
    edge = {"observed": {"coherent": True}}
    issues = _check_edge_claim("t/relationships.annotations.yaml", "observed.coherent", 1, edge)

    assert [i.code for i in issues] == ["annotations.claim-unassertable"]
