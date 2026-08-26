"""Baseline manifest loading + per-table state hydration from disk.

Column/index/comment fields stay empty in v1, where those live in DDL the format does not
require parsing. A malformed artifact degrades to absent rather than crashing - the smallest
unit holding the defect drops - and `conformance.validate_print` reports it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from . import diff as diff_module
from .relationship_graph import IncomingFk


_LOG = logging.getLogger(__name__)


def load_baseline_manifest(prints_root: Path) -> dict[str, Any] | None:
    """Load `prints/<conn>/manifest.yaml` if present and usable; otherwise None."""

    manifest = prints_root / "manifest.yaml"

    if not manifest.is_file():
        return None

    try:
        data = yaml.safe_load(manifest.read_text())
    except yaml.YAMLError:
        return None

    reason = manifest_shape_error(data)

    if reason is not None:
        _LOG.warning("ignoring %s: %s", manifest, reason)

        return None

    return data if isinstance(data, dict) else None


def manifest_shape_error(data: Any) -> str | None:
    """Why a parsed manifest is unusable, or None when its readers can walk it.

    Usable means the document is a mapping and its `tables` is one too. An empty file is
    usable-as-absent; a written-but-empty `tables` is not, `dict.get` being unable to tell
    it from a never-written key.
    """

    if data is None:
        return None

    if not isinstance(data, dict):
        return f"expected a mapping, found {type(data).__name__}"

    if "tables" in data and not isinstance(data["tables"], dict):
        found = "nothing" if data["tables"] is None else type(data["tables"]).__name__

        return f"`tables` must be a mapping of table name to entry, found {found}"

    return None


def walkable_tables(manifest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The manifest entries a reader can follow, keyed by table name.

    Followable means a mapping whose `path`, if present, is a string. One unusable entry
    drops its own table and nothing else; the conformance suite is what reports it.
    """

    out: dict[str, dict[str, Any]] = {}

    for fqn, entry in ((manifest or {}).get("tables") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("path", ""), str):
            out[fqn] = entry

    return out


def declared_artifacts(entry: dict[str, Any]) -> dict[str, Any]:
    """The artifact filenames a reader can open from one manifest entry; non-strings drop."""

    artifacts = entry.get("artifacts") or {}

    if not isinstance(artifacts, dict):
        return {}

    return {kind: name for kind, name in artifacts.items() if isinstance(name, str)}


def missing_artifacts(table_dir: Path, artifacts: dict[str, Any]) -> tuple[str, ...]:
    """Declared kinds whose file is absent from `table_dir`, sorted - a broken promise.

    Not a kind `artifacts` never names, and not a corrupt-but-present file (SPEC 2.5).
    """

    return tuple(
        sorted(kind for kind, name in artifacts.items() if not (table_dir / name).is_file()),
    )


def baseline_states_from_manifest(
    baseline_manifest: dict[str, Any] | None,
) -> dict[str, diff_module.TableState] | None:
    """Build a thin TableState index from the manifest's table list.

    Only `type` is set; `hydrate_baseline_states` fills the rest from the per-table YAML.
    """

    if not baseline_manifest:
        return None

    out: dict[str, diff_module.TableState] = {}

    for fqn, entry in walkable_tables(baseline_manifest).items():
        out[fqn] = diff_module.TableState(fqn=fqn, type=entry.get("type", "table"))

    return out


def hydrate_baseline_states(
    states: dict[str, diff_module.TableState] | None,
    prints_root: Path,
    baseline_manifest: dict[str, Any] | None,
) -> None:
    """Fill `states` in-place from each table's relationships.yaml + statistics.yaml."""

    if not states or not baseline_manifest:
        return

    for fqn, entry in walkable_tables(baseline_manifest).items():
        if fqn not in states:
            continue

        tbl_dir = prints_root / entry.get("path", "")
        artifacts = declared_artifacts(entry)

        if "relationships" in artifacts:
            _hydrate_relationships(states[fqn], tbl_dir / artifacts["relationships"])

        if "statistics" in artifacts:
            _hydrate_statistics(states[fqn], tbl_dir / artifacts["statistics"])


