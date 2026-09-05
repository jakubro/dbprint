"""The landing page's recording must be what the CLI emits, and must stay replayable.

`site/public/demo.cast` is a golden over `scripts/gen_demo_cast.py`, as the reference example is.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import PostgresCluster


REPO_ROOT = Path(__file__).resolve().parents[2]
CAST = REPO_ROOT / "site/public/demo.cast"

# What the recording claims to run. A frame carries one keystroke, so the commands are read off
# the concatenated stream rather than off any single frame.
EXPECTED_COMMANDS = (
    "dbprint init",
    "dbprint generate",
    "dbprint list",
    "dbprint context public.taxon --no-stats --no-relationships",
    "dbprint context public.taxon --no-ddl",
)


def _header() -> dict[str, Any]:
    return json.loads(CAST.read_text().splitlines()[0])


def _frames() -> list[list[Any]]:
    return [json.loads(line) for line in CAST.read_text().splitlines()[1:]]


def _screen() -> str:
    return "".join(frame[2] for frame in _frames())


class TestTheFileIsAValidRecording:
    def test_the_header_declares_asciicast_v2(self) -> None:
        assert _header()["version"] == 2

    def test_the_header_sizes_the_terminal(self) -> None:
        header = _header()

        assert header["width"] > 0
        assert header["height"] > 0

    def test_every_frame_is_an_output_event_in_time_order(self) -> None:
        frames = _frames()
        times = [frame[0] for frame in frames]

        assert frames, "the recording carries no frames"
        assert all(frame[1] == "o" for frame in frames)
        assert times == sorted(times)


class TestTheRecordingShowsWhatItClaims:
    @pytest.mark.parametrize("command", EXPECTED_COMMANDS)
    def test_the_command_is_typed_out(self, command: str) -> None:
        assert command in _screen()

    def test_the_profiled_table_reports_its_rows(self) -> None:
        """A recording that showed the commands but no output would pass the check above."""

        screen = _screen()

        assert "300 rows" in screen
        assert "4 ok  0 failed  0 skipped" in screen

    def test_the_statistics_block_carries_a_measured_column(self) -> None:
        """`dbprint context` renders no raw YAML key - `biome`'s Notes cell states the same
        facts in prose, which is what a reader of this fragment actually sees.
        """

        screen = _screen()

        assert "3 distinct: arid / temperate / tropical, uniform" in screen
        assert "4% null" in screen


class TestGeneratorAgreement:
    """The committed recording must be what a real run emits, byte for byte."""

    def test_regenerating_reproduces_the_committed_cast(
        self,
        postgres_cluster: PostgresCluster,
    ) -> None:
        generator = _load_generator()
        credentials = {
            "host": "127.0.0.1",
            "port": str(postgres_cluster.port),
            "database": generator.DATABASE,
            "user": postgres_cluster.superuser,
            "password": "postgres",
        }
        generator._create_database(credentials)

        assert generator.build_cast(credentials) == CAST.read_text(), (
            "the committed recording is not what the CLI emits; run `just demo`"
        )


def _load_generator() -> Any:
    """Import `scripts/gen_demo_cast.py` by path; `scripts/` carries no `__init__.py`."""

    path = REPO_ROOT / "scripts" / "gen_demo_cast.py"
    spec = importlib.util.spec_from_file_location("gen_demo_cast", path)

    if spec is None or spec.loader is None:
        pytest.fail(f"could not load {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module
