"""Which manifest entries a reader may follow, and which artifacts it may open from one."""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.engine import baseline
from dbprint.spec.artifacts import declared_artifacts, walkable_tables


_ENTRY = {"path": "public/curator", "artifacts": {"ddl": "ddl.sql"}}


@pytest.mark.parametrize(
    ("manifest", "followed"),
    [
        pytest.param({"tables": {"public.curator": "not an entry"}}, [], id="entry-not-a-mapping"),
        pytest.param({"tables": {"public.curator": {"path": 5}}}, [], id="path-not-a-string"),
        pytest.param(
            {"tables": {"public.curator": _ENTRY, "public.specimen_loan": 5}},
            ["public.curator"],
            id="one-of-two-broken",
        ),
        pytest.param({"tables": None}, [], id="tables-empty"),
        pytest.param({}, [], id="no-tables-key"),
        pytest.param(None, [], id="no-manifest"),
        pytest.param(
            {"tables": {"public.curator": {"type": "table"}}},
            ["public.curator"],
            id="no-path",
        ),
    ],
)
def test_an_unusable_entry_drops_only_itself(manifest: Any, followed: list[str]) -> None:
    assert list(walkable_tables(manifest)) == followed


@pytest.mark.parametrize(
    ("entry", "opened"),
    [
        pytest.param({**_ENTRY, "artifacts": 5}, {}, id="artifacts-not-a-map"),
        pytest.param(
            {**_ENTRY, "artifacts": {"ddl": 7, "statistics": "statistics.yaml"}},
            {"statistics": "statistics.yaml"},
            id="one-name-not-a-string",
        ),
        pytest.param({"path": "public/curator"}, {}, id="no-artifacts"),
    ],
)
def test_a_name_that_is_not_a_string_is_never_opened(
    entry: dict[str, Any],
    opened: dict[str, str],
) -> None:
    assert declared_artifacts(entry) == opened


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(["one", "two"], id="sequence"),
        pytest.param("a string", id="scalar"),
        pytest.param({"tables": ["public.curator"]}, id="tables-is-a-sequence"),
    ],
)
def test_a_document_the_walkers_are_not_defined_for_is_refused_upstream(document: Any) -> None:
    assert baseline.manifest_shape_error(document) is not None
