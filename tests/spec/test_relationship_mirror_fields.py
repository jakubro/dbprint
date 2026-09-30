"""Every field of both relationships entry schemas is an address or compared across the mirror."""

from __future__ import annotations

from dbprint.conformance.relationships import (
    MIRRORED_FIELDS,
    REFERENCED_BY_ADDRESS,
    REFERS_TO_ADDRESS,
)
from dbprint.conformance.schema_validation import relationships_schema


def test_no_entry_field_is_left_unclassified() -> None:
    defs = relationships_schema()["$defs"]
    refers_to = set(defs["RefersTo"]["properties"])
    referenced_by = set(defs["ReferencedBy"]["properties"])

    assert refers_to - REFERS_TO_ADDRESS - MIRRORED_FIELDS == set()
    assert referenced_by - REFERENCED_BY_ADDRESS - MIRRORED_FIELDS == set()
