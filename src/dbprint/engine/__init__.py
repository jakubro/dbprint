"""dbprint engine - orchestrates Config + Adapter + Writer into generate().

`Engine` is the orchestrator; `GenerateResult`, `TableResult`, `SummaryCounts` and
`DiffSummary` are its return types. The `EXIT_*` constants are the whole exit-code
vocabulary, including codes only the CLI returns.
"""

from __future__ import annotations

from dbprint.config.duration import DurationError, parse_duration
from .context_assembler import AssemblyOptions, AssemblyResult, PayloadResult, Purpose
from .context_assembler import assemble as assemble_context
from .context_assembler import assemble_payloads as assemble_context_payloads
from .context_assembler import assemble_structured as assemble_structured_context
from .context_assembler import ranked_sections as context_sections
from .context_assembler import structured_sections as structured_context_sections
from .freshness import StaleEntry
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
    "PayloadResult",
    "ProgressCallback",
    "ProgressEvent",
    "Purpose",
    "SketchFailure",
    "StaleEntry",
    "SummaryCounts",
    "TableResult",
    "assemble_context",
    "assemble_context_payloads",
    "assemble_structured_context",
    "context_sections",
    "evaluate_freshness",
    "format_freshness_age",
    "parse_duration",
    "structured_context_sections",
]
