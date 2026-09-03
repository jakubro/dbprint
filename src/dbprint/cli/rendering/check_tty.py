"""Human-readable renderer for `dbprint check`.

Emits a grouped per-connection report: manifest status, conformance summary, freshness
verdict. Markers stay ASCII (`OK` / `FAIL`) and the output is identical TTY or piped.
"""

from __future__ import annotations

from typing import TextIO

from dbprint.conformance import Issue
from dbprint.engine.freshness import format_age, format_threshold
from .check_data import CheckResult


def render_human(results: list[CheckResult], stream: TextIO) -> None:
    """Print every CheckResult in the locked grouped-sections layout."""

    for result in results:
        _render_one(result, stream)
        stream.write("\n")


def _render_one(result: CheckResult, stream: TextIO) -> None:
    stream.write(f"Connection: {result.connection_name}\n")

    if not result.manifest_present:
        stream.write(f"  FAIL: no manifest at {result.print_root}/manifest.yaml\n")
        stream.write(f"  exit {result.exit_code}\n")

        return

    errors = [i for i in result.issues if i.severity == "error"]
    warnings = [i for i in result.issues if i.severity == "warning"]

    # Printed ahead of every finding, so no reader assumes the report covered everything.
    # Split by severity: under an explicit --max-age a refused table does not fail the run,
    # and a non-failing entry has no business under a FAIL heading.
    failed_not_run = [n for n in result.not_run if n.severity == "error"]
    noted_not_run = [n for n in result.not_run if n.severity == "warning"]

    if failed_not_run:
        noun = "check" if len(failed_not_run) == 1 else "checks"
        stream.write(f"  FAIL: {len(failed_not_run)} {noun} did not run\n")

        for entry in failed_not_run:
            stream.write(f"    {entry.subject}\n")
            stream.write(f"      {entry.cause}\n")

    if noted_not_run:
        noun = "check" if len(noted_not_run) == 1 else "checks"
        stream.write(f"  NOTE: {len(noted_not_run)} {noun} reported, exit unaffected\n")

        for entry in noted_not_run:
            stream.write(f"    {entry.subject}\n")
            stream.write(f"      {entry.cause}\n")

    if errors:
        warning_note = f", {len(warnings)} warning(s)" if warnings else ""
        stream.write(f"  FAIL: conformance ({len(errors)} error(s){warning_note})\n")

        for issue in errors:
            stream.write(f"    {issue.path}\n")
            stream.write(f"      {issue.code} ({issue.spec_ref})\n")
            stream.write(f"      {issue.detail}\n")
    else:
        stream.write("  OK: conformance clean")

        if warnings:
            stream.write(f" ({len(warnings)} warning(s))")

        stream.write("\n")

    # An unmeasurable age (unparseable `profiled_at`) is not exceedance - `evaluate` records
    # both as a `StaleEntry`, but only a finite age actually exceeded its threshold.
    measured_stale = [s for s in result.stale_entries if s.age_days != float("inf")]
    unmeasurable = [s for s in result.stale_entries if s.age_days == float("inf")]

    # Unconditional: a conformance error must not silence the freshness verdict.
    if measured_stale:
        stream.write(f"  FAIL: {len(measured_stale)} print(s) exceed their max-age\n")

        # Reported per entry, not once in the header - the threshold is per table.
        stream.writelines(
            f"    {stale.fqn}    {format_age(stale.age_days)} "
            f"(max {format_threshold(stale.max_age_days)})\n"
            for stale in measured_stale[:10]
        )

        if len(measured_stale) > 10:
            stream.write(f"    ... {len(measured_stale) - 10} more\n")

        stream.write("  Run `dbprint generate` to refresh.\n")
    else:
        # An all-clear over an incomplete set would overclaim, so the wording narrows.
        judged = "every print that was judged" if result.not_run else "every print"
        stream.write(f"  OK: {judged} is within its max-age threshold\n")

    if unmeasurable:
        stream.write(f"  NOTE: {len(unmeasurable)} print(s) have an unmeasurable age\n")
        stream.writelines(f"    {stale.fqn}\n" for stale in unmeasurable[:10])

        if len(unmeasurable) > 10:
            stream.write(f"    ... {len(unmeasurable) - 10} more\n")

    if result.drift_issues:
        stream.write(f"  FAIL: {_drift_heading(result.drift_issues)}\n")

        for issue in result.drift_issues[:10]:
            stream.write(f"    {issue.path}\n")
            stream.write(f"      {issue.detail}\n")

        if len(result.drift_issues) > 10:
            stream.write(f"    ... {len(result.drift_issues) - 10} more\n")

        stream.write("  Run `dbprint diff` for details, `dbprint generate` to refresh.\n")

    if result.assertion_issues:
        errors_a = [i for i in result.assertion_issues if i.severity == "error"]
        warnings_a = [i for i in result.assertion_issues if i.severity == "warning"]

        if errors_a:
            stream.write(f"  FAIL: {len(errors_a)} assertion error(s)\n")

            for issue in errors_a[:10]:
                stream.write(f"    {issue.path}\n")
                stream.write(f"      {issue.code} ({issue.spec_ref})\n")
                stream.write(f"      {issue.detail}\n")

            if len(errors_a) > 10:
                stream.write(f"    ... {len(errors_a) - 10} more\n")

        if warnings_a and not errors_a:
            stream.write(f"  OK: assertions clean ({len(warnings_a)} warning(s))\n")
        elif warnings_a:
            stream.write(f"    ({len(warnings_a)} additional warning(s))\n")

    stream.write(f"  exit {result.exit_code}\n")


def _drift_heading(issues: tuple[Issue, ...]) -> str:
    """Name the drift a run carries - schema, statistics, or both.

    Counted over the full list, not the ten issues printed, so truncation cannot narrow it.
    """

    n_schema = sum(1 for i in issues if i.code == "drift.schema-changed")
    n_statistic = len(issues) - n_schema

    if n_statistic == 0:
        return f"schema drift on {len(issues)} event(s)"
    elif n_schema == 0:
        return f"statistics drift on {len(issues)} event(s)"
    else:
        return f"drift on {len(issues)} event(s) ({n_schema} schema, {n_statistic} statistics)"
