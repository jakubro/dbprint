"""`validate_print`'s `on_table` pass identity and per-table findings attribution."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from dbprint.conformance import ValidationTick, validate_print
from dbprint.conformance.progress import VALIDATION_PASSES


EXAMPLE = (
    Path(__file__).resolve().parents[2] / "docs/format/v1/examples/production/prints/production"
)


@pytest.fixture
def print_dir(tmp_path: Path) -> Path:
    """Writable copy of the reference example."""

    dst = tmp_path / "production"
    shutil.copytree(EXAMPLE, dst)

    return dst


def _break_reciprocity(print_dir: Path) -> None:
    """The reciprocity mutation from `test_negative_cases.py` - one Issue on `seedbank.collector`."""

    target = print_dir / "seedbank/collector/relationships.yaml"
    data = yaml.safe_load(target.read_text())
    data["referenced_by"].append(
        {
            "column": ["collector_id"],
            "referencer_table": "seedbank.germination_by_taxon_mv",
            "referencer_column": ["no_such_column_id"],
            "on_delete": "NO ACTION",
            "on_update": "NO ACTION",
            "detection": "declared",
        },
    )
    target.write_text(yaml.safe_dump(data, sort_keys=False))


def test_validate_print_positional_call_is_unaffected(print_dir: Path) -> None:
    """SPEC 6.7's normative call - one positional argument, no on_table at all."""

    issues = validate_print(print_dir)
    assert isinstance(issues, list)


def test_on_table_fires_once_per_table_per_pass(print_dir: Path) -> None:
    manifest = yaml.safe_load((print_dir / "manifest.yaml").read_text())
    total_tables = len(manifest["tables"])
    ticks: list[ValidationTick] = []

    validate_print(print_dir, on_table=ticks.append)

    assert len(ticks) == len(VALIDATION_PASSES) * total_tables

    # Ticks arrive pass-by-pass, in VALIDATION_PASSES order, each pass covering every table once.
    for pass_index, pass_name in enumerate(VALIDATION_PASSES, start=1):
        pass_ticks = ticks[(pass_index - 1) * total_tables : pass_index * total_tables]
        assert {t.pass_name for t in pass_ticks} == {pass_name}
        assert {t.pass_index for t in pass_ticks} == {pass_index}
        assert all(t.pass_total == len(VALIDATION_PASSES) for t in pass_ticks)
        assert [t.index for t in pass_ticks] == list(range(1, total_tables + 1))
        assert all(t.total == total_tables for t in pass_ticks)


def test_findings_is_none_until_the_last_pass(print_dir: Path) -> None:
    ticks: list[ValidationTick] = []
    validate_print(print_dir, on_table=ticks.append)

    last_pass_index = len(VALIDATION_PASSES)
    assert all(t.findings is None for t in ticks if t.pass_index != last_pass_index)
    assert all(t.findings is not None for t in ticks if t.pass_index == last_pass_index)


def test_findings_count_matches_the_tables_own_returned_issues(print_dir: Path) -> None:
    """A cross-check between two independent outputs of one call, not a re-derived formula."""

    _break_reciprocity(print_dir)

    manifest = yaml.safe_load((print_dir / "manifest.yaml").read_text())
    table_dirs = {fqn: entry["path"] for fqn, entry in manifest["tables"].items()}

    ticks: list[ValidationTick] = []
    issues = validate_print(print_dir, on_table=ticks.append)

    final_ticks = {t.fqn: t for t in ticks if t.pass_index == len(VALIDATION_PASSES)}

    for fqn, tick in final_ticks.items():
        table_dir = table_dirs[fqn]
        expected = sum(
            1 for i in issues if i.path == table_dir or i.path.startswith(table_dir + "/")
        )
        assert tick.findings == expected, fqn

    collector_findings = final_ticks["seedbank.collector"].findings
    assert collector_findings is not None
    assert collector_findings >= 1
    assert "relationships.broken-reciprocity" in {i.code for i in issues}
