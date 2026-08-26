"""json / yaml emitters for `dbprint check`: `CheckResult` serialized for machine consumers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, TextIO

import yaml

from dbprint.conformance import Issue
from dbprint.engine.freshness import StaleEntry


@dataclass(frozen=True)
class NotRun:
    """One thing this check did not do, and why.

    `subject` is a table, or the connection when nothing about it could be reached.
    `severity` is `"warning"` only when an explicit `--max-age` already governs every table's
    freshness, so the entry cannot move an exit code the override has already decided.
    """

    subject: str
    cause: str
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class CheckResult:
    """Aggregate outcome of a single connection's check run.

    `drift_issues`/`assertion_issues` populate only on --online runs, or when offline
    statistic assertions evaluate. `default_max_age_days` is the run default, not the applied
    threshold: those resolve per table, and `stale_entries` carries the one each was judged
    against.
    """

    connection_name: str
    manifest_present: bool
    issues: tuple[Issue, ...]
    stale_entries: tuple[StaleEntry, ...]
    default_max_age_days: float
    exit_code: int
    drift_issues: tuple[Issue, ...] = ()
    assertion_issues: tuple[Issue, ...] = ()
    not_run: tuple[NotRun, ...] = ()


def render_data(results: list[CheckResult], fmt: str, stream: TextIO) -> None:
    """Emit the aggregate of CheckResults as `fmt` (json or yaml)."""

    payload = [_to_dict(r) for r in results]

    if fmt == "yaml":
        yaml.safe_dump_all(payload, stream, sort_keys=False, default_flow_style=False)
    else:
        json.dump(payload, stream, indent=2, default=str, sort_keys=False)
        stream.write("\n")


def _to_dict(result: CheckResult) -> dict[str, Any]:
    return {
        "connection": result.connection_name,
        "manifest_present": result.manifest_present,
        "exit_code": result.exit_code,
        "default_max_age_days": result.default_max_age_days,
        "issues": [_issue_to_dict(i) for i in result.issues],
        "stale_entries": [
            {
                "fqn": s.fqn,
                "age_days": s.age_days if s.age_days != float("inf") else None,
                "max_age_days": s.max_age_days,
            }
            for s in result.stale_entries
        ],
        "drift_issues": [_issue_to_dict(i) for i in result.drift_issues],
        "assertion_issues": [_issue_to_dict(i) for i in result.assertion_issues],
        "not_run": [
            {"subject": n.subject, "cause": n.cause, "severity": n.severity} for n in result.not_run
        ],
        "summary": {
            "errors": sum(1 for i in result.issues if i.severity == "error"),
            "warnings": sum(1 for i in result.issues if i.severity == "warning"),
            "stale_count": len(result.stale_entries),
            "drift_count": len(result.drift_issues),
            "not_run_count": len(result.not_run),
            "assertion_errors": sum(1 for i in result.assertion_issues if i.severity == "error"),
            "assertion_warnings": sum(
                1 for i in result.assertion_issues if i.severity == "warning"
            ),
        },
    }


def _issue_to_dict(issue: Issue) -> dict[str, str]:
    return {
        "path": issue.path,
        "code": issue.code,
        "severity": issue.severity,
        "detail": issue.detail,
        "spec_ref": issue.spec_ref,
    }
