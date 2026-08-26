"""`committed_print` - the shipped print, copied per test so nothing is shared."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_it_yields_the_shipped_print(committed_print: Path) -> None:
    manifest = yaml.safe_load((committed_print / "production" / "manifest.yaml").read_text())

    assert manifest["connection"] == "production"
    assert "seedbank.accession" in manifest["tables"]


def test_a_test_may_tamper_with_its_own_copy(committed_print: Path) -> None:
    target = committed_print / "production" / "manifest.yaml"
    target.write_text("format_version: 1\ntables: {}\n")

    assert yaml.safe_load(target.read_text())["tables"] == {}


def test_the_next_test_sees_the_shipped_tree_again(committed_print: Path) -> None:
    """Runs after the tamper above; a shared copy would carry that edit into this one."""

    manifest = yaml.safe_load((committed_print / "production" / "manifest.yaml").read_text())

    assert "seedbank.accession" in manifest["tables"]


def test_it_is_not_the_committed_tree_itself(committed_print: Path) -> None:
    """A test writing into the repository's own copy would corrupt what the package ships."""

    assert "examples" not in committed_print.parts
