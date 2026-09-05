"""pg_dump shell-out + SPEC 2.1.3 DDL normalization.

`extract_ddl` runs `pg_dump --schema-only --table=<pattern>` through the strip pipeline here,
producing a diff-stable `ddl.sql`; SPEC 2.1.3 (Postgres section) lists the exact strips.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time

from .connection import (
    PG_DUMP_BIN,
    PG_DUMP_TIMEOUT_SECONDS,
    ConnectionParams,
    PostgresConnectionError,
)
from .identity import Identity
from .. import trace_context


_LOG = logging.getLogger(__name__)


_HEADER_PREFIX_RE = re.compile(
    r"^--\s+(PostgreSQL database dump$|Dumped from database version |Dumped by pg_dump version |Started on )",
)
_FOOTER_LINE_RE = re.compile(r"^--\s+PostgreSQL database dump complete$")

_STRIPPED_SET_KEYS = (
    "statement_timeout",
    "lock_timeout",
    "idle_in_transaction_session_timeout",
    "transaction_timeout",
    "client_encoding",
    "standard_conforming_strings",
    "check_function_bodies",
    "xmloption",
    "client_min_messages",
    "row_security",
    "search_path",
    "default_tablespace",
    "default_table_access_method",
)

_SET_LINE_RE = re.compile(
    r"^SET\s+(" + "|".join(_STRIPPED_SET_KEYS) + r")\b",
    re.IGNORECASE,
)

_SET_CONFIG_LINE_RE = re.compile(r"^SELECT\s+pg_catalog\.set_config\b", re.IGNORECASE)

# The per-run token in \restrict/\unrestrict breaks diff-stability (SPEC 2.1.6); no schema lost.
_RESTRICT_LINE_RE = re.compile(r"^\\(?:un)?restrict\b")

# Statement-level strips: drop the whole statement (maybe multi-line) to its terminating `;`.
_STATEMENT_DROP_HEADS = (
    re.compile(r"^GRANT\b", re.IGNORECASE),
    re.compile(r"^REVOKE\b", re.IGNORECASE),
    re.compile(r"^CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\b", re.IGNORECASE),
    re.compile(r"^CREATE\s+(?:OR\s+REPLACE\s+)?RULE\b", re.IGNORECASE),
)


def extract_ddl(params: ConnectionParams, identity: Identity) -> str:
    """Run pg_dump for the relation and return the normalized DDL string."""

    raw = _run_pg_dump(params, identity)

    if not raw.strip():
        raise PostgresConnectionError(
            f"pg_dump produced no output for {identity.dotted()!r}; "
            "the table may not exist or be inaccessible.",
        )

    return normalize(raw)


def normalize(raw: str) -> str:
    """Apply the strip pipeline to raw pg_dump output."""

    lines = raw.splitlines()
    lines = _drop_single_line_patterns(lines)
    lines = _drop_banner_blocks(lines)
    lines = _drop_multi_line_statements(lines)
    lines = _strip_trailing_whitespace(lines)
    lines = _collapse_blank_runs(lines)
    lines = _trim_leading_and_trailing_blanks(lines)
    text = "\n".join(lines)

    if not text.endswith("\n"):
        text += "\n"

    return text


def _run_pg_dump(params: ConnectionParams, identity: Identity) -> str:
    """Shell out to pg_dump; traces the invocation at DEBUG (argv + elapsed, never the env).

    The `--table` pattern quotes each segment: pg_dump folds an unquoted one and matches nothing.
    """

    env = {**os.environ, **params.env_for_pg_dump()}
    argv = [
        PG_DUMP_BIN,
        "--schema-only",
        "--no-owner",
        "--no-privileges",
        f"--table={identity.quoted()}",
        params.database,
    ]
    started = time.monotonic()

    try:
        completed = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=PG_DUMP_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        _trace_pg_dump(started, argv, failed=True)

        raise PostgresConnectionError(
            f"pg_dump failed for {identity.dotted()!r}: exit {exc.returncode}; "
            f"stderr: {exc.stderr.strip()}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        _trace_pg_dump(started, argv, failed=True)

        raise PostgresConnectionError(
            f"pg_dump timed out after {PG_DUMP_TIMEOUT_SECONDS}s for {identity.dotted()!r}",
        ) from exc

    _trace_pg_dump(started, argv, failed=False)

    return completed.stdout


def _trace_pg_dump(started: float, argv: list[str], *, failed: bool) -> None:
    """Log a DEBUG record for the pg_dump invocation: argv, elapsed time, outcome."""

    _LOG.debug(
        "pg_dump conn=%s fqn=%s phase=%s elapsed_ms=%d failed=%s argv=%r",
        trace_context.connection.get(),
        trace_context.fqn.get(),
        trace_context.phase.get(),
        int((time.monotonic() - started) * 1000),
        failed,
        argv,
    )


def _drop_single_line_patterns(lines: list[str]) -> list[str]:
    out: list[str] = []

    for line in lines:
        if _HEADER_PREFIX_RE.match(line):
            continue

        if _FOOTER_LINE_RE.match(line):
            continue

        if _SET_LINE_RE.match(line):
            continue

        if _SET_CONFIG_LINE_RE.match(line):
            continue

        if _RESTRICT_LINE_RE.match(line):
            continue

        out.append(line)

    return out


def _drop_banner_blocks(lines: list[str]) -> list[str]:
    """Drop pg_dump's 3-line `-- Name: ...` banners and `--`/`--` pairs left by the strip."""

    out: list[str] = []
    i = 0

    while i < len(lines):
        if (
            lines[i] == "--"
            and i + 2 < len(lines)
            and lines[i + 1].startswith("-- Name: ")
            and lines[i + 2] == "--"
        ):
            i += 3
            continue

        if lines[i] == "--" and i + 1 < len(lines) and lines[i + 1] == "--":
            i += 2
            continue

        out.append(lines[i])
        i += 1

    return out


def _drop_multi_line_statements(lines: list[str]) -> list[str]:
    """Drop GRANT/REVOKE/CREATE TRIGGER/CREATE RULE statements (possibly multi-line)."""

    out: list[str] = []
    skipping = False

    for line in lines:
        if skipping:
            if _statement_terminator(line):
                skipping = False

            continue

        stripped = line.lstrip()

        if any(p.match(stripped) for p in _STATEMENT_DROP_HEADS):
            if _statement_terminator(line):
                continue  # single-line statement

            skipping = True
            continue

        out.append(line)

    return out


def _statement_terminator(line: str) -> bool:
    """True iff line ends a statement (semicolon outside trailing comment)."""

    code = line.split("--", 1)[0].rstrip()

    return code.endswith(";")


def _strip_trailing_whitespace(lines: list[str]) -> list[str]:
    return [line.rstrip() for line in lines]


def _collapse_blank_runs(lines: list[str]) -> list[str]:
    out: list[str] = []
    prev_blank = False

    for line in lines:
        is_blank = line == ""

        if is_blank and prev_blank:
            continue

        out.append(line)
        prev_blank = is_blank

    return out


def _trim_leading_and_trailing_blanks(lines: list[str]) -> list[str]:
    start = 0

    while start < len(lines) and lines[start] == "":
        start += 1

    end = len(lines)

    while end > start and lines[end - 1] == "":
        end -= 1

    return lines[start:end]
