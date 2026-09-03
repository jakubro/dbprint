"""Every specification a failure cites is one the wheel carries.

A citation names a document, so the wheel has to carry it; the citations are read, not listed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SOURCES = sorted((REPO / "src/dbprint").rglob("*.py"))

sys.path.insert(0, str(REPO))
from hatch_build import PACKAGED, SITE_ORIGIN, rewrite_links


# Two spelt forms of a `spec_ref`: a bare section marker, which SPEC 6.2 reads as its own
# document; one prefixed with a document name (ASSERTIONS.md 5.1).
_SPEC_REF_RE = re.compile(r'"(?:(?P<doc>[A-Za-z_]+\.md) )?§[0-9][0-9.]*"')
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_FENCE_RE = re.compile(r"^```")


def all_mappings() -> dict[str, str]:
    """Every repo-relative source -> wheel-relative target the wheel force-includes - both the
    static `force-include` entries and `hatch_build.PACKAGED`, which the static table omits.
    """

    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    static = config["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})

    return {**static, **PACKAGED}


def force_included() -> dict[str, Path]:
    """Map each force-included document's basename to the repo file it is included from."""

    return {Path(source).name: REPO / source for source in all_mappings()}


def cited_documents() -> set[str]:
    """Every document named by a `spec_ref` literal anywhere in the shipped source."""

    cited = set()

    for source in SOURCES:
        for match in _SPEC_REF_RE.finditer(source.read_text(encoding="utf-8")):
            named = match.group("doc")
            cited.add("SPEC.md" if named is None else named)

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

    mapping = all_mappings()
    target = next(v for k, v in mapping.items() if Path(k).name == document)
    package = REPO / "src" / Path(target).parent

    assert (package / "__init__.py").is_file()


def _built_wheel(tmp_path: Path, env: dict[str, str] | None = None) -> zipfile.ZipFile:
    """Build a real wheel via `uv build --wheel` and open it."""

    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO,
        check=True,
        capture_output=True,
        env=env,
    )
    wheel = max(tmp_path.glob("*.whl"), key=lambda p: p.stat().st_mtime)

    return zipfile.ZipFile(wheel)


def _links(text: str) -> list[str]:
    """Every markdown link target outside a fenced code block."""

    found, in_fence = [], False

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence

            continue

        if not in_fence:
            found.extend(_LINK_RE.findall(line))

    return found


@pytest.mark.parametrize("source,target", sorted(PACKAGED.items()))
def test_packaged_links_are_absolute_and_survive_a_real_build(
    tmp_path: Path,
    source: str,
    target: str,
) -> None:
    """A built wheel carries no relative link in either rewritten document."""

    wheel = _built_wheel(tmp_path)
    text = wheel.read(target).decode()

    assert _links(text), "no links found - the regex would make this test vacuous"
    assert all(link.startswith(("http", SITE_ORIGIN)) for link in _links(text))


@pytest.mark.parametrize("source,target", sorted(PACKAGED.items()))
def test_packaged_body_differs_from_source_only_in_link_targets(
    tmp_path: Path,
    source: str,
    target: str,
) -> None:
    """Prose is untouched - only link targets differ between repo source and packaged copy."""

    wheel = _built_wheel(tmp_path)
    packaged = wheel.read(target).decode("utf-8")
    original = (REPO / source).read_text(encoding="utf-8")

    assert _LINK_RE.sub("LINK", packaged) == _LINK_RE.sub("LINK", original)


@pytest.mark.parametrize("source,target", sorted(PACKAGED.items()))
def test_packaged_document_survives_a_non_utf8_default_encoding(
    tmp_path: Path,
    source: str,
    target: str,
) -> None:
    """A build host whose locale defaults to ASCII must still ship the real document, not a
    decode error or mojibake - both `SPEC.md`/`ASSERTIONS.md` carry non-ASCII prose.
    """

    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"}
    wheel = _built_wheel(tmp_path, env=env)
    packaged = wheel.read(target).decode("utf-8")
    original = (REPO / source).read_text(encoding="utf-8")

    assert _LINK_RE.sub("LINK", packaged) == _LINK_RE.sub("LINK", original)


