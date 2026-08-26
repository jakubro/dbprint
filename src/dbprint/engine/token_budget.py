"""Soft token-budget allocator for `dbprint context` output.

Sections are filled greedily in declared priority order; the first that would exceed the
budget stops emission, and a truncation marker records what was dropped. Token counts are
the `len(text) // 4` heuristic. Pure: no I/O, no state.
"""

from __future__ import annotations

from dataclasses import dataclass


CHARS_PER_TOKEN = 4  # universal approximation; no tokenizer dep


@dataclass(frozen=True)
class Section:
    """A named, ordered section of rendered text plus its token cost."""

    name: str
    text: str
    tokens: int


@dataclass(frozen=True)
class Selection:
    """Result of running the budget algorithm over an ordered Section list."""

    included: tuple[Section, ...]
    omitted: tuple[Section, ...]
    truncated: bool
    used_tokens: int
    budget: int | None  # None when no budget was set


def tokens_of(text: str) -> int:
    """Approximate token count `max(1, len(text) // CHARS_PER_TOKEN)`, so no section is free."""

    if not text:
        return 0

    return max(1, len(text) // CHARS_PER_TOKEN)


def make_section(name: str, text: str) -> Section:
    """Build a Section, measuring its token cost from the text."""

    return Section(name=name, text=text, tokens=tokens_of(text))


def select(sections: list[Section], budget: int | None) -> Selection:
    """Apply the stop-at-boundary budget algorithm.

    `budget=None` includes everything. Otherwise sections are included until one would exceed
    the budget; that one and every section after it are omitted.
    """

    if budget is None:
        used = sum(s.tokens for s in sections)

        return Selection(
            included=tuple(sections),
            omitted=(),
            truncated=False,
            used_tokens=used,
            budget=None,
        )

    used = 0
    included: list[Section] = []
    omitted: list[Section] = []
    blocked = False

    for sec in sections:
        if blocked or used + sec.tokens > budget:
            omitted.append(sec)
            blocked = True
            continue

        included.append(sec)
        used += sec.tokens

    return Selection(
        included=tuple(included),
        omitted=tuple(omitted),
        truncated=bool(omitted),
        used_tokens=used,
        budget=budget,
    )


def truncation_marker(selection: Selection) -> str:
    """One-line HTML comment summarizing the truncation, or empty when none."""

    if not selection.truncated or selection.budget is None:
        return ""

    total = len(selection.included) + len(selection.omitted)
    omitted_names = ", ".join(s.name for s in selection.omitted)

    return (
        f"<!-- truncated: included {len(selection.included)}/{total} sections; "
        f"budget {selection.used_tokens}/{selection.budget} tokens; "
        f"omitted: {omitted_names} -->"
    )
