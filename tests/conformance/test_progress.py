"""`validate_print`'s `on_table` pass identity and per-table findings attribution."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from dbprint.conformance import ValidationTick, validate_print
from tests._scripts import REPO_ROOT


EXAMPLE = REPO_ROOT / "docs/format/v1/examples/production/prints/production"


@pytest.fixture
def print_dir(tmp_path: Path) -> Path:
    """Writable copy of the reference example."""

    dst = tmp_path / "production"
    shutil.copytree(EXAMPLE, dst)

    return dst


def _break_reciprocity(print_dir: Path) -> None:
    """The reciprocity mutation from `test_negative_cases.py` - one Issue on `seedbank.collector`."""

    target = print_dir / "arboretum/seedbank/collector/relationships.yaml"
    data = yaml.safe_load(target.read_text())
    data["referenced_by"].append(
        {
            "column": ["collector_id"],
            "referencer_table": "arboretum.seedbank.germination_by_taxon_mv",
            "referencer_column": ["no_such_column_id"],
            "on_delete": "NO ACTION",
            "on_update": "NO ACTION",
            "detection": "declared",
        },
    )
    target.write_text(yaml.safe_dump(data, sort_keys=False))


PASSES = (
    "manifest",
    "artifacts",
    "edge reciprocity",
    "edge arithmetic",
    "annotation keys",
    "column claims",
    "value notes",
    "grain keys",
    "edge verdicts",
    "edge claims",
)


def test_on_table_fires_once_per_table_per_pass(print_dir: Path) -> None:
    ticks: list[ValidationTick] = []

    validate_print(print_dir, on_table=ticks.append)

    assert len(ticks) == 100

    # Ticks arrive pass-by-pass, in execution order, each pass covering all 10 tables once.
    for pass_index, pass_name in enumerate(PASSES, start=1):
        pass_ticks = ticks[(pass_index - 1) * 10 : pass_index * 10]
        assert {t.pass_name for t in pass_ticks} == {pass_name}
        assert {t.pass_index for t in pass_ticks} == {pass_index}
        assert all(t.pass_total == 10 for t in pass_ticks)
        assert [t.index for t in pass_ticks] == list(range(1, 11))
        assert all(t.total == 10 for t in pass_ticks)


def test_findings_is_none_until_the_last_pass(print_dir: Path) -> None:
    ticks: list[ValidationTick] = []
    validate_print(print_dir, on_table=ticks.append)

    assert all(t.findings is None for t in ticks if t.pass_index != 10)
    assert all(t.findings is not None for t in ticks if t.pass_index == 10)


def test_findings_count_attributes_each_issue_to_its_table(print_dir: Path) -> None:
    _break_reciprocity(print_dir)

    ticks: list[ValidationTick] = []
    validate_print(print_dir, on_table=ticks.append)

    findings = {t.fqn: t.findings for t in ticks if t.pass_index == 10}
    assert {fqn: n for fqn, n in findings.items() if n} == {
        "arboretum.fixture.shape_probe": 1,
        "arboretum.seedbank.collector": 2,
        "arboretum.seedbank.taxon": 1,
        "arboretum.seedbank.vault": 1,
    }
    assert len(findings) == 10


def test_severity_names_the_worst_finding_and_is_none_when_clean(print_dir: Path) -> None:
    """A renderer cannot pick a leaf colour from a bare count - error must outrank a table's
    own warnings, and a table with zero findings must carry no severity at all.
    """

    _break_reciprocity(print_dir)

    ticks: list[ValidationTick] = []
    validate_print(print_dir, on_table=ticks.append)

    final_ticks = {t.fqn: t for t in ticks if t.pass_index == 10}

    assert final_ticks["arboretum.seedbank.collector"].severity == "error"
    assert final_ticks["arboretum.seedbank.taxon"].severity == "warning"
    assert final_ticks["arboretum.seedbank.accession"].severity is None
