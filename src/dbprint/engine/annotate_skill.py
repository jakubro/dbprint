"""The shipped annotation skill's steps, which `dbprint annotate` prints ahead of a table's brief.

Read from the installed package (`hatch_build.py` force-includes it), else from the repo source.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path


_PACKAGE_FILE = ("dbprint.engine", "annotate_skill.md")
_SOURCE_TREE_FALLBACK = "docs/examples/skill/dbprint-annotate.md"

# src/dbprint/engine/annotate_skill.py -> repo root (parents[0]=engine, [1]=dbprint, [2]=src, [3]=root).
_REPO_ROOT = Path(__file__).resolve().parents[3]


def annotate_skill_steps() -> str:
    """The skill's body with its frontmatter stripped."""

    package, filename = _PACKAGE_FILE

    try:
        text = resources.files(package).joinpath(filename).read_text(encoding="utf-8")
    except FileNotFoundError:
        fallback = _REPO_ROOT / _SOURCE_TREE_FALLBACK

        if not fallback.is_file():
            message = (
                f"the annotation skill is missing: neither {package}/{filename} nor {fallback}"
            )

            raise FileNotFoundError(message) from None

        text = fallback.read_text(encoding="utf-8")

    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[1]

    return text.lstrip("\n")
