"""The claims register: one table of consumer-visible states and the obligation each imposes.

Each entry names a state the adversarial print (tests/fixtures/adversarial.py) carries and
the property a correct rendering must hold. A per-surface module declares what it asserts in
a module-level `COVERS` frozenset; test_register_coverage.py fails any listed surface short
of the full register. Listing is manual, so a new surface module inherits nothing until it
is added to `_SURFACES`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClaimState:
    """One register entry: a fixture state and the obligation it imposes on every surface."""

    key: str
    obligation: str


REGISTER: tuple[ClaimState, ...] = (
    ClaimState(
        "scoped_table",
        "States the scanned population for the scoped table; never presents a scanned-set "
        "count as if it covered the whole table.",
    ),
    ClaimState(
        "redacted_column",
        "Never orders, plots or compares a masked bound; never presents the redaction "
        "placeholder as if it were a real value.",
    ),
    ClaimState(
        "future_dated_temporal",
        "Never reports the future-dated temporal column as fresh without naming the "
        "SPEC 2.2.4 zero-floor clamp.",
    ),
    ClaimState(
        "truncated_fk_values",
        "States that the foreign key's value list is partial wherever it shows the list.",
    ),
    ClaimState(
        "unevaluated_diff_table",
        "Never reports an unevaluated table as unchanged.",
    ),
    ClaimState(
        "empty_columns_map",
        "Says the scan read nothing for the empty-columns table, never 'no columns'.",
    ),
    ClaimState(
        "approximate_row_count",
        "Never presents the approximate row count's derived difference as measured growth.",
    ),
    ClaimState(
        "incomplete_grain_search",
        "Never reports a grain search that gave up (exhausted: false) as if it had ruled "
        "out every key.",
    ),
    ClaimState(
        "catalog_only_table",
        "Never presents a catalog-only object's absent dependencies or physical_layout as "
        "a measured emptiness - the marker means nobody looked, not that nothing was found.",
    ),
    ClaimState(
        "declared_missing_artifact",
        "Names a declared artifact kind whose file is absent from disk, distinguishably "
        "from a kind the manifest never declared for that table.",
    ),
)

REGISTER_KEYS: frozenset[str] = frozenset(state.key for state in REGISTER)


def assert_full_coverage(surface: str, covers: frozenset[str]) -> None:
    """Fail naming exactly the register entries `surface` does not assert.

    A stale key in `covers` also fails, so a renamed or removed entry leaves no dead coverage.
    """

    missing = sorted(REGISTER_KEYS - covers)
    stale = sorted(covers - REGISTER_KEYS)
    problems = []

    if missing:
        problems.append(f"does not assert: {missing}")

    if stale:
        problems.append(f"asserts entries no longer in the register: {stale}")

    assert not problems, f"{surface} " + "; ".join(problems)
