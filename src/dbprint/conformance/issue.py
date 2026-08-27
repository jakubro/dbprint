"""Conformance Issue dataclass per SPEC 6.2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, order=True)
class Issue:
    """A single conformance violation or anomaly.

    Field order is the sort order, so sorted Issues come out path-then-code per SPEC 6.6.
    """

    path: str
    code: str
    severity: Literal["error", "warning"]
    detail: str
    # A bare section marker cites the format spec; any other document names itself first.
    spec_ref: str
