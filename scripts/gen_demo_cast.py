"""Regenerate the landing page's terminal recording from a real CLI run.

Frame contents are the CLI's own bytes; timings are synthesized so the cast stays byte-stable
and golden-testable. Run via `just demo`, which needs a local `postgres` as `just example` does.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import secrets
import shutil
import time
import types
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast

import yaml
from rich.live import Live


REPO_ROOT = Path(__file__).resolve().parents[1]
CAST_PATH = REPO_ROOT / "site/public/demo.cast"

# A fixed root, not a temp dir: `dbprint init` echoes absolute paths, so a per-run name would
# change the cast. `/var/tmp` because a container may mount `/tmp`, and `cwd` resolves to that.
DEMO_ROOT = Path("/var/tmp/dbprint-demo")
PROJECT_DIR = DEMO_ROOT / "seedbank"
HOME_DIR = DEMO_ROOT / "user-home"

DATABASE = "seedbank"
CONNECTION = "primary"
# The scaffold's own default include is `public.*`, so seeding there is what lets the recording
# run `init` and `generate` back to back with nothing edited in between.
SCHEMA = "public"

# The terminal the player replays into.
WIDTH = 100
HEIGHT = 32

# Synthesized frame timings, in seconds.
_KEYSTROKE = 0.09  # per typed character
_BEFORE_OUTPUT = 0.35  # between the submitting newline and the first output byte
_AFTER_OUTPUT = 1.6  # reading pause before the next prompt
_FINAL_HOLD = 4.0  # how long the closing screen stays up before the player loops

PROMPT = "\x1b[1;32m$\x1b[0m "

# Removed for the run - each silently changes what the CLI writes: `TTY_COMPATIBLE` outranks
# `FORCE_COLOR` and can force the piped renderer, `COLORTERM` widens it, `NO_COLOR` strips it.
_NEUTRALIZED_ENVIRONMENT_KEYS = ("NO_COLOR", "TTY_COMPATIBLE", "COLORTERM")

# Everything the run touches in the environment, saved and restored around it.
_ENVIRONMENT_KEYS = (
    "HOME",
    "COLUMNS",
    "LINES",
    "FORCE_COLOR",
    "TERM",
    *_NEUTRALIZED_ENVIRONMENT_KEYS,
)

# Every stamped and compared instant collapses here, so a regenerated cast carries the same
# `profiled_at` and the same freshness verdict however long after the last run it is made.
FROZEN_NOW = datetime(2026, 5, 17, 22, 48, 1, tzinfo=UTC)

SCHEMA_SQL = """
CREATE TABLE taxon (
    id              integer PRIMARY KEY,
    scientific_name text NOT NULL,
    family          text NOT NULL,
    biome           text
);

CREATE TABLE collector (
    id          integer PRIMARY KEY,
    full_name   text NOT NULL,
    institution text
);

CREATE TABLE accession (
    id            integer PRIMARY KEY,
    taxon_id      integer NOT NULL REFERENCES taxon(id),
    collector_id  integer REFERENCES collector(id),
    collected_on  date NOT NULL,
    seed_count    integer NOT NULL,
    viability_pct numeric(5, 2),
    notes         text
);

CREATE VIEW taxon_names AS SELECT id, scientific_name FROM taxon;
"""

# Every value derives from the row ordinal, so a regenerated print reports the same statistics.
SEED_SQL = """
INSERT INTO taxon (id, scientific_name, family, biome)
SELECT g,
       'Taxon ' || g,
       (ARRAY['Fabaceae', 'Poaceae', 'Asteraceae', 'Rosaceae'])[1 + (g % 4)],
       CASE WHEN g % 25 = 0 THEN NULL
            ELSE (ARRAY['temperate', 'tropical', 'arid'])[1 + (g % 3)] END
FROM generate_series(1, 300) g;

/* Offset past taxon's own id domain (1-300): a subset range here reads as a candidate foreign
   key into taxon by pure numeric coincidence, adding two inferred edges the schema never meant. */
