"""fnmatch-based table selector matching.

Selectors are stdlib fnmatch globs over lowercased FQNs, so `*` crosses the dot separator of
`schema.table`. The CLI may narrow scope (include intersect, exclude union) but never widen
it beyond the project config.
"""

from __future__ import annotations

from fnmatch import fnmatchcase


def match(fqn: str, include: list[str], exclude: list[str]) -> bool:
    """True iff fqn matches an include pattern and no exclude; an empty include matches none."""

    if not _any_match(fqn, include):
        return False

    return not _any_match(fqn, exclude)


def expand(
    fqns: list[str],
    config_include: list[str],
    config_exclude: list[str],
    cli_include: list[str] | None = None,
    cli_exclude: list[str] | None = None,
) -> list[str]:
    """Filter fqns through effective selectors: CLI narrows scope, never widens it.

    `cli_include` intersects `config_include`, `cli_exclude` unions with `config_exclude`,
    and input order is preserved.
    """

    cli_inc = cli_include or []
    cli_exc = cli_exclude or []

    return [
        f
        for f in fqns
        if _any_match(f, config_include)
        and (not cli_inc or _any_match(f, cli_inc))
        and not _any_match(f, config_exclude)
        and not _any_match(f, cli_exc)
    ]


def _any_match(fqn: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(fqn, pat) for pat in patterns)