def test_the_real_loader_resolves_against_an_installed_wheel(tmp_path: Path) -> None:
    """`importlib.resources` must resolve against a real installed (non-editable) copy of the
    package, not the dev source tree a monkeypatch would let it read instead.

    Installs the wheel's files into an isolated target and imports fresh with `-S`, so no
    site-packages or `.pth` hook from this repo can shadow it.
    """

    wheel = _built_wheel(tmp_path / "build")
    site_dir = tmp_path / "site"
    subprocess.run(
        ["uv", "pip", "install", "--target", str(site_dir), f"{wheel.filename}[mcp]"],
        check=True,
        capture_output=True,
        text=True,
    )
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from dbprint.mcp import reference; "
        "print(reference.__file__); "
        "print(reference.read_document('spec')[:40].replace(chr(10), ' '))"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", script, str(site_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    resolved_from, snippet = result.stdout.splitlines()

    assert resolved_from.startswith(str(site_dir)), (
        f"reference module resolved from {resolved_from}, not the installed wheel at "
        f"{site_dir} - an ambient install may have shadowed it"
    )
    assert "dbprint format specification" in snippet


def test_rewrite_links_preserves_fragments() -> None:
    """A fragment on a `.md` target survives the rewrite unchanged."""

    text = "[x](format/v1/SPEC.md#22-statisticsyaml)"
    rewritten = rewrite_links(text, "ASSERTIONS.md")

    assert rewritten == "[x](https://jakubro.github.io/dbprint/format/v1/spec/#22-statisticsyaml)"


def test_rewrite_links_skips_fenced_code_blocks() -> None:
    """A bracket-paren pair inside a fence is not mistaken for a link."""

    text = "```\n[not a link](../../ASSERTIONS.md)\n```\n"

    assert rewrite_links(text, "format/v1/SPEC.md") == text


def test_rewrite_links_repository_target_uses_tree_or_blob() -> None:
    """A trailing slash means `tree`; anything else means `blob`."""

    assert "/tree/main/docs/" in rewrite_links("[x](examples/)", "format/v1/SPEC.md")
    assert "/blob/main/docs/" in rewrite_links("[x](examples/manifest.yaml)", "format/v1/SPEC.md")


def test_rewrite_links_leaves_a_same_page_fragment_untouched() -> None:
    """A pure `#fragment` target has no path to resolve - it must survive verbatim, matching
    the site plugin's own `!target` no-op (`rewrite-markdown-links.mjs`).
    """

    text = "[x](#22-statisticsyaml)"

    assert rewrite_links(text, "ASSERTIONS.md") == text


@pytest.mark.parametrize(
    ("text", "doc_relpath"),
    [
        ("[x](../../ASSERTIONS.md)", "format/v1/SPEC.md"),
        ("[x](examples/)", "format/v1/SPEC.md"),
        ("[x](examples/manifest.yaml)", "format/v1/SPEC.md"),
        ("[x](https://semver.org)", "format/v1/SPEC.md"),
        ("[x](#foo)", "format/v1/SPEC.md"),
        ("[x](format/v1/SPEC.md#22-statisticsyaml)", "ASSERTIONS.md"),
    ],
)
def test_python_rewrite_agrees_with_the_real_site_plugin(text: str, doc_relpath: str) -> None:
    """Runs the site's own `rewriteMarkdownLinks` plugin over the same link Python just rewrote
    and compares the targets - pinning the URL constants alone would miss a real disagreement.
    """

    node = shutil.which("node")

    if node is None:
        pytest.skip("node not on PATH - cannot cross-check against the real site plugin")

    payload = json.dumps({"text": text, "docRelpath": doc_relpath})
    result = subprocess.run(
        [node, str(REPO / "site" / "scripts" / "rewrite-link-check.mjs")],
        input=payload,
        cwd=REPO / "site",
        capture_output=True,
        text=True,
        check=True,
    )
    js_target = result.stdout.strip()

    match = _LINK_RE.search(rewrite_links(text, doc_relpath))
    assert match is not None

    assert match.group(1) == js_target


def test_url_constants_match_astro_config() -> None:
    """`hatch_build`'s URL constants agree with `site/astro.config.mjs` - different runtimes, so
    this reads the config's source text rather than importing it.
    """

    from hatch_build import REF, REPOSITORY, SITE_BASE, SITE_ORIGIN

    config = (REPO / "site/astro.config.mjs").read_text(encoding="utf-8")

    assert f'site: "{SITE_ORIGIN}"' in config
    assert f'const BASE = "{SITE_BASE}"' in config
    assert f'const REPOSITORY = "{REPOSITORY}"' in config
    assert f'const REF = "{REF}"' in config
