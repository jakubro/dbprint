"""Relationship-graph reverse-index and rejected-edge tests.

`resolve()` is pure and table-agnostic, so each case borrows the shipped print's own edge
that best matches the shape under test - self-referential, composite-key, two-referencer,
or an object with no incoming edges at all.
"""

from __future__ import annotations

import copy
from typing import Any

from dbprint.adapters.base import ForeignKeyMeta
from dbprint.engine.relationship_graph import (
    rejected_edges,
    resolve,
    with_human_edges,
    withhold_rejected,
)


def _fk(
    column: tuple[str, ...],
    target_table: str,
    target_column: tuple[str, ...],
    name: str = "fk",
) -> ForeignKeyMeta:
    return ForeignKeyMeta(
        column=column,
        target_table=target_table,
        target_column=target_column,
        on_delete="CASCADE",
        on_update="NO ACTION",
        constraint_name=name,
    )


class TestResolve:
    def test_single_fk_reverses(self) -> None:
        graph = {
            "seedbank.accession": [_fk(("taxon_id",), "seedbank.taxon", ("taxon_id",))],
            "seedbank.taxon": [],
        }
        out = resolve(graph)
        assert out["seedbank.taxon"][0].referencer_table == "seedbank.accession"
        assert out["seedbank.taxon"][0].column == ("taxon_id",)
        assert out["seedbank.taxon"][0].referencer_column == ("taxon_id",)
        assert out["seedbank.accession"] == []

    def test_self_referential_fk(self) -> None:
        graph = {
            "seedbank.taxon": [_fk(("parent_taxon_id",), "seedbank.taxon", ("taxon_id",))],
        }
        out = resolve(graph)
        assert len(out["seedbank.taxon"]) == 1
        entry = out["seedbank.taxon"][0]
        assert entry.referencer_table == "seedbank.taxon"
        assert entry.referencer_column == ("parent_taxon_id",)

    def test_composite_fk(self) -> None:
        graph = {
            "seedbank.accession": [
                _fk(
                    ("vault_id", "shelf_code"),
                    "seedbank.vault",
                    ("vault_id", "shelf_code"),
                ),
            ],
            "seedbank.vault": [],
        }
        out = resolve(graph)
        entry = out["seedbank.vault"][0]
        assert entry.column == ("vault_id", "shelf_code")
        assert entry.referencer_column == ("vault_id", "shelf_code")

    def test_external_target_still_appears_in_output(self) -> None:
        """Target outside the input keyset is recorded; engine decides whether to emit."""

        graph = {
            "public.t": [_fk(("external_id",), "external.other.entities", ("id",))],
        }
        out = resolve(graph)
        assert "external.other.entities" in out
        assert out["external.other.entities"][0].referencer_table == "public.t"

    def test_multiple_referencers_sorted(self) -> None:
        graph = {
            "seedbank.germination_trial": [
                _fk(
                    ("collector_id",),
                    "seedbank.collector",
                    ("collector_id",),
                    "germination_trial_to_collector",
                ),
            ],
            "seedbank.accession": [
                _fk(
                    ("collector_id",),
                    "seedbank.collector",
                    ("collector_id",),
                    "accession_to_collector",
                ),
            ],
            "seedbank.collector": [],
        }
        out = resolve(graph)
        names = [e.referencer_table for e in out["seedbank.collector"]]
        assert names == ["seedbank.accession", "seedbank.germination_trial"]

    def test_empty_graph(self) -> None:
        assert resolve({}) == {}

    def test_table_with_no_incoming_has_empty_list(self) -> None:
        graph = {"seedbank.specimen_image": []}
        out = resolve(graph)
        assert out == {"seedbank.specimen_image": []}


_EDGES: dict[str, Any] = {
    "refers_to": [
        {
            "column": ["herbarium_id"],
            "target_table": "public.herbarium",
            "target_column": ["id"],
            "detection": "declared",
        },
        {
            "column": ["garden_id"],
            "target_table": "public.garden",
            "target_column": ["garden_code"],
            "detection": "inferred",
        },
    ],
    "referenced_by": [
        {
            "column": ["id"],
            "referencer_table": "public.accession",
            "referencer_column": ["sheet_id"],
            "detection": "measured",
        },
    ],
}


def _verdict(column: str, target_table: str, target_column: str) -> dict[str, Any]:
    return {
        "column": [column],
        "target_table": target_table,
        "target_column": [target_column],
        "verdict": "rejected",
    }


