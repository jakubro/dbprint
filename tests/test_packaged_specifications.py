"""Every specification a failure cites is one the wheel carries.

`check` prints a `spec_ref` on every issue, in the spelling SPEC 6.2 and ASSERTIONS.md 5.1
require. Those citations name a document, so the document has to be in the package a reader
installed - the citations are read here rather than listed, so a new one is covered too.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SOURCES = sorted((REPO / "src/dbprint").rglob("*.py"))

# Three spelt forms of a `spec_ref`: a bare section marker, which SPEC 6.2 reads as its own
# document; one prefixed `SPEC`; one prefixed with a document name (ASSERTIONS.md 5.1).
_SPEC_REF_RE = re.compile(r'"(?:(?P<doc>[A-Za-z_]+\.md|SPEC) )?§[0-9][0-9.]*"')


def force_included() -> dict[str, Path]:
    """Map each force-included document's basename to the repo file it is included from."""

    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    mapping = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    return {Path(source).name: REPO / source for source in mapping}


def cited_documents() -> set[str]:
    """Every document named by a `spec_ref` literal anywhere in the shipped source."""

    cited = set()

    for source in SOURCES:
        for match in _SPEC_REF_RE.finditer(source.read_text()):
            named = match.group("doc")
            cited.add("SPEC.md" if named in (None, "SPEC") else named)

    return cited


def test_citations_were_actually_found() -> None:
    """A regex that matched nothing would make the rest of this module vacuous."""

    assert cited_documents() >= {"SPEC.md", "ASSERTIONS.md"}


@pytest.mark.parametrize("document", sorted(cited_documents()))
def test_every_cited_document_ships(document: str) -> None:
    """A citation a reader cannot follow from an install is the defect this guards."""

    assert document in force_included()


@pytest.mark.parametrize("document,source", sorted(force_included().items()))
def test_every_included_document_exists(document: str, source: Path) -> None:
    """A mapping whose source has moved would ship nothing and say nothing."""

    assert source.is_file()


@pytest.mark.parametrize("document,source", sorted(force_included().items()))
def test_each_document_lands_in_a_real_package(document: str, source: Path) -> None:
    """The packaged path has to sit inside a package, or `importlib.resources` cannot reach it."""

    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    mapping = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    target = next(v for k, v in mapping.items() if Path(k).name == document)
    package = REPO / "src" / Path(target).parent

    assert (package / "__init__.py").is_file()
