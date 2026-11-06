"""Hatchling build hook: rewrites packaged documents' link targets to absolute URLs at
wheel-build time, so an installed copy cites a working address while `docs/` stays relative.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


# Repo-relative source -> wheel-relative target - this rewrite's sole mapping.
PACKAGED: dict[str, str] = {
    "docs/format/v1/SPEC.md": "dbprint/spec/v1/SPEC.md",
    "docs/ASSERTIONS.md": "dbprint/assertions/ASSERTIONS.md",
    "plugin/skills/dbprint-annotate/SKILL.md": "dbprint/engine/annotate_skill.md",
}

# Mirror `site/astro.config.mjs`'s own constants - a `.mjs` file cannot be imported here, so
# `test_packaged_specifications.py` pins the two by reading that config's source text.
SITE_ORIGIN = "https://jakubro.github.io"
SITE_BASE = "/dbprint"
REPOSITORY = "https://github.com/jakubro/dbprint"
REF = "main"

_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_FENCE_RE = re.compile(r"^```")
# The one published page outside docs/, mirroring `publishedRoute` in the site's link plugin.
_PLUGIN_SKILL_RE = re.compile(r"^\.\./plugin/skills/([^/]+)/SKILL\.md$")


def rewrite_links(text: str, doc_relpath: str) -> str:
    """Rewrite every markdown link target in `text` to an absolute URL, fenced blocks skipped -
    `doc_relpath` resolves relative targets, and a non-`.md` target becomes a GitHub URL.
    """

    doc_dir = posixpath.dirname(doc_relpath)
    in_fence = False
    out_lines: list[str] = []

    for line in text.splitlines(keepends=True):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out_lines.append(line)

            continue

        if in_fence:
            out_lines.append(line)

            continue

        out_lines.append(_LINK_RE.sub(lambda m: _rewrite_one(m, doc_dir), line))

    return "".join(out_lines)


def _rewrite_one(match: re.Match[str], doc_dir: str) -> str:
    label, target = match.group(1), match.group(2)

    if target.startswith("http"):
        return match.group(0)

    target_path, _, fragment = target.partition("#")

    # A same-page fragment (`#foo`) has no path to resolve - the site's own plugin leaves it
    # untouched (`rewrite-markdown-links.mjs`'s `!target` check), and so does this one.
    if not target_path:
        return match.group(0)

    suffix = f"#{fragment}" if fragment else ""
    resolved = posixpath.relpath(posixpath.join("docs", doc_dir, target_path), "docs")
    route = _route(resolved) if target_path.endswith(".md") else None

    if resolved.startswith("..") and route is None:
        message = f"link target escapes the docs root: {target}"

        raise ValueError(message)

    if route is not None:
        return f"[{label}]({SITE_ORIGIN}{SITE_BASE}/{route}/{suffix})"

    kind = "tree" if target_path.endswith("/") else "blob"

    return f"[{label}]({REPOSITORY}/{kind}/{REF}/docs/{resolved}{suffix})"


def _route(docs_relpath: str) -> str | None:
    if skill := _PLUGIN_SKILL_RE.match(docs_relpath):
        return f"examples/skill/{skill.group(1)}"

    if docs_relpath.startswith(".."):
        return None

    return docs_relpath[: -len(".md")].lower()


class LinkRewriteBuildHook(BuildHookInterface):
    """Force-includes `PACKAGED`'s documents with every link rewritten to an absolute URL."""

    PLUGIN_NAME = "link-rewrite"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del version
        scratch = Path(tempfile.mkdtemp(prefix="dbprint-link-rewrite-"))
        self._scratch = scratch

        for source, target in PACKAGED.items():
            doc_relpath = posixpath.relpath(source, "docs")
            text = (Path(self.root) / source).read_text(encoding="utf-8")
            rewritten = rewrite_links(text, doc_relpath)
            out_path = scratch / Path(target).name
            out_path.write_text(rewritten, encoding="utf-8")
            build_data["force_include"][str(out_path)] = target

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        """Remove the scratch tree - runs after hatchling has already read from it."""

        del version, build_data, artifact_path
        shutil.rmtree(self._scratch, ignore_errors=True)
