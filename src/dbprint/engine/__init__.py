"""dbprint engine - orchestrates Config + Adapter + Writer into generate().

`Engine` is the orchestrator; `GenerateResult`, `TableResult`, `SummaryCounts` and
`DiffSummary` are its return types. The `EXIT_*` constants are the whole exit-code
vocabulary, including codes only the CLI returns.
"""

from __future__ import annotations

from .context_assembler import AssemblyOptions, AssemblyResult
from .context_assembler import assemble as assemble_context
from .context_assembler import assemble_structured as assemble_structured_context
from .freshness import DurationError, StaleEntry, parse_duration
from .freshness import evaluate as evaluate_freshness
from .freshness import format_age as format_freshness_age
from .orchestrator import Engine
from .result import (
    EXIT_ASSERTION,
    EXIT_CONNECTION,
    EXIT_DRIFT,
    EXIT_GENERIC,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_STALENESS,
    EXIT_TOTAL_FAILURE,
    DiffRequest,
    DiffResult,
    DiffSummary,
    GenerateRequest,
    GenerateResult,
    ProgressCallback,
    ProgressEvent,
    SketchFailure,
    SummaryCounts,
    TableResult,
)


__all__ = [
    "EXIT_ASSERTION",
    "EXIT_CONNECTION",
    "EXIT_DRIFT",
    "EXIT_GENERIC",
    "EXIT_OK",
    "EXIT_PARTIAL",
    "EXIT_STALENESS",
    "EXIT_TOTAL_FAILURE",
    "AssemblyOptions",
    "AssemblyResult",
    "DiffRequest",
    "DiffResult",
    "DiffSummary",
    "DurationError",
    "Engine",
    "GenerateRequest",
    "GenerateResult",
    "ProgressCallback",
    "ProgressEvent",
    "SketchFailure",
    "StaleEntry",
    "SummaryCounts",
    "TableResult",
    "assemble_context",
    "assemble_structured_context",
    "evaluate_freshness",
    "format_freshness_age",
    "parse_duration",
]
