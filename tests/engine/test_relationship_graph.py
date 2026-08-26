"""Relationship-graph reverse-index tests.

`resolve()` is pure and table-agnostic, so each case borrows the shipped print's own edge
that best matches the shape under test - self-referential, composite-key, two-referencer,
or an object with no incoming edges at all.
"""

from __future__ import annotations

from dbprint.adapters.base import ForeignKeyMeta
from dbprint.engine.relationship_graph import resolve


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
