"""fnmatch-based table selector matching.

Selectors are stdlib fnmatch globs over lowercased FQNs, so `*` crosses the dot separator of
`schema.table`. A pattern is folded before it is matched, so a selector means the same thing
whichever case it is written in; the FQN is expected lowercased from the adapter and is not
folded here. The CLI may narrow scope (include intersect, exclude union) but never widen it
beyond the project config.
"""

from __future__ import annotations

from fnmatch import fnmatchcase


def match(fqn: str, include: list[str], exclude: list[str]) -> bool:
    """True iff fqn matches an include pattern and no exclude; an empty include matches none."""

    if not _any_match(fqn, include):
        return False

    return not _any_match(fqn, exclude)


def covers(
    fqn: str,
    config_include: list[str],
    config_exclude: list[str],
    cli_include: list[str] | None = None,
    cli_exclude: list[str] | None = None,
) -> bool:
    """Whether a run under these four lists would scan `fqn`.

    A CLI include narrows the configured scope and can never widen it.
    """

    cli_inc = cli_include or []
    cli_exc = cli_exclude or []

    return (
        _any_match(fqn, config_include)
        and (not cli_inc or _any_match(fqn, cli_inc))
        and not _any_match(fqn, config_exclude)
        and not _any_match(fqn, cli_exc)
    )


def expand(
    fqns: list[str],
    config_include: list[str],
    config_exclude: list[str],
    cli_include: list[str] | None = None,
    cli_exclude: list[str] | None = None,
) -> list[str]:
    """Filter fqns through effective selectors, preserving input order."""

    return [f for f in fqns if covers(f, config_include, config_exclude, cli_include, cli_exclude)]


def _any_match(fqn: str, patterns: list[str]) -> bool:
    # `fnmatch` is not the shorter spelling of this: its normcase is identity off Windows, so
    # the fold has to be explicit. `lower`, not `casefold` - the FQN side is `lower`ed too.
    return any(fnmatchcase(fqn, pat.lower()) for pat in patterns)
