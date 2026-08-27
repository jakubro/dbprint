"""`mcp.reference`'s heading-subtree slicing (MCP.md 4.6) - pure text, plus real content."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbprint.mcp import reference


_SAMPLE = """\
# dbprint format specification - v1

## 0. Scope and terminology

### 0.1 What this spec covers

Covers this.

### 0.2 What this spec does NOT cover

Does not cover that.

## 1. Directory layout

### 1.1 Project root

Root text.

#### 1.1.1 Nested detail

Deep text.

### 1.2 Connection root

Connection text.

## Appendix A - Example print

Unnumbered appendix.

## Cross-references

- [SPEC.md](format/v1/SPEC.md)
"""

# A YAML comment inside a fenced block, shaped exactly like ASSERTIONS.md's real one - a
# fence-blind scanner misreads it as a level-1 heading.
_SAMPLE_WITH_FENCE = """\
## 1. Configuration

### 1.5 Equivalent empty forms

The following are equivalent: no assertions to evaluate.

```yaml
# (no assertions: key)

assertions: {}
```

### 1.6 Next real heading

Real text after the fence.
"""


class TestParseHeadings:
    def test_levels_and_numbers(self) -> None:
        headings = reference._parse_headings(_SAMPLE)
        by_number = {h.number: h for h in headings if h.number is not None}

        assert by_number["0"].level == 2
        assert by_number["0.1"].level == 3
        assert by_number["1.1.1"].level == 4

    def test_unnumbered_headings_carry_no_number(self) -> None:
        headings = reference._parse_headings(_SAMPLE)
        titles_with_no_number = {h.title for h in headings if h.number is None}

        assert "Appendix A - Example print" in titles_with_no_number
        assert "Cross-references" in titles_with_no_number

    def test_a_heading_shaped_comment_inside_a_fence_is_not_a_heading(self) -> None:
        """The exact ASSERTIONS.md gotcha this scanner exists to avoid."""

        headings = reference._parse_headings(_SAMPLE_WITH_FENCE)
        numbers = {h.number for h in headings}

        assert numbers == {"1", "1.5", "1.6"}

    def test_subtree_ends_at_the_next_same_or_shallower_heading(self) -> None:
        headings = reference._parse_headings(_SAMPLE)
        by_number = {h.number: h for h in headings if h.number is not None}

        # 0.1's subtree stops at 0.2 (same level), not at 1. or 1.1.
        assert by_number["0.1"].end == by_number["0.2"].start

        # 1.1's subtree swallows its deeper child 1.1.1 and stops only at 1.2 (its own level).
        assert by_number["1.1"].end == by_number["1.2"].start
        assert by_number["1.1"].start < by_number["1.1.1"].start < by_number["1.1"].end


class TestSectionOf:
    def test_returns_the_heading_and_its_subtree(self) -> None:
        text = reference.section_of(_SAMPLE, "0.1")

        assert text is not None
        assert "0.1 What this spec covers" in text
        assert "Covers this." in text
        assert "0.2 What this spec does NOT cover" not in text

    def test_resolves_at_any_depth(self) -> None:
        shallow = reference.section_of(_SAMPLE, "1")
        deep = reference.section_of(_SAMPLE, "1.1.1")

        assert shallow is not None and "1.1 Project root" in shallow
        assert deep is not None and "Deep text." in deep

    def test_unknown_number_returns_none(self) -> None:
        assert reference.section_of(_SAMPLE, "9.9") is None

    def test_fenced_comment_never_resolves_as_a_section(self) -> None:
        assert reference.section_of(_SAMPLE_WITH_FENCE, "0") is None

    def test_a_bare_spec_ref_citation_resolves_unstripped(self) -> None:
        """A citation copied verbatim off a finding must work with no editing by the caller."""

        text = reference.section_of(_SAMPLE, "§0.1")

        assert text is not None
        assert "0.1 What this spec covers" in text

    def test_a_document_prefixed_spec_ref_citation_resolves_unstripped(self) -> None:
        """The document-prefixed form strips both the document name and the section sign."""

        text = reference.section_of(_SAMPLE, "ASSERTIONS.md §0.1")

        assert text is not None
        assert "0.1 What this spec covers" in text


class TestHeadingTreeOf:
    def test_lists_every_heading_including_unnumbered(self) -> None:
        tree = reference.heading_tree_of(_SAMPLE)

        assert "0. Scope and terminology" in tree
        assert "0.1 What this spec covers" in tree
        assert "Appendix A - Example print" in tree
        assert "Cross-references" in tree

    def test_indents_by_nesting_level(self) -> None:
        lines = reference.heading_tree_of(_SAMPLE).splitlines()
        top = next(line for line in lines if "0. Scope" in line)
        nested = next(line for line in lines if "0.1 What" in line)
        deepest = next(line for line in lines if "Nested detail" in line)

        assert len(nested) - len(nested.lstrip(" ")) > len(top) - len(top.lstrip(" "))
        assert len(deepest) - len(deepest.lstrip(" ")) > len(nested) - len(nested.lstrip(" "))

    def test_does_not_return_the_whole_document(self) -> None:
        tree = reference.heading_tree_of(_SAMPLE)

        assert "Covers this." not in tree
        assert "Deep text." not in tree


class TestAgainstRealPackagedContent:
    """The slicing logic against real SPEC.md/ASSERTIONS.md content.

    `_read` is monkeypatched to the on-disk files: only a built wheel force-includes them.
    """

    @pytest.fixture(autouse=True)
    def _real_documents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repo_docs = Path(__file__).resolve().parents[2] / "docs"
        paths = {
            "spec": repo_docs / "format/v1/SPEC.md",
            "assertions": repo_docs / "ASSERTIONS.md",
        }
        monkeypatch.setattr(reference, "_read", lambda document: paths[document].read_text())

    def test_spec_section_resolves(self) -> None:
        text = reference.section("spec", "2.2.9")

        assert text is not None
        assert "2.2.9" in text.splitlines()[0]

    def test_assertions_section_resolves(self) -> None:
        text = reference.section("assertions", "1.2")

        assert text is not None
        assert "1.2" in text.splitlines()[0]

    def test_spec_heading_tree_is_not_the_whole_document(self) -> None:
        tree = reference.heading_tree("spec")
        whole = reference.read_document("spec")

        assert len(tree) < len(whole)
        assert "0. Scope and terminology" in tree

    def test_every_citable_spec_ref_resolves_verbatim(self) -> None:
        """Every `spec_ref` the source emits must resolve verbatim - section sign and all."""

        import re

        sources = (Path(__file__).resolve().parents[2] / "src/dbprint").rglob("*.py")
        cited: set[str] = set()

        for source in sources:
            for match in re.finditer(r'"(§[0-9][0-9.]*)"', source.read_text()):
                cited.add(match.group(1).rstrip("."))

        assert cited, "no spec_ref citations found - the scan itself is broken"

        for citation in cited:
            assert reference.section("spec", citation) is not None, citation

    def test_every_citable_assertions_ref_resolves_verbatim(self) -> None:
        """The document-prefixed citation form, cross-checked against the shipped source."""

        import re

        sources = (Path(__file__).resolve().parents[2] / "src/dbprint").rglob("*.py")
        cited: set[str] = set()

        for source in sources:
            for match in re.finditer(r'"(ASSERTIONS\.md §[0-9][0-9.]*)"', source.read_text()):
                cited.add(match.group(1).rstrip("."))

        assert cited, "no ASSERTIONS.md citations found - the scan itself is broken"

        for citation in cited:
            assert reference.section("assertions", citation) is not None, citation
