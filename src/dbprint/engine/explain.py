"""What one table resolves to under `.dbprint.yaml`, and why: config plus manifest in, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dbprint.config import ConnectionConfig, match
from dbprint.config.project import ResolvedProfile, RuleMatch
from dbprint.spec.profile import effective_profile
from .baseline import ManifestRead, unusable_manifest_message
from .carried import profile_changes


CountSource = Literal["print", "database", "none"]


@dataclass(frozen=True)
class TableExplanation:
    """A table's resolved profile with the facts it was resolved against.

    `fqn` None explains the connection, whose `rules` then list matchers with no verdict;
    `row_count` is what size rules were tested against, and `note` says why there is none.
    """

    connection: str
    fqn: str | None
    printed: bool
    in_scope: bool
    object_type: str | None
    row_count: int | None
    count_source: CountSource
    profiled_at: str | None
    note: str | None
    profile: ResolvedProfile
    ceiling: tuple[int, float | None] | None = None
    recorded_changes: tuple[str, ...] = ()
    rules: tuple[tuple[str, RuleMatch], ...] = ()


def explain(
    conn: ConnectionConfig,
    fqn: str | None,
    manifest: ManifestRead,
    *,
    estimate: int | None = None,
    online: bool = False,
) -> TableExplanation:
    """Explain `fqn` under `conn`: offline against the print's recorded count, else `estimate`."""

    if fqn is None:
        return TableExplanation(
            connection=conn.name,
            fqn=None,
            printed=False,
            in_scope=True,
            object_type=None,
            row_count=None,
            count_source="none",
            profiled_at=None,
            note=None,
            profile=conn.resolve_profile(None),
            rules=tuple(
                (rule.label or f"rules[{index}]", rule.match)
                for index, rule in enumerate(conn.rules)
            ),
        )

    entry = (manifest.manifest or {}).get("tables", {}).get(fqn)
    entry = entry if isinstance(entry, dict) else None
    object_type = entry.get("type") if entry is not None else None
    count, source = _row_count(entry, estimate, online=online)

    # A queried view records a row_count, yet generate never tests a size rule against a view.
    if object_type == "view":
        count, source = None, "none"

    kind = "view" if object_type == "view" else "table"
    settings = conn.settings_for(fqn, kind, count)
    recorded = (manifest.manifest or {}).get("profile")
    changes = (
        tuple(
            profile_changes(
                effective_profile(recorded, entry.get("profile")),
                settings.profile,
                object_type,
            ),
        )
        if entry is not None and isinstance(recorded, dict)
        else ()
    )

    return TableExplanation(
        connection=conn.name,
        fqn=fqn,
        printed=entry is not None,
        in_scope=match(fqn, list(conn.tables.include), list(conn.tables.exclude)),
        object_type=object_type,
        row_count=count,
        count_source=source,
        profiled_at=entry.get("profiled_at") if entry is not None else None,
        note=_note(manifest, conn.name, entry, object_type, source, online=online),
        profile=conn.resolve_profile(fqn, count),
        ceiling=(
            (settings.max_rows_scanned, settings.sample)
            if settings.max_rows_scanned is not None
            else None
        ),
        recorded_changes=changes,
    )


def _row_count(
    entry: dict | None,
    estimate: int | None,
    *,
    online: bool,
) -> tuple[int | None, CountSource]:
    if online:
        return estimate, "database" if estimate is not None else "none"

    recorded = entry.get("row_count") if entry is not None else None

    if isinstance(recorded, int) and not isinstance(recorded, bool):
        return recorded, "print"

    return None, "none"


def _note(
    manifest: ManifestRead,
    connection: str,
    entry: dict | None,
    object_type: str | None,
    source: CountSource,
    *,
    online: bool,
) -> str | None:
    if manifest.manifest is None and not online:
        return unusable_manifest_message(manifest, connection)

    if entry is None and not online:
        return "not in the print: matched by name only"

    if object_type == "view":
        return "a view has no row-count estimate: size rules never apply"

    if source == "none":
        return "the database returned no estimate" if online else "the print records no row count"

    return None
