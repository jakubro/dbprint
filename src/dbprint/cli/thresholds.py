"""Per-table freshness thresholds for the commands that hold no database.

`check` and `list` judge a committed print against the threshold each table was profiled
under, falling back to the rule cascade when an entry records none. Neither connects, so
both report which tables the cascade refuses and which rules went unapplied for want of a
row count. The whole manifest resolves up front; a resolver called per table mid-walk
raises mid-iteration, leaving the caller holding half a verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from dbprint.config import ConfigError, ConnectionConfig


@dataclass(frozen=True)
class OfflineThresholds:
    """The freshness threshold for every manifest entry, and what did not settle.

    `refused` maps a table to why it has no threshold - its own rules raised, or its recorded
    `max_age_days` is a number SPEC 2.5 forbids - and the connection default is never
    substituted for one. `size_gated` names tables whose fallback ignored a `min_rows` rule
    that selects them, so their `resolved` number came from name-matching rules alone.
    """

    resolved: dict[str, float] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    size_gated: tuple[str, ...] = ()

    def threshold_for(self, fqn: str) -> float:
        """The threshold one table is judged against; only settled tables are asked."""

        return self.resolved[fqn]


def resolve(conn: ConnectionConfig, manifest: dict[str, Any] | None) -> OfflineThresholds:
    """Settle every entry's threshold: the one it records, else what its rules resolve to.

    Fallback is per entry, not per manifest: a partial re-extract leaves entries carrying
    the field beside inherited ones that do not. A non-numeric `max_age_days` is treated as
    absent; one SPEC 2.5 forbids (negative, or not whole) is refused, as is a table whose own
    rules raise - refusals are per table, so one bad config cannot drop a neighbour's verdict.
    """

    resolved: dict[str, float] = {}
    refused: dict[str, str] = {}
    size_gated: list[str] = []

    for fqn, entry in ((manifest or {}).get("tables") or {}).items():
        entry_type = entry.get("type") if isinstance(entry, dict) else None
        recorded = entry.get("max_age_days") if isinstance(entry, dict) else None

        if not isinstance(recorded, bool) and isinstance(recorded, (int, float)):
            if recorded < 0:
                refused[fqn] = (
                    f"{fqn}: max_age_days is {recorded}, which no print can ever satisfy: "
                    f"every table re-extracts on every run and check reports every one of "
                    f"them stale. Use 0 to ask for that deliberately, or a positive number "
                    f"of days."
                )
            elif not float(recorded).is_integer():
                refused[fqn] = (
                    f"{fqn}: max_age_days is {recorded}, which is not a whole number of days."
                )
            else:
                resolved[fqn] = float(recorded)

            continue

        try:
            resolved[fqn] = float(conn.settings_for(fqn).max_age_days)
        except ConfigError as exc:
            refused[fqn] = str(exc)
            continue

        # A plain view is never queried, so no size condition can have governed it.
        if entry_type != "view" and conn.min_rows_conditions_name(fqn):
            size_gated.append(fqn)

    return OfflineThresholds(
        resolved=resolved,
        refused=refused,
        size_gated=tuple(size_gated),
    )


def size_gate_warning(connection_name: str, size_gated: Sequence[str]) -> str:
    """Say which thresholds resolved without the size conditions that select them.

    Offline there is no row count, so a rule carrying `min_rows` is left unapplied; silence
    would leave a user assuming a size-gated `max_age_days` governed the verdict.
    """

    tables = ", ".join(size_gated)

    return (
        f"{connection_name}: no row count is available without a database, so rules carrying "
        f"`min_rows` did not apply to {tables}. Their thresholds come from the rules matching "
        f"by name alone, or from the connection."
    )