class TestWithholdRejected:
    def test_an_own_rejected_edge_is_withheld_and_the_rest_kept(self) -> None:
        rejected = rejected_edges([_verdict("garden_id", "public.garden", "garden_code")])
        shown = withhold_rejected(_EDGES, rejected, {}, "public.sheet")

        assert shown is not None
        assert [e["column"] for e in shown["refers_to"]] == [["herbarium_id"]]
        assert shown["referenced_by"] == _EDGES["referenced_by"]

    def test_a_referencers_rejection_withholds_the_incoming_edge(self) -> None:
        incoming = {("public.accession", ("sheet_id",), "public.sheet", ("id",)): {}}
        shown = withhold_rejected(_EDGES, {}, incoming, "public.sheet")

        assert shown is not None
        assert shown["referenced_by"] == []
        assert len(shown["refers_to"]) == 2

    def test_a_verdict_on_a_declared_edge_withholds_nothing(self) -> None:
        rejected = rejected_edges([_verdict("herbarium_id", "public.herbarium", "id")])

        assert withhold_rejected(_EDGES, rejected, {}, "public.sheet") is _EDGES

    def test_a_verdict_on_another_edge_withholds_nothing(self) -> None:
        rejected = rejected_edges([_verdict("other_id", "public.other", "id")])

        assert withhold_rejected(_EDGES, rejected, {}, "public.sheet") is _EDGES

    def test_the_input_is_never_mutated(self) -> None:
        before = copy.deepcopy(_EDGES)
        rejected = rejected_edges([_verdict("garden_id", "public.garden", "garden_code")])
        withhold_rejected(_EDGES, rejected, {}, "public.sheet")

        assert before == _EDGES


def _authored(column: str, target: str, target_column: str, **extra: Any) -> dict[str, Any]:
    return {"column": [column], "target_table": target, "target_column": [target_column], **extra}


class TestWithHumanEdges:
    """An annotation entry with no verdict that matches no emitted edge is a human-authored edge."""

    def test_an_edge_a_human_added_joins_the_source_tagged_annotated(self) -> None:
        own = [_authored("vault_id", "public.vault", "id", note="the storage vault")]
        shown = with_human_edges(_EDGES, {"public.sheet": own}, "public.sheet")

        assert shown is not None
        assert shown["refers_to"][-1] == {
            "column": ["vault_id"],
            "target_table": "public.vault",
            "target_column": ["id"],
            "detection": "annotated",
            "note": "the storage vault",
        }
        assert shown["referenced_by"] == _EDGES["referenced_by"]

    def test_the_target_sees_it_as_incoming(self) -> None:
        authored = {"public.accession": [_authored("sheet_ref", "public.sheet", "id")]}
        shown = with_human_edges(_EDGES, authored, "public.sheet")

        assert shown is not None
        assert shown["referenced_by"][-1] == {
            "referencer_table": "public.accession",
            "column": ["id"],
            "referencer_column": ["sheet_ref"],
            "detection": "annotated",
        }

    def test_a_note_on_an_emitted_edge_rides_on_it_and_adds_no_edge(self) -> None:
        own = [_authored("herbarium_id", "public.herbarium", "id", note="always set")]
        shown = with_human_edges(_EDGES, {"public.sheet": own}, "public.sheet")

        assert shown is not None
        assert len(shown["refers_to"]) == 2
        assert shown["refers_to"][0]["note"] == "always set"
        assert shown["refers_to"][0]["detection"] == "declared"
        assert "note" not in _EDGES["refers_to"][0]

    def test_a_path_valued_entry_keeps_its_paths_on_both_sides(self) -> None:
        entry = _authored("traits", "public.sheet", "id", path=["sheet_ref"])
        shown = with_human_edges(_EDGES, {"public.sheet": [entry]}, "public.sheet")

        assert shown is not None
        assert shown["refers_to"][-1]["path"] == ["sheet_ref"]
        assert shown["referenced_by"][-1]["referencer_path"] == ["sheet_ref"]

    def test_a_verdict_entry_and_an_unaddressed_one_add_nothing(self) -> None:
        own = [
            _verdict("vault_id", "public.vault", "id"),
            {"column": ["vault_id"], "target_table": "public.vault", "note": "no target column"},
        ]

        assert with_human_edges(_EDGES, {"public.sheet": own}, "public.sheet") is _EDGES

    def test_no_relationships_file_stays_none(self) -> None:
        own = [_authored("vault_id", "public.vault", "id")]

        assert with_human_edges(None, {"public.sheet": own}, "public.sheet") is None
