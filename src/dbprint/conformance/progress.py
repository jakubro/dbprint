"""Pass identity for `validate_print`'s offline walk - the vocabulary `on_table` carries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


# Execution order inside `validate_print`, one entry per `on_table` call site.
VALIDATION_PASSES: tuple[str, ...] = (
    "manifest",
    "artifacts",
    "edge reciprocity",
    "edge arithmetic",
    "annotation keys",
    "column claims",
    "value notes",
    "grain keys",
    "edge verdicts",
    "edge claims",
)

# Each sub-checker's own callback shape: (fqn, done-so-far, total), carrying no pass identity.
TableSink = Callable[[str, int, int], None]


@dataclass(frozen=True)
class ValidationTick:
    """One (table, pass) progress signal from `validate_print`'s offline walk. `findings` and
    `severity` are set only on the pass that closes a table; `severity` is None when clean.
    """

    fqn: str
    index: int
    total: int
    pass_name: str
    pass_index: int
    pass_total: int
    findings: int | None = None
    severity: Literal["error", "warning"] | None = None


ValidationProgress = Callable[[ValidationTick], None]
