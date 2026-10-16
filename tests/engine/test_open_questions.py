"""What `open_questions` asks of one table: only what its print measured and nothing answers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.engine import OpenQuestion, open_questions


SEEDBANK = "arboretum/seedbank"
VAULT = "arboretum.seedbank.vault"
ACCESSION = "arboretum.seedbank.accession"
MV = "arboretum.seedbank.germination_by_taxon_mv"


def _root(committed_print: Path) -> Path:
    return committed_print / "production"


def _asked(root: Path, table: str) -> list[OpenQuestion]:
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())

    return open_questions(manifest, root, table)


def _write(root: Path, table: str, name: str, body: dict[str, Any]) -> None:
    leaf = table.rsplit(".", 1)[1]
    (root / SEEDBANK / leaf / name).write_text(yaml.safe_dump(body, sort_keys=False))
    manifest_path = root / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    key = "statistics_annotations" if name.startswith("statistics") else "relationships_annotations"
    manifest["tables"][table]["artifacts"][key] = name
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))


def _columns(body: dict[str, Any]) -> dict[str, Any]:
    return {"format_version": 1, "columns": body}


class TestAnUndocumentedTable:
    def test_asks_for_its_description_then_each_unnoted_code_list(
        self,
        committed_print: Path,
    ) -> None:
        root = _root(committed_print)

        assert _asked(root, VAULT) == [
            OpenQuestion("description", None, (), None, f"{SEEDBANK}/vault/description.md", None),
            OpenQuestion(
                "codes",
                "target_temperature_c",
                (-19.5, -20, -19),
                None,
                f"{SEEDBANK}/vault/statistics.annotations.yaml",
                "columns.target_temperature_c.values",
            ),
            OpenQuestion(
                "codes",
                "opens_at",
                ("07:30:00", "08:00:00"),
                None,
                f"{SEEDBANK}/vault/statistics.annotations.yaml",
                "columns.opens_at.values",
            ),
            OpenQuestion(
                "codes",
                "closes_at",
                ("16:30:00", "17:00:00"),
                None,
                f"{SEEDBANK}/vault/statistics.annotations.yaml",
                "columns.closes_at.values",
            ),
        ]

    def test_a_written_description_answers_its_question(self, committed_print: Path) -> None:
        root = _root(committed_print)
        (root / SEEDBANK / "vault" / "description.md").write_text("One row per vault.\n")
        manifest_path = root / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"][VAULT]["artifacts"]["description"] = "description.md"
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

        assert "description" not in {q.kind for q in _asked(root, VAULT)}


class TestACodeList:
    @pytest.mark.parametrize(
        "answer",
        [
            {"values": [{"value": "07:30:00", "note": "Winter opening."}]},
            {"note": "Opening time, local to the vault's site."},
            {"derived_from": [{"table": VAULT, "column": "closes_at"}]},
        ],
        ids=["value-note", "column-note", "derived-from"],
    )
    def test_is_answered_by_any_meaning(self, committed_print: Path, answer: dict) -> None:
        root = _root(committed_print)
        annotations = {"opens_at": answer}

        if "derived_from" in answer:
            annotations["closes_at"] = {"note": "Closing time, local to the vault's site."}

        _write(root, VAULT, "statistics.annotations.yaml", _columns(annotations))

        assert "opens_at" not in {q.column for q in _asked(root, VAULT)}


class TestAFrequentValue:
    def test_each_unnoted_one_is_asked_and_a_value_note_answers_it(
        self,
        committed_print: Path,
    ) -> None:
        root = _root(committed_print)
        before = [q.values for q in _asked(root, MV) if q.kind == "frequent_value"]
        _write(
            root,
            MV,
            "statistics.annotations.yaml",
            _columns({"total_germinated": {"values": [{"value": 14, "note": "One full tray."}]}}),
        )
        after = [q.values for q in _asked(root, MV) if q.kind == "frequent_value"]

        assert before == [(14,), (12,), (6,), (20,), (7,)]
        assert after == [(12,), (6,), (20,), (7,)]

    def test_a_redacted_column_is_never_asked(self, committed_print: Path) -> None:
        root = _root(committed_print)
        path = root / SEEDBANK / "germination_by_taxon_mv" / "statistics.yaml"
        statistics = yaml.safe_load(path.read_text())
        statistics["columns"]["total_germinated"]["redacted"] = "mask"
        path.write_text(yaml.safe_dump(statistics, sort_keys=False))

        assert "total_germinated" not in {q.column for q in _asked(root, MV)}


def _seed_sentinel_print(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """One table whose `matures_at` holds a far-future date on 40 of 100 rows."""

    root = tmp_path / "prints" / "primary"
    directory = root / "arboretum" / "seedbank" / "lot"
    directory.mkdir(parents=True)
    (directory / "ddl.sql").write_text("CREATE TABLE lot (matures_at date);\n")
    (directory / "description.md").write_text("One row per seed lot.\n")
    (directory / "statistics.yaml").write_text(
        yaml.safe_dump(
            {
                "row_count": 100,
                "columns": {
                    "matures_at": {
                        "sql_type": "date",
                        "classification": "temporal",
                        "nullable": False,
                        "null_count": 0,
                        "null_rate": 0.0,
                        "cardinality": 50,
                        "values": [
                            {"value": "2099-12-31", "count": 40},
                            {"value": "2021-03-04", "count": 2},
                        ],
                    },
                },
            },
        ),
    )
    manifest = {
        "format_version": 1,
        "connection": "primary",
        "tables": {
            "arboretum.seedbank.lot": {
                "type": "table",
                "path": "arboretum/seedbank/lot",
                "artifacts": {
                    "ddl": "ddl.sql",
                    "statistics": "statistics.yaml",
                    "description": "description.md",
                },
                "row_count": 100,
                "columns": 1,
            },
        },
    }

    return root, manifest


class TestADateSentinel:
    def test_is_asked_and_an_unquoted_date_note_answers_it(self, tmp_path: Path) -> None:
        root, manifest = _seed_sentinel_print(tmp_path)
        asked = open_questions(manifest, root, "arboretum.seedbank.lot")
        (root / "arboretum" / "seedbank" / "lot" / "statistics.annotations.yaml").write_text(
            "format_version: 1\ncolumns:\n  matures_at:\n    values:\n"
            "      - value: 2099-12-31\n        note: Not yet scheduled.\n",
        )
        manifest["tables"]["arboretum.seedbank.lot"]["artifacts"]["statistics_annotations"] = (
            "statistics.annotations.yaml"
        )

        assert [(q.kind, q.values) for q in asked] == [("frequent_value", ("2099-12-31",))]
        assert open_questions(manifest, root, "arboretum.seedbank.lot") == []


class TestAnEdge:
    def test_only_measured_and_inferred_outgoing_edges_are_asked(
        self,
        committed_print: Path,
    ) -> None:
        """Accession's declared edges and its annotated `traits` edge are never asked."""

        edges = [q.edge for q in _asked(_root(committed_print), ACCESSION) if q.kind == "edge"]

        assert edges == [
            "taxon_id -> arboretum.seedbank.germination_trial.trial_id (measured)",
            "taxon_id -> arboretum.seedbank.specimen_image.image_id (measured)",
        ]

    @pytest.mark.parametrize(
        "answer",
        [
            {"note": "Coincidence of two id ranges."},
            {"verdict": "rejected", "note": "Coincidence."},
        ],
        ids=["note", "rejected"],
    )
    def test_a_note_or_a_rejection_answers_it(self, committed_print: Path, answer: dict) -> None:
        root = _root(committed_print)
        path = root / SEEDBANK / "accession" / "relationships.annotations.yaml"
        body = yaml.safe_load(path.read_text())
        body["refers_to"].append(
            {
                "column": ["taxon_id"],
                "target_table": "arboretum.seedbank.germination_trial",
                "target_column": ["trial_id"],
                **answer,
            },
        )
        path.write_text(yaml.safe_dump(body, sort_keys=False))
        edges = [q.edge for q in _asked(root, ACCESSION) if q.kind == "edge"]

        assert edges == ["taxon_id -> arboretum.seedbank.specimen_image.image_id (measured)"]
