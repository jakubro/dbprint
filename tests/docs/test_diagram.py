"""`diagram.py` - the Mermaid relationship flowchart for one table."""

from __future__ import annotations

from dbprint.docs import diagram


class TestBuild:
    def test_no_relationships_is_none(self) -> None:
        assert diagram.build("public.orphan", {"refers_to": [], "referenced_by": []}, "c") is None

    def test_absent_relationships_block_is_none(self) -> None:
        assert diagram.build("public.orphan", None, "c") is None

    def test_declared_edge_uses_a_solid_arrow(self) -> None:
        relationships = {
            "refers_to": [
                {"column": ["taxon_id"], "target_table": "seedbank.taxon", "detection": "declared"},
            ],
            "referenced_by": [],
        }

        source = diagram.build("seedbank.accession", relationships, "primary")

        assert source is not None
        assert "flowchart LR" in source
        assert '-->|"taxon_id"|' in source
        assert '-.->|"taxon_id"|' not in source

    def test_inferred_edge_uses_a_dashed_arrow(self) -> None:
        relationships = {
            "refers_to": [
                {"column": ["taxon_id"], "target_table": "seedbank.taxon", "detection": "inferred"},
            ],
            "referenced_by": [],
        }

        source = diagram.build("seedbank.accession", relationships, "primary")

        assert source is not None
        assert '-.->|"taxon_id"|' in source

    def test_every_table_gets_a_click_link(self) -> None:
        relationships = {
            "refers_to": [
                {"column": ["taxon_id"], "target_table": "seedbank.taxon", "detection": "declared"},
            ],
            "referenced_by": [
                {
                    "column": ["accession_id"],
                    "referencer_table": "seedbank.germination_trial",
                    "detection": "inferred",
                },
            ],
        }

        source = diagram.build("seedbank.accession", relationships, "primary")

        assert source is not None
        assert "click" in source
        assert "/t/primary/seedbank.accession" in source
        assert "/t/primary/seedbank.taxon" in source
        assert "/t/primary/seedbank.germination_trial" in source

    def test_current_table_carries_the_highlight_class(self) -> None:
        relationships = {
            "refers_to": [
                {"column": ["taxon_id"], "target_table": "seedbank.taxon", "detection": "declared"},
            ],
            "referenced_by": [],
        }

        source = diagram.build("seedbank.accession", relationships, "primary")

        assert source is not None
        assert "classDef current" in source
        assert source.count("class n") >= 1  # the current-table node is classed

    def test_shared_prefix_nests_into_a_subgraph(self) -> None:
        relationships = {
            "refers_to": [
                {"column": ["taxon_id"], "target_table": "seedbank.taxon", "detection": "declared"},
            ],
            "referenced_by": [],
        }

        source = diagram.build("seedbank.accession", relationships, "primary")

        assert source is not None
        assert 'subgraph sg1["seedbank"]' in source
        assert source.count("  end") == 1
