"""Every committed print records the producer version that would write it today.

The set is swept rather than listed, so a print added later is covered without an edit here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from dbprint import __version__


REPO_ROOT = Path(__file__).resolve().parents[1]

# `diff.yaml` records the producer of the baseline it compared against, `manifest.yaml` its own.
ARTIFACT_NAMES = ("manifest.yaml", "diff.yaml")

# The reference example outlives any single print, so its manifest anchors the sweep.
ANCHOR = "docs/format/v1/examples/production/prints/production/manifest.yaml"

_UNSWEPT = frozenset({".git", ".venv", "__pycache__", "node_modules", "dist", "build"})


def committed_artifacts() -> list[Path]:
    """Repository-relative paths of every committed manifest and diff."""

    found = [
        path.relative_to(REPO_ROOT)
        for name in ARTIFACT_NAMES
        for path in REPO_ROOT.rglob(name)
        if _UNSWEPT.isdisjoint(path.parts)
    ]

    return sorted(found)


def recorded_versions(path: Path) -> list[str]:
    """Every non-null `dbprint_version` in one artifact, nested blocks included.

    A diff records one per side, and a baseline that carried no producer records `null`.
    """

    document = yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))

    if not isinstance(document, dict):
        return []

    blocks = [document, *(value for value in document.values() if isinstance(value, dict))]

    return [
        block["dbprint_version"] for block in blocks if block.get("dbprint_version") is not None
    ]


class TestEveryCommittedStampIsCurrent:
    def test_the_sweep_reaches_the_committed_prints(self) -> None:
        """A clean result means nothing if the walk enumerated no artifact."""

        swept = [path.as_posix() for path in committed_artifacts()]

        assert ANCHOR in swept, f"the sweep missed the reference example; found {swept}"

    def test_at_least_one_artifact_records_a_producer(self) -> None:
        """Every stamp reading `null` would also pass the comparison below."""

        recorded = [v for path in committed_artifacts() for v in recorded_versions(path)]

        assert recorded, "no committed artifact records a producer version"

    def test_no_committed_artifact_records_another_producer(self) -> None:
        stale = [
            f"  {path.as_posix()}: {value}"
            for path in committed_artifacts()
            for value in recorded_versions(path)
            if value != __version__
        ]

        assert not stale, (
            f"committed artifacts record a producer other than {__version__}; "
            "regenerate the print that records each one.\n" + "\n".join(stale)
        )
