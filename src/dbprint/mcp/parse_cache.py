"""Parsed print files kept across calls, each re-checked against its file on disk (MCP.md 7.1)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbprint.spec import artifact_yaml


@dataclass
class ParseCache:
    """One server's parsed files, keyed on path and reused while the file's identity is unchanged.

    A hit returns the held object itself, so every reader treats it as read-only.
    """

    _entries: dict[Path, tuple[tuple[int, int, int], Any]] = field(default_factory=dict)

    def read(self, path: Path) -> Any:
        """Parse `path`, or return the held parse; raises OSError or `yaml.YAMLError` uncached."""

        with path.open(encoding="utf-8") as handle:
            # Identity from the open descriptor, so a rename between stat and read cannot pair
            # one file's identity with another's content.
            stat = os.fstat(handle.fileno())
            identity = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            held = self._entries.get(path)

            if held is not None and held[0] == identity:
                return held[1]

            data = artifact_yaml.load(handle.read())

        self._entries[path] = (identity, data)

        return data
