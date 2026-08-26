"""SPEC 3.4 and 4.1.6 reserve lists never collide with a name the format already ships.

Each SPEC list is mirrored by hand (as `conformance/statistics.py` mirrors SPEC 2.2.3), so a
name moving between a reserve list and the shipped set must move in the mirror too.
"""

from __future__ import annotations

from typing import get_args

from dbprint.spec.classification import Classification
from dbprint.spec.looks_like import LooksLike
from dbprint.spec.sensitivity import Sensitivity


# SPEC 3.4: reserved for potential future classifications.
_RESERVED_CLASSIFICATIONS = frozenset(
    {"geographic", "monetary", "binary", "array", "composite", "enum"},
)

# SPEC 4.1.6: empty - every name it covers is a shipped `looks_like` pattern.
_RESERVED_LOOKS_LIKE: frozenset[str] = frozenset()


def test_reserved_classifications_are_not_shipped() -> None:
    assert _RESERVED_CLASSIFICATIONS & set(get_args(Classification)) == set()


def test_the_five_shipped_names_are_not_reserved() -> None:
    """All five are shipped patterns and absent from the reserve list."""

    shipped = {"mac_address", "iso8601_duration", "urn", "hex", "latlon"}

    assert shipped <= set(get_args(LooksLike))


def test_no_sensitivity_category_is_a_reserved_classification() -> None:
    """The two axes are unrelated vocabularies; a name should not silently mean both."""

    assert set(get_args(Sensitivity)) & _RESERVED_CLASSIFICATIONS == set()
