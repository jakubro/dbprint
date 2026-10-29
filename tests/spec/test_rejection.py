"""SPEC 2.7.1: a rejected inferred field and its supporting keys are withheld; nothing else is."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from dbprint.spec.rejection import RejectableField, rejected_fields, without_rejected


_COLUMN: dict[str, Any] = {
    "sql_type": "varchar",
    "classification": "text",
    "cardinality_ratio": 0.98,
    "inferred": {
        "looks_like": "email",
        "sampled": 50,
        "matched": 49,
        "candidate_key": True,
        "candidate_key_exception": "near_unique",
        "sensitivity": "contact",
        "epoch_unit": "seconds",
    },
}


@pytest.mark.parametrize(
    ("field", "left"),
    [
        (
            "candidate_key",
            {
                "looks_like": "email",
                "sampled": 50,
                "matched": 49,
                "sensitivity": "contact",
                "epoch_unit": "seconds",
            },
        ),
        (
            "looks_like",
            {
                "candidate_key": True,
                "candidate_key_exception": "near_unique",
                "sensitivity": "contact",
                "epoch_unit": "seconds",
            },
        ),
        (
            "sensitivity",
            {
                "looks_like": "email",
                "sampled": 50,
                "matched": 49,
                "candidate_key": True,
                "candidate_key_exception": "near_unique",
                "epoch_unit": "seconds",
            },
        ),
        (
            "epoch_unit",
            {
                "looks_like": "email",
                "sampled": 50,
                "matched": 49,
                "candidate_key": True,
                "candidate_key_exception": "near_unique",
                "sensitivity": "contact",
            },
        ),
    ],
)
def test_a_rejection_withholds_its_field_and_the_keys_beside_it(
    field: RejectableField,
    left: dict[str, Any],
) -> None:
    reduced = without_rejected(_COLUMN, {field})

    assert reduced["inferred"] == left
    assert {k: v for k, v in reduced.items() if k != "inferred"} == {
        "sql_type": "varchar",
        "classification": "text",
        "cardinality_ratio": 0.98,
    }


def test_a_rejected_near_miss_withholds_its_share() -> None:
    column = {
        "classification": "text",
        "inferred": {
            "looks_like_candidate": "email",
            "looks_like_candidate_share": 0.7,
            "sensitivity": "contact",
        },
    }

    assert without_rejected(column, {"looks_like_candidate"})["inferred"] == {
        "sensitivity": "contact",
    }


def test_an_emptied_inferred_block_is_dropped() -> None:
    column = {"classification": "temporal", "inferred": {"candidate_key": True}}

    assert without_rejected(column, {"candidate_key"}) == {"classification": "temporal"}


def test_nothing_to_withhold_returns_the_same_column() -> None:
    column = {"classification": "categorical", "inferred": {"sensitivity": "contact"}}

    assert without_rejected(column, {"epoch_unit", "looks_like"}) is column
    assert without_rejected(column, set()) is column
    assert without_rejected({"classification": "numeric"}, {"candidate_key"}) == {
        "classification": "numeric",
    }


def test_the_column_it_reads_is_left_unmutated() -> None:
    before = copy.deepcopy(_COLUMN)

    without_rejected(_COLUMN, {"candidate_key", "looks_like", "sensitivity", "epoch_unit"})

    assert before == _COLUMN


@pytest.mark.parametrize(
    ("entry", "fields"),
    [
        (None, frozenset()),
        ({"note": "no rejection"}, frozenset()),
        ({"rejected": []}, frozenset()),
        ({"rejected": ["candidate_key", "epoch_unit"]}, frozenset({"candidate_key", "epoch_unit"})),
    ],
)
def test_the_rejected_fields_of_an_entry(
    entry: dict[str, Any] | None,
    fields: frozenset[str],
) -> None:
    assert rejected_fields(entry) == fields
