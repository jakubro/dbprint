"""The roots the suite must leave untouched, and the comparisons that prove it did.

Two rules, because one does not cover both kinds of root. Nothing but this suite writes under
a private root, so any new entry there is an escape. A package manager does write under
`/var/lib`, so only entries carrying the suite's own prefix count there. The comparisons are
pure, so they can be tested without writing anywhere.
"""

from __future__ import annotations

from pathlib import Path


PRIVATE_ROOTS: tuple[Path, ...] = (Path("/root"), Path.home())

PROVISIONED_ROOTS: tuple[Path, ...] = (Path("/var/lib"), Path("/var/lib/postgresql"))

SUITE_PREFIX = "dbprint"


def snapshot(roots: tuple[Path, ...] = PRIVATE_ROOTS) -> dict[str, frozenset[str]]:
    """Entry names under each root that exists, keyed by root.

    A root that does not exist is omitted rather than recorded empty, so one appearing
    mid-run reads as new entries rather than as no change.
    """

    return {
        str(root): frozenset(entry.name for entry in root.iterdir())
        for root in dict.fromkeys(roots)
        if root.is_dir()
    }


def escaped(
    before: dict[str, frozenset[str]],
    after: dict[str, frozenset[str]],
) -> dict[str, list[str]]:
    """Entries that appeared under a watched root between two snapshots.

    Disappearances are not reported: the suite removing something it did not create is a
    different fault, and a root the run deleted entirely cannot be described by this shape.
    """

    appeared = {
        root: sorted(names - before.get(root, frozenset())) for root, names in after.items()
    }

    return {root: names for root, names in appeared.items() if names}


def suite_entries(
    roots: tuple[Path, ...] = PROVISIONED_ROOTS,
    prefix: str = SUITE_PREFIX,
) -> list[str]:
    """Suite-named entries under a root that must hold none of them."""

    return sorted(
        str(entry)
        for root in dict.fromkeys(roots)
        if root.is_dir()
        for entry in root.iterdir()
        if entry.name.startswith(prefix)
    )
