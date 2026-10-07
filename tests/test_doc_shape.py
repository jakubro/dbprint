"""Shipped Markdown keeps one shape: a paragraph per line, em dashes, no test-suite internals."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from tests._scripts import REPO_ROOT


EM_DASH = chr(0x2014)

IMPLEMENTER_DOCS = frozenset({"docs/ARCHITECTURE.md", "docs/GUIDELINES.md"})

SUBSTRATE_VOCABULARY = re.compile(
    r"the test suite|test substrate|emulator|\bchdb\b|recorded response|local spark",
    re.IGNORECASE,
)

_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_QUOTE = re.compile(r"^\s*>")
_NOT_PROSE = re.compile(r"^\s*(\||#|---\s*$|\*\*\*\s*$|<!--|</?details|<summary)")
_SPACED_HYPHEN = re.compile(r"(?<=[\w`)\]'\".,;:*_%?!]) - (?=[\w`(\['\"*_])")
_CODE_SPAN = re.compile(r"`[^`]*`")


@dataclass(frozen=True)
class Offence:
    """One shape violation, located precisely enough to fix."""

    path: str
    line: int
    kind: str
    excerpt: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.kind}] {self.excerpt.strip()[:100]!r}"


def shape_offences(text: str, path: str) -> list[Offence]:
    """Every wrapped paragraph, spaced-hyphen dash and substrate mention in one Markdown file."""

    offences: list[Offence] = []
    fence = False
    continuable = False
    lines = text.splitlines()
    # YAML front matter is key-per-line data, not prose, so its consecutive lines are no wrap.
    start = lines.index("---", 1) + 1 if lines[:1] == ["---"] and "---" in lines[1:] else 0

    for number, line in enumerate(lines[start:], start + 1):
        if _FENCE.match(line):
            fence = not fence
            continuable = False
            continue

        if fence or not line.strip() or _NOT_PROSE.match(line):
            continuable = False
            continue

        quoted = bool(_QUOTE.match(line))
        body = re.sub(r"^\s*>\s?", "", line) if quoted else line

        if not body.strip():
            continuable = False
            continue

        if continuable and not _LIST_ITEM.match(body):
            offences.append(Offence(path, number, "wrapped paragraph", line))

        continuable = True
        masked = _CODE_SPAN.sub(lambda span: "`" + "x" * (len(span.group()) - 2) + "`", body)

        if not quoted and _SPACED_HYPHEN.search(masked):
            offences.append(Offence(path, number, f"spaced hyphen, not {EM_DASH}", line))

        if path not in IMPLEMENTER_DOCS and SUBSTRATE_VOCABULARY.search(masked):
            offences.append(Offence(path, number, "test-suite internals", line))

    return offences


def shipped_markdown() -> list[str]:
    """Repo-relative paths of every Markdown file the package or the site publishes."""

    paths = [REPO_ROOT / "README.md", REPO_ROOT / "src/dbprint/engine/reading_guide.md"]
    paths.extend(p for p in (REPO_ROOT / "docs").rglob("*.md") if "node_modules" not in p.parts)

    return sorted(p.relative_to(REPO_ROOT).as_posix() for p in paths)


class TestShippedMarkdownKeepsItsShape:
    def test_no_page_breaks_the_shape(self) -> None:
        offences = [
            offence
            for relative in shipped_markdown()
            for offence in shape_offences((REPO_ROOT / relative).read_text("utf-8"), relative)
        ]

        assert not offences, "\n".join(o.render() for o in offences)

    def test_the_scan_actually_read_the_docs(self) -> None:
        files = shipped_markdown()

        assert len(files) > 30, f"expected every shipped page, got {len(files)}"
        assert "docs/format/v1/SPEC.md" in files
        assert "src/dbprint/engine/reading_guide.md" in files


class TestEnforcementIsLive:
    def test_a_hard_wrapped_paragraph_is_caught(self) -> None:
        found = shape_offences("First half of a sentence\nand its second half.\n", "p.md")

        assert [(o.kind, o.line) for o in found] == [("wrapped paragraph", 2)]

    def test_a_wrapped_list_item_is_caught(self) -> None:
        found = shape_offences("- An item that\n  carries on here.\n", "p.md")

        assert [o.kind for o in found] == ["wrapped paragraph"]

    def test_a_wrapped_quote_is_caught(self) -> None:
        found = shape_offences("> Quoted text\n> carrying on.\n", "p.md")

        assert [o.kind for o in found] == ["wrapped paragraph"]

    def test_a_spaced_hyphen_is_caught(self) -> None:
        found = shape_offences("A clause - and another.\n", "p.md")

        assert [o.kind for o in found] == [f"spaced hyphen, not {EM_DASH}"]

    def test_a_hyphen_beside_a_code_span_is_caught(self) -> None:
        found = shape_offences("The profile only - `cardinality` stays.\n", "p.md")

        assert [o.kind for o in found] == [f"spaced hyphen, not {EM_DASH}"]

    @pytest.mark.parametrize(
        "phrase",
        ["The test suite runs on chdb.", "The test substrate is duckdb.", "an emulator"],
    )
    def test_substrate_vocabulary_is_caught_on_a_user_page(self, phrase: str) -> None:
        found = shape_offences(f"{phrase}\n", "docs/adapters/p.md")

        assert "test-suite internals" in [o.kind for o in found]


class TestWhatIsNotProse:
    def test_consecutive_list_items_are_not_a_wrap(self) -> None:
        assert shape_offences("- one\n- two\n1. three\n", "p.md") == []

    def test_a_fence_is_skipped(self) -> None:
        assert shape_offences("```text\nline one - of\nline two\n```\n", "p.md") == []

    def test_a_table_and_a_heading_are_skipped(self) -> None:
        assert shape_offences("## A - B\n\n| a - b |\n|---|\n", "p.md") == []

    def test_a_hyphen_inside_a_code_span_is_skipped(self) -> None:
        assert shape_offences("Compute `rows_scanned - null_count` first.\n", "p.md") == []

    def test_a_quote_keeps_its_hyphens(self) -> None:
        assert shape_offences("> Reads prints - offline.\n", "p.md") == []

    def test_front_matter_is_skipped(self) -> None:
        assert shape_offences("---\nname: x\ndescription: y\n---\n\nBody.\n", "p.md") == []

    def test_implementer_docs_may_name_the_substrates(self) -> None:
        assert shape_offences("The test suite runs on chdb.\n", "docs/ARCHITECTURE.md") == []

    def test_an_em_dash_passes(self) -> None:
        assert shape_offences(f"A clause {EM_DASH} and another.\n", "p.md") == []
