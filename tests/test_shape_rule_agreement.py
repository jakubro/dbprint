"""The manifest shape rule is implemented twice and must answer alike.

`engine/baseline.py` and `conformance/layout.py` each carry `walkable_tables` and
`declared_artifacts`; the conformance suite must not import the engine, so the duplicate is
pinned here rather than unified. Both are asked only about a document a caller already
cleared, and that boundary is pinned too, so a non-mapping document reads as a missing guard.
"""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.conformance import layout
from dbprint.engine import baseline


_ENTRY = {"path": "public/curator", "artifacts": {"ddl": "ddl.sql"}}

# Ids of the manifests below whose `tables` yields no walkable entry at all - relevant only to
# the artifact-comparison test, which would otherwise loop zero times and pass vacuously.
_NO_WALKABLE_TABLES = {
    "entry-not-a-mapping",
    "path-not-a-string",
    "tables-empty",
    "tables-none-declared",
    "no-tables-key",
}

MANIFESTS = [
    pytest.param({"tables": {"public.curator": "not an entry"}}, id="entry-not-a-mapping"),
    pytest.param({"tables": {"public.curator": {"path": 5}}}, id="path-not-a-string"),
    pytest.param(
        {"tables": {"public.curator": {**_ENTRY, "artifacts": 5}}},
        id="artifacts-not-a-map",
    ),
    pytest.param(
        {"tables": {"public.curator": {**_ENTRY, "artifacts": {"ddl": 7}}}},
        id="artifact-name-not-a-string",
    ),
    pytest.param(
        {"tables": {"public.curator": _ENTRY, "public.specimen_loan": 5}},
        id="one-of-two-broken",
    ),
    pytest.param({"tables": None}, id="tables-empty"),
    pytest.param({"tables": {}}, id="tables-none-declared"),
    pytest.param({}, id="no-tables-key"),
    pytest.param({"tables": {"public.curator": _ENTRY}}, id="well-formed"),
]

WALKABLE_MANIFESTS = [p for p in MANIFESTS if p.id not in _NO_WALKABLE_TABLES]
EMPTY_MANIFESTS = [p for p in MANIFESTS if p.id in _NO_WALKABLE_TABLES]


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_both_copies_follow_the_same_entries(manifest: dict[str, Any]) -> None:
    assert baseline.walkable_tables(manifest) == layout.walkable_tables(manifest)


@pytest.mark.parametrize("manifest", EMPTY_MANIFESTS)
def test_a_manifest_with_no_walkable_table_yields_nothing_to_compare(
    manifest: dict[str, Any],
) -> None:
    """The mirror of the loop below: these are exactly the manifests it would skip silently."""

    assert baseline.walkable_tables(manifest) == {}


@pytest.mark.parametrize("manifest", WALKABLE_MANIFESTS)
def test_both_copies_open_the_same_artifacts(manifest: dict[str, Any]) -> None:
    walkable = baseline.walkable_tables(manifest)
    assert walkable, "this manifest is expected to yield at least one walkable entry"

    for fqn, entry in walkable.items():
        assert baseline.declared_artifacts(entry) == layout.declared_artifacts(entry), fqn


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(["one", "two"], id="sequence"),
        pytest.param("a string", id="scalar"),
        pytest.param({"tables": ["public.curator"]}, id="tables-is-a-sequence"),
    ],
)
def test_a_document_neither_copy_is_asked_about_is_refused_upstream(document: Any) -> None:
    """Neither pair is defined for these, which is why the callers must clear them first."""

    assert baseline.manifest_shape_error(document) is not None
