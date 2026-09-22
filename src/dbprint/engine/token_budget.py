"""Soft token-budget allocator for `dbprint context` output.

Sections are offered in priority order, pinned first, and one that does not fit is skipped
rather than closing the door behind it. Token counts are `len(text) // 4`. Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass


CHARS_PER_TOKEN = 4  # universal approximation; no tokenizer dep


@dataclass(frozen=True)
class Section:
    """A named, ordered section of rendered text plus its token cost.

    `pinned` decides the order a section is offered in, never whether the budget applies to it.
    """

    name: str
    text: str
    tokens: int
    pinned: bool = False


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


def make_section(name: str, text: str, *, pinned: bool = False) -> Section:
    """Build a Section, measuring its token cost from the text."""

    return Section(name=name, text=text, tokens=tokens_of(text), pinned=pinned)


def select(sections: list[Section], budget: int | None) -> Selection:
    """Fill the budget in priority order, pinned sections first.

    `budget=None` includes everything; otherwise a section that does not fit is skipped and a
    later, smaller one can still land. Both lists keep the caller's order, the render order.
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
    chosen: set[int] = set()

    for pinned_pass in (True, False):
        for index, sec in enumerate(sections):
            if index in chosen or sec.pinned is not pinned_pass or used + sec.tokens > budget:
                continue

            chosen.add(index)
            used += sec.tokens

    included = [sec for index, sec in enumerate(sections) if index in chosen]
    omitted = [sec for index, sec in enumerate(sections) if index not in chosen]

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