INSERT INTO collector (id, full_name, institution)
SELECT 1000 + g, 'Collector ' || g, (ARRAY['Kew', 'Missouri', 'Svalbard'])[1 + (g % 3)]
FROM generate_series(1, 120) g;

INSERT INTO accession
    (id, taxon_id, collector_id, collected_on, seed_count, viability_pct, notes)
SELECT g,
       1 + (g % 300),
       CASE WHEN g % 40 = 0 THEN NULL ELSE 1000 + 1 + (g % 120) END,
       DATE '2019-03-01' + ((g % 2000) * INTERVAL '1 day'),
       (g * 37) % 5000,
       ROUND(((g % 97) + 3)::numeric, 2),
       CASE WHEN g % 7 = 0 THEN 'field-collected' ELSE NULL END
FROM generate_series(1, 2500) g;
"""


class FrozenDatetime(datetime):
    """A `datetime` whose `now` is `FROZEN_NOW` - what a stamp and a freshness read agree on."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        del tz

        return FROZEN_NOW


class EventDrivenLive(Live):
    """A live footer that redraws on `update` rather than on Rich's timer.

    A timed redraw writes a frame count that depends on the run's wall time; events do not.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **{**kwargs, "auto_refresh": False})

    def update(self, renderable: Any, *, refresh: bool = False) -> None:
        del refresh

        super().update(renderable, refresh=True)


class SteppedClock(types.ModuleType):
    """A `time` stand-in whose `monotonic` steps a fixed amount per call; the rest forwards.

    Printed durations are `monotonic` deltas, so a real clock would vary the cast between runs.
    """

    def __init__(self, step: float = 0.041) -> None:
        super().__init__("time")
        self._step = step
        self._now = 0.0

    def monotonic(self) -> float:
        self._now += self._step

        return self._now

    def peek(self) -> float:
        """Current stepped time, without advancing it."""

        return self._now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class _RecordingStream(io.StringIO):
    """A text stream that timestamps each write against a shared clock, in call order."""

    def __init__(self, events: list[tuple[float, str]], clock: SteppedClock) -> None:
        super().__init__()
        self._events = events
        self._clock = clock

    def write(self, text: str) -> int:
        if text:
            self._events.append((self._clock.peek(), text))

        return super().write(text)


def build_cast(credentials: dict[str, str]) -> str:
    """Drive the CLI through the first-print sequence and return the asciicast v2 text."""

    _prepare_workspace(credentials)
    frames: list[tuple[float, str]] = []
    at = 0.0

    for command, events in _session(credentials):
        at = _type_command(frames, at, command)
        at = _emit_events(frames, at, events)

    frames.append((at, PROMPT))
    # A trailing empty frame writes no bytes but extends the recording, holding the closing screen.
    frames.append((at + _FINAL_HOLD, ""))
    header = {
        "version": 2,
        "width": WIDTH,
        "height": HEIGHT,
        "title": "dbprint: a first print",
        "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
    }
    lines = [json.dumps(header, separators=(",", ":"))]
    lines += [json.dumps([round(t, 3), "o", data], separators=(",", ":")) for t, data in frames]

    return "\n".join(lines) + "\n"


def _session(credentials: dict[str, str]) -> list[tuple[str, list[tuple[float, str]]]]:
    """The command/event-stream pairs the recording shows, in order.

    Every line is really run in the demo project, so a reader who runs it gets the same screen.
    """

    # DDL first, statistics second - the recording's closing frame, held longest by a looping
    # player, is the Cardinality table rather than the schema any other tool already shows.
    table = f"{SCHEMA}.taxon"

    return [
        ("dbprint init", _run_cli(["init"])),
        ("dbprint generate", _run_cli(["generate"], credentials=credentials)),
        ("dbprint list", _run_cli(["list"])),
        (
            f"dbprint context {table} --no-stats --no-relationships",
            _run_cli(["context", table, "--no-stats", "--no-relationships"]),
        ),
        (
            f"dbprint context {table} --no-ddl",
            _run_cli(["context", table, "--no-ddl"]),
        ),
    ]


def _type_command(frames: list[tuple[float, str]], at: float, command: str) -> float:
    """Push the prompt, one frame per typed character, and the newline that submits it."""

    frames.append((at, PROMPT))
    at += _KEYSTROKE

    for char in command:
        frames.append((at, char))
        at += _KEYSTROKE

    frames.append((at, "\r\n"))

    return at + _BEFORE_OUTPUT


def _run_cli(argv: list[str], credentials: dict[str, str] | None = None) -> list[tuple[float, str]]:
    """Invoke the real CLI in-process; return its writes as (elapsed, chunk), in call order.

    In-process for the event-driven live footer; one clock times both streams into real order.
    """

    from dbprint.cli.main import main

    clock = SteppedClock()
    events: list[tuple[float, str]] = []
    stdout = _RecordingStream(events, clock)
    stderr = _RecordingStream(events, clock)
    previous = Path.cwd()
    os.chdir(PROJECT_DIR)

    try:
        with (
            _patched_environment(credentials, clock),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                main.main(args=argv, prog_name="dbprint", standalone_mode=False)
            except SystemExit as signal:
                if signal.code not in (0, None):
                    raise SystemExit(
                        f"dbprint {' '.join(argv)} exited {signal.code}:\n{stderr.getvalue()}",
                    ) from signal
    finally:
        os.chdir(previous)

    return events


def _emit_events(
    frames: list[tuple[float, str]],
    at: float,
    events: list[tuple[float, str]],
) -> float:
    """Append one frame per distinct write instant, grouping same-instant writes together.

    Guards a CRLF split across a write boundary: grouped-then-converted must match whole-converted.
    """

    if not events:
        return at + _AFTER_OUTPUT

    grouped: list[tuple[float, str]] = []

    for ts, chunk in events:
        if grouped and grouped[-1][0] == ts:
            grouped[-1] = (ts, grouped[-1][1] + chunk)
        else:
            grouped.append((ts, chunk))

    converted = [(ts, _as_terminal_bytes(chunk)) for ts, chunk in grouped]
    whole = _as_terminal_bytes("".join(chunk for _, chunk in events))

    if "".join(text for _, text in converted) != whole:
        raise SystemExit("a write boundary split a CRLF sequence - per-group conversion diverged")

    frames.extend((at + ts, text) for ts, text in converted)

    return at + grouped[-1][0] + _AFTER_OUTPUT


def _as_terminal_bytes(text: str) -> str:
    """A terminal needs CRLF; a captured stream carries bare LF."""

    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def _prepare_workspace(credentials: dict[str, str]) -> None:
    """Rebuild the demo project and its home from scratch, then seed the database."""

    if DEMO_ROOT.exists():
        shutil.rmtree(DEMO_ROOT)

    PROJECT_DIR.mkdir(parents=True)
    HOME_DIR.mkdir(parents=True)
    _apply_sql(credentials, SCHEMA_SQL)
    _apply_sql(credentials, SEED_SQL)


@contextmanager
def _patched_environment(credentials: dict[str, str] | None, clock: SteppedClock) -> Iterator[None]:
    """Redirect HOME, supply the credentials the run needs, and pin what varies between runs.

    Both clocks, the live footer and the colour/size environment each change what the CLI writes.
    """

    from dbprint.cli import run_log
    from dbprint.cli.commands import init as init_command
    from dbprint.cli.commands import list_cmd
    from dbprint.cli.rendering import progress
    from dbprint.config import connections as connections_config
    from dbprint.engine import freshness, orchestrator

    connections_dir = HOME_DIR / ".dbprint"
    connections_file = connections_dir / "connections.yaml"
    saved: dict[str, Any] = {
        "env": {key: os.environ.get(key) for key in _ENVIRONMENT_KEYS},
        "credentials_env": {
            key: value
            for key, value in os.environ.items()
            if key.startswith(f"DBPRINT_{CONNECTION.upper()}_")
        },
        "logs_root": run_log.LOGS_ROOT,
        "init_dir": init_command.CONNECTIONS_DIR,
        "init_file": init_command.CONNECTIONS_FILE,
        "config_file": connections_config.CONNECTIONS_FILE_DEFAULT,
        "live": progress.Live,
        "orchestrator_time": orchestrator.time,
        "progress_time": progress.time,
        "clocks": [(module, module.datetime) for module in (orchestrator, list_cmd, freshness)],
    }
    for key in _NEUTRALIZED_ENVIRONMENT_KEYS:
        os.environ.pop(key, None)

    for key in saved["credentials_env"]:
        os.environ.pop(key, None)

    os.environ.update(
        {
            "HOME": str(HOME_DIR),
            "COLUMNS": str(WIDTH),
            "LINES": str(HEIGHT),
            "FORCE_COLOR": "1",
            "TERM": "xterm-256color",
        },
    )
    init_command.CONNECTIONS_DIR = connections_dir
    init_command.CONNECTIONS_FILE = connections_file
    connections_config.CONNECTIONS_FILE_DEFAULT = connections_file
    run_log.LOGS_ROOT = HOME_DIR / ".dbprint" / "logs"
    progress.Live = EventDrivenLive
    orchestrator.time = cast(Any, clock)
    progress.time = cast(Any, clock)

    for module, _original in saved["clocks"]:
        module.datetime = cast(Any, FrozenDatetime)

    if credentials is not None:
        connections_dir.mkdir(parents=True, exist_ok=True)
        connections_file.write_text(
            yaml.safe_dump({CONNECTION: credentials}, sort_keys=False),
            encoding="utf-8",
        )

    try:
        yield
    finally:
        for key, value in {**saved["env"], **saved["credentials_env"]}.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        init_command.CONNECTIONS_DIR = saved["init_dir"]
        init_command.CONNECTIONS_FILE = saved["init_file"]
        connections_config.CONNECTIONS_FILE_DEFAULT = saved["config_file"]
        run_log.LOGS_ROOT = saved["logs_root"]
        progress.Live = saved["live"]
        orchestrator.time = saved["orchestrator_time"]
        progress.time = saved["progress_time"]

        for module, original in saved["clocks"]:
            module.datetime = original


def _create_database(credentials: dict[str, str]) -> None:
    """Create the demo database on the throwaway cluster."""

    import psycopg
    from psycopg import sql

    admin = _dsn({**credentials, "database": "postgres"})
    name = sql.Identifier(credentials["database"])

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(name))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(name))


def _dsn(credentials: dict[str, str]) -> str:
    """libpq connection string for one credentials dict."""

    return (
        f"host={credentials['host']} port={credentials['port']} "
        f"dbname={credentials['database']} user={credentials['user']} "
        f"password={credentials['password']}"
    )


def _apply_sql(credentials: dict[str, str], statements: str) -> None:
    """Run one SQL script against the throwaway database."""

    import psycopg

    with psycopg.connect(_dsn(credentials), autocommit=True) as conn:
        # SQL is a constant in this file; cast for psycopg's LiteralString overload.
        conn.execute(cast(LiteralString, statements))


def _load_module(name: str, directory: Path) -> Any:
    """Import a sibling script by path; `scripts/` carries no `__init__.py`."""

    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")

    if spec is None or spec.loader is None:
        raise SystemExit(f"could not load {name} from {directory}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def _main() -> int:
    """Provision a throwaway cluster, regenerate the committed cast, report."""

    # Loaded by path: `scripts/` is not a package, so a plain import would not resolve for `ty`.
    sibling = _load_module("gen_reference_example", Path(__file__).resolve().parent)

    bin_dir = sibling._discover_postgres_bin_dir()
    data_dir = Path("/var/lib/postgresql/dbprint-demo-" + secrets.token_hex(4))
    port = sibling._free_port()

    sibling._start_cluster(bin_dir, data_dir, port)

    try:
        credentials = {
            "host": "127.0.0.1",
            "port": str(port),
            "database": DATABASE,
            "user": "postgres",
            "password": "postgres",
        }
        _create_database(credentials)
        text = build_cast(credentials)
    finally:
        sibling._stop_cluster(bin_dir, data_dir)

    CAST_PATH.parent.mkdir(parents=True, exist_ok=True)
    CAST_PATH.write_text(text, encoding="utf-8")
    print(f"demo cast regenerated at {CAST_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
