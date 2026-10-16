"""The packaged `.dbprint.yaml` schema: well-formed, and in agreement with the loader."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
import yaml
from jsonschema import Draft202012Validator

from dbprint.cli.commands.init import PROJECT_TEMPLATE
from dbprint.config import ConfigError, load_project, project_schema
from dbprint.config.project import ADAPTERS
from dbprint.spec.looks_like import LooksLike
from dbprint.spec.sensitivity import Sensitivity
from tests._scripts import REPO_ROOT


ACCEPTED = [
    "connections:\n  w: {adapter: postgres}\n",
    "connections:\n  w:\n    adapter: duckdb\n    tables: {include: [a.*], exclude: [a.b]}\n",
    "connections:\n  w:\n    adapter: postgres\n    run: {parallelism: 4, statement_timeout: 10m}\n",
    "defaults:\n  profile: {freshness: {max_age_days: 0}}\nconnections:\n  w: {adapter: mysql}\n",
    (
        """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - a.b
        min_rows: 5
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
"""
    ),
    "x-shared: {a: 1}\nconnections:\n  w: {adapter: snowflake}\n",
    "connections:\n  w:\n    adapter: postgres\n    profile: {read: {tables: {mode: sample, max_rows: 9}}}\n",
]
REFUSED = [
    "connections: {}\n",
    "connections:\n  w: {adapter: oracle}\n",
    "connections:\n  w: {adapter: postgres, include: [a.*]}\n",
    "connections:\n  w:\n    adapter: postgres\n    run: {statement_timeout: 1h30m}\n",
    "connections:\n  w:\n    adapter: postgres\n    rules: [{match: {tables: [a.b]}, profile: {}}]\n",
    "connections:\n  w:\n    adapter: postgres\n    profile: {read: {tables: {mode: filter, fraction: 0.5}}}\n",
    "connections:\n  w:\n    adapter: postgres\n    profile: {read: {views: {mode: sample}}}\n",
    "connections:\n  w:\n    adapter: postgres\n    profile: {redact: [{with: mask}]}\n",
    "connections:\n  w:\n    adapter: postgres\n    rules: [{profile: {redact: []}}]\n",
    "x-other: 1\nconnections:\n  w: {adapter: postgres}\n",
]


def test_the_schema_is_a_valid_2020_12_schema() -> None:
    Draft202012Validator.check_schema(project_schema())


@pytest.mark.parametrize("body", ACCEPTED + REFUSED)
def test_the_schema_and_the_loader_agree(tmp_path: Path, body: str) -> None:
    by_schema = Draft202012Validator(project_schema()).is_valid(yaml.safe_load(body))
    (tmp_path / ".dbprint.yaml").write_text(body)

    try:
        load_project(tmp_path)
        by_loader = True
    except ConfigError:
        by_loader = False

    assert by_schema == by_loader == (body in ACCEPTED)


def test_the_closed_vocabularies_are_the_codes_own() -> None:
    defs = project_schema()["$defs"]
    redact = defs["RedactRule"]["properties"]

    assert defs["Connection"]["properties"]["adapter"]["enum"] == list(ADAPTERS)
    assert set(redact["sensitivity"]["items"]["enum"]) == set(get_args(Sensitivity))
    assert set(redact["looks_like"]["items"]["enum"]) == set(get_args(LooksLike))


@pytest.mark.parametrize(
    "path",
    [
        "docs/format/v1/examples/production/.dbprint.yaml",
        "docs/format/v1/examples/vocabulary/.dbprint.yaml",
    ],
)
def test_the_shipped_examples_validate(path: str) -> None:
    raw = yaml.safe_load((REPO_ROOT / path).read_text())

    assert list(Draft202012Validator(project_schema()).iter_errors(raw)) == []


def test_the_init_scaffold_validates_and_names_the_schema() -> None:
    assert PROJECT_TEMPLATE.startswith("# yaml-language-server: $schema=https://")
    assert (
        list(Draft202012Validator(project_schema()).iter_errors(yaml.safe_load(PROJECT_TEMPLATE)))
        == []
    )
