"""Guard the identity every packaged JSON Schema publishes in its `$id`.

The handle in that address is derived from `pyproject.toml` rather than written here, so the
assertions cross two independently maintained files instead of restating one of them.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
SCHEMAS = sorted((REPO / "src/dbprint/spec/v1").glob("*.schema.json"))

_HANDLE_RE = re.compile(r"//[^/]*/([^/]+)")


def _packaging() -> dict:
    """The `[project]` table of pyproject.toml."""

    return tomllib.loads((REPO / "pyproject.toml").read_text())["project"]


def account_handle() -> str:
    """The account owning the project, read out of pyproject.toml's own URLs."""

    urls = _packaging()["urls"]
    handles = {match.group(1) for url in urls.values() if (match := _HANDLE_RE.search(str(url)))}

    assert len(handles) == 1, f"project URLs name more than one account: {sorted(handles)}"

    return handles.pop()


def test_the_schema_set_is_not_empty() -> None:
    """A glob that matched nothing would make every other test here vacuous."""

    assert len(SCHEMAS) == 7


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda path: path.name)
def test_the_identifier_names_its_own_file(schema: Path) -> None:
    """Each `$id` ends in the basename of the file declaring it."""

    declared = json.loads(schema.read_text())["$id"]

    assert declared.rsplit("/", 1)[-1] == schema.name


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda path: path.name)
def test_the_identifier_is_under_the_project_account(schema: Path) -> None:
    """Each `$id` is served from an address the project controls."""

    declared = json.loads(schema.read_text())["$id"]

    assert declared.startswith(f"https://{account_handle()}.github.io/")