def load_incoming_edges(
    prints_root: Path,
    baseline_manifest: dict[str, Any] | None,
) -> dict[str, list[IncomingFk]]:
    """Read every committed table's `referenced_by` list.

    A run resolves incoming edges only from the tables it re-extracted; edges from tables it
    left alone come from the prints that last recorded them.
    """

    out: dict[str, list[IncomingFk]] = {}

    if not baseline_manifest:
        return out

    for fqn, entry in walkable_tables(baseline_manifest).items():
        artifacts = declared_artifacts(entry)

        if "relationships" not in artifacts:
            continue

        edges = _incoming_from_file(
            prints_root / entry.get("path", "") / artifacts["relationships"],
        )

        if edges:
            out[fqn] = edges

    return out


def _incoming_from_file(path: Path) -> list[IncomingFk]:
    if not path.is_file():
        return []

    try:
        data = _as_mapping(yaml.safe_load(path.read_text()), path)
    except yaml.YAMLError:
        return []

    if data is None:
        return []

    out: list[IncomingFk] = []

    for entry in data.get("referenced_by") or []:
        if not isinstance(entry, dict):
            continue

        try:
            out.append(
                IncomingFk(
                    column=tuple(entry["column"]),
                    referencer_table=entry["referencer_table"],
                    referencer_column=tuple(entry["referencer_column"]),
                    # Absent on an inferred edge (SPEC 2.3.8) - `.get`, or the entry is dropped.
                    on_delete=entry.get("on_delete", "NO ACTION"),
                    on_update=entry.get("on_update", "NO ACTION"),
                    detection=entry.get("detection", "declared"),
                    constraint_name=entry.get("constraint_name"),
                ),
            )
        except (KeyError, TypeError):
            continue

    return out


def _hydrate_relationships(state: diff_module.TableState, path: Path) -> None:
    if not path.is_file():
        return

    try:
        data = _as_mapping(yaml.safe_load(path.read_text()), path)
    except yaml.YAMLError:
        return

    if data is None:
        return

    refers_to = data.get("refers_to") or []
    fks: list[diff_module.FkState] = []

    for entry in refers_to:
        try:
            fks.append(
                diff_module.FkState(
                    source_columns=tuple(entry["column"]),
                    target_table=entry["target_table"],
                    target_columns=tuple(entry["target_column"]),
                    # Absent on an inferred edge (SPEC 2.3.8) - `.get`, or the entry is dropped.
                    on_delete=entry.get("on_delete", "NO ACTION"),
                    on_update=entry.get("on_update", "NO ACTION"),
                    detection=entry.get("detection", "declared"),
                ),
            )
        except (KeyError, TypeError):
            continue

    state.relationships = fks


def _hydrate_statistics(state: diff_module.TableState, path: Path) -> None:
    if not path.is_file():
        return

    try:
        data = _as_mapping(yaml.safe_load(path.read_text()), path)
    except yaml.YAMLError:
        return

    if data is None:
        return

    row_count = data.get("row_count")
    state.row_count = row_count if isinstance(row_count, int) else None
    row_count_method = data.get("row_count_method")
    state.row_count_method = row_count_method if isinstance(row_count_method, str) else None
    state.scoped = isinstance(data.get("scope"), dict)
    state.catalog_only = data.get("catalog_only") is True
    state.grain = diff_module.grain_from_block(data.get("grain"))
    state.physical_layout = diff_module.physical_layout_from_block(data.get("physical_layout"))

    cols = data.get("columns") or {}

    if not isinstance(cols, dict):
        return

    state.statistics = diff_module.comparable_columns(cols)

    # v1 statistics.yaml carries no default, so default_known=False suppresses default drift.
    state.columns = {
        name: diff_module.ColumnState(
            name=name,
            sql_type=str(payload.get("sql_type", "")),
            nullable=bool(payload.get("nullable", False)),
            default=None,
            default_known=False,
        )
        for name, payload in cols.items()
        if isinstance(payload, dict)
    }


def _as_mapping(data: Any, path: Path) -> dict[str, Any] | None:
    """One artifact as the mapping its reader assumes, or None with the file named."""

    if isinstance(data, dict):
        return data

    if data is not None:
        _LOG.warning("ignoring %s: expected a mapping, found %s", path, type(data).__name__)

    return None
