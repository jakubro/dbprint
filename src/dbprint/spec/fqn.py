"""The FQN syntax of SPEC 1.3: a table's folded parts joined by the one separator.

Every FQN is built and split here; keeping the separator out of a segment is the producer's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final


SEPARATOR: Final = "."


def join(parts: Sequence[str]) -> str:
    """The FQN of already-folded `parts`; no validation, so an out-of-print name joins as-is."""

    return SEPARATOR.join(parts)


def split(fqn: str) -> tuple[str, ...]:
    """The segments of a manifest FQN - meaningless for an out-of-print reference (SPEC 1.3)."""

    return tuple(fqn.split(SEPARATOR))


def directory(fqn: str) -> str:
    """The relative directory a table's files live in: one path segment per FQN part (SPEC 1.3)."""

    return "/".join(split(fqn))


def directory_segments(path: str) -> tuple[str, ...]:
    """The path segments of a table directory, one per FQN part when it matches (SPEC 1.3)."""

    return tuple(path.split("/"))
