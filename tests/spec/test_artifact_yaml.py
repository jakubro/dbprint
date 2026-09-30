"""The shared artifact loader parses like PyYAML's pure-Python safe loader, and only its parser differs."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
import yaml.scanner
from click.testing import CliRunner

from dbprint.cli.main import main
from dbprint.conformance.yaml_utils import SourcedFloat, SourcedInt, load_yaml
from dbprint.spec import artifact_yaml


_EXAMPLES = Path(__file__).resolve().parents[2] / "docs/format/v1/examples"
_PROJECT_YAML = "connections:\n  production:\n    adapter: postgres\n    output: prints\n"
_CONTEXT = ["context", "arboretum.seedbank.accession"]


@pytest.mark.parametrize(
    "path",
    sorted(_EXAMPLES.rglob("*.yaml")),
    ids=lambda p: p.relative_to(_EXAMPLES).as_posix(),
)
def test_every_shipped_artifact_loads_as_the_pure_python_parser_reads_it(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert artifact_yaml.load(text) == yaml.load(text, Loader=yaml.SafeLoader)


def test_a_parse_error_is_pyyamls_own() -> None:
    with pytest.raises(yaml.YAMLError):
        artifact_yaml.load("columns: [unclosed\n")


def test_a_sourced_number_keeps_the_text_it_was_written_as(tmp_path: Path) -> None:
    path = tmp_path / "statistics.yaml"
    path.write_text("wide: 12345678901234567890.123\ncount: 0x1F\n", encoding="utf-8")

    loaded = load_yaml(path)

    assert isinstance(loaded["wide"], SourcedFloat)
    assert loaded["wide"].source == "12345678901234567890.123"
    assert isinstance(loaded["count"], SourcedInt)
    assert (loaded["count"], loaded["count"].source) == (31, "0x1F")


def test_context_parses_no_artifact_with_the_pure_python_scanner(
    tmp_path: Path,
    committed_print: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".dbprint.yaml").write_text(_PROJECT_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    scanned: set[str] = set()
    check_token = yaml.scanner.Scanner.check_token

    def recording(self: Any, *choices: type) -> bool:
        scanned.add(self.buffer.rstrip("\0"))

        return check_token(self, *choices)

    monkeypatch.setattr(yaml.scanner.Scanner, "check_token", recording)
    result = CliRunner().invoke(main, _CONTEXT)

    assert result.exit_code == 0, result.output
    assert "# Table: arboretum.seedbank.accession" in result.output
    assert scanned == {_PROJECT_YAML}


def test_without_libyaml_every_read_falls_back_to_the_pure_python_parser(
    tmp_path: Path,
    committed_print: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".dbprint.yaml").write_text(_PROJECT_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with_libyaml = CliRunner().invoke(main, _CONTEXT)

    with _libyaml_removed():
        without_libyaml = CliRunner().invoke(main, _CONTEXT)
        fallback_mro = artifact_yaml.ArtifactLoader.__mro__

    assert yaml.SafeLoader in fallback_mro
    assert without_libyaml.exit_code == 0, without_libyaml.output
    assert without_libyaml.output == with_libyaml.output


@contextmanager
def _libyaml_removed() -> Iterator[None]:
    c_loader = yaml.CSafeLoader
    del yaml.CSafeLoader
    importlib.reload(artifact_yaml)

    try:
        yield
    finally:
        yaml.CSafeLoader = c_loader
        importlib.reload(artifact_yaml)
