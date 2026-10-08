"""SPEC 2.2.9's substitution primitives: a stable salted digest, a mask, and the 90-day floor."""

from __future__ import annotations

from decimal import Decimal

import pytest

from dbprint.spec.normalization import fold
from dbprint.spec.redaction import apply_redaction_rule, coarsen_day_count, redact_value


@pytest.mark.parametrize(("days", "expected"), [(0, 0), (89, 0), (90, 90), (179, 90), (180, 180)])
def test_a_day_count_floors_to_ninety(days: int, expected: int) -> None:
    assert coarsen_day_count(days) == expected


def test_a_digest_is_stable_across_releases() -> None:
    assert redact_value("alice", "hash", "pepper") == "62d0a49769945fd4"


def test_a_digest_depends_on_the_salt_and_the_value() -> None:
    digests = {
        redact_value("alice", "hash", "pepper"),
        redact_value("alice", "hash", "salt"),
        redact_value("bob", "hash", "pepper"),
    }

    assert len(digests) == 3


def test_an_exact_number_is_digested_as_the_artifact_spells_it() -> None:
    assert redact_value(Decimal("1.50"), "hash", "pepper") == redact_value(
        Decimal("1.5"),
        "hash",
        "pepper",
    )


@pytest.mark.parametrize("salt", [None, "", "   "])
def test_a_digest_without_a_salt_is_refused(salt: str | None) -> None:
    with pytest.raises(
        ValueError,
        match=r"^hash redaction requires a redaction_salt carrying a value$",
    ):
        redact_value("alice", "hash", salt)


def test_a_mask_substitutes_the_placeholder() -> None:
    assert redact_value("alice", "mask", None) == "[redacted]"


def test_withholding_tolerates_a_field_already_absent() -> None:
    column = {"redacted": "mask", "mean": 1.5}

    apply_redaction_rule(column)

    assert column == {"redacted": "mask"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [("  Ab C ", "ab c"), ("\tAb\t", "\tab\t"), ("XY ", "xy")],
)
def test_fold_trims_spaces_only_then_lowercases(text: str, expected: str) -> None:
    assert fold(text) == expected
