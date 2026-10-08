"""The registry of live substrates the suite starts, and the sweep that reclaims a dead run's.

A marker names the starting process; a later run reclaims it once that process is provably dead.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REGISTRY_NAME = "dbprint--substrates"

OWNER_LABEL = "dbprint.owner"

_MARKER_SUFFIX = ".json"

_REAPING_INFIX = ".reaping-"

_UNMARKED_PREFIXES = (
    "dbprint-test-postgres-",
    "dbprint-test-mariadb-",
    "dbprint-spark-warehouse-",
)

_MEM_AVAILABLE_RE = re.compile(r"^MemAvailable:\s+(\d+) kB$", re.MULTILINE)

_TCP_LISTEN = "0A"


@dataclass(frozen=True)
class Owner:
    """The process a substrate belongs to, identified so a recycled pid cannot pass for it."""

    pid: int
    start: int
    pid_ns: str
    boot_id: str

    def label(self) -> str:
        """Render the value the owner's containers carry, so a reaper can match it."""

        return f"{self.boot_id}:{self.pid_ns}:{self.pid}:{self.start}"


def current_owner() -> Owner:
    """Describe the running process."""

    pid = os.getpid()
    start = process_start(pid)
    assert start is not None

    return Owner(
        pid=pid,
        start=start,
        pid_ns=os.readlink("/proc/self/ns/pid"),
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
    )


def process_start(pid: int) -> int | None:
    """Return a process's start time in clock ticks since boot, or None where it is gone."""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None

    # The command name is parenthesised and may itself hold spaces or parentheses.
    fields = stat[stat.rindex(")") + 2 :].split()

    return int(fields[19])


def owner_is_dead(owner: Owner, here: Owner, start_of: Callable[[int], int | None]) -> bool | None:
    """Return whether `owner` has exited, or None where it cannot be judged from `here`.

    Another PID namespace is never judged; within one, a pid whose start time changed is dead.
    """

    if owner.boot_id != here.boot_id:
        return True

    if owner.pid_ns != here.pid_ns:
        return None

    return start_of(owner.pid) != owner.start


def registry(root: Path) -> Path:
    """Return the registry directory under a scratch root."""

    return root / REGISTRY_NAME


@contextmanager
def registered(root: Path, name: str, kind: str, **handle: str) -> Generator[Path]:
    """Hold a marker for a substrate while the body, its own teardown included, runs.

    The marker precedes the resource and outlives it, so a run killed in between leaves one.
    """

    directory = registry(root)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / (name + _MARKER_SUFFIX)
    _write_json(marker, {"kind": kind, "handle": handle, "owner": asdict(current_owner())})

    try:
        yield marker
    finally:
        marker.unlink(missing_ok=True)


def register(root: Path, name: str, kind: str, owner: Owner, **handle: str) -> Path:
    """Write a marker naming `owner` rather than this process, left for `stop_owned` to remove."""

    directory = registry(root)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / (name + _MARKER_SUFFIX)
    _write_json(marker, {"kind": kind, "handle": handle, "owner": asdict(owner)})

    return marker


def shared(
    root: Path,
    kind: str,
    owner: Owner,
    start: Callable[[], dict[str, str]],
) -> dict[str, str]:
    """Return the handle of `owner`'s `kind` substrate, calling `start` only if no process has.

    Every process of a run passes one owner; `start` must `register` under it before the resource exists.
    """

    directory = registry(root)
    directory.mkdir(parents=True, exist_ok=True)

    # One lock per kind, kept across runs: nothing to clean up after a run that was killed.
    with (directory / f"{kind}.lock").open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)

        for marker in sorted(directory.glob("*" + _MARKER_SUFFIX)):
            entry = _read_json(marker)

            if entry is not None and entry["kind"] == kind and Owner(**entry["owner"]) == owner:
                return entry["handle"]

        return start()


def stop_owned(
    root: Path,
    stoppers: Mapping[str, Callable[[Mapping[str, str], Owner], None]],
    owner: Owner,
) -> None:
    """Stop every substrate registered under `owner`, then drop its markers."""

    directory = registry(root)

    if not directory.is_dir():
        return

    for marker in sorted(directory.glob("*" + _MARKER_SUFFIX)):
        entry = _read_json(marker)

        if entry is not None and Owner(**entry["owner"]) == owner:
            stoppers[entry["kind"]](entry["handle"], owner)
            marker.unlink(missing_ok=True)


def sweep(
    root: Path,
    stoppers: Mapping[str, Callable[[Mapping[str, str], Owner], None]],
) -> list[str]:
    """Reclaim every registered substrate whose owner is dead; return one line per entry seen.

    Claiming renames the marker, so one of two concurrent sweeps stops it; unmarked dirs are reported.
    """

    here = current_owner()
    directory = registry(root)
    lines: list[str] = []
    marked: set[str] = set()

    if directory.is_dir():
        for marker in sorted(directory.iterdir()):
            entry = _read_json(marker)

            if entry is None:
                continue

            marked.add(entry["handle"].get("path", ""))
            line = _reclaim(marker, entry, here, stoppers)

            if line is not None:
                lines.append(line)

    for child in sorted(root.iterdir()) if root.is_dir() else ():
        if child.name.startswith(_UNMARKED_PREFIXES) and str(child) not in marked:
            lines.append(f"left alone, no marker: {child}")

    return lines


def live_entries(root: Path) -> list[str]:
    """Describe every registered substrate and whether its owner still runs."""

    here = current_owner()
    directory = registry(root)
    out = []

    for marker in sorted(directory.iterdir()) if directory.is_dir() else ():
        entry = _read_json(marker)

        if entry is None:
            continue

        owner = Owner(**entry["owner"])
        dead = owner_is_dead(owner, here, process_start)
        state = {True: "dead", False: "alive", None: "other namespace"}[dead]
        handle = ", ".join(f"{key}={value}" for key, value in sorted(entry["handle"].items()))
        out.append(f"{entry['kind']} {handle} owner {owner.pid} ({state})")

    return out


def start_failure(kind: str, server_said: str, root: Path, port: int | None = None) -> str:
    """Compose the message a substrate that could not start fails with."""

    entries = live_entries(root)
    lines = [f"{kind} could not start" + (f" on 127.0.0.1:{port}" if port is not None else "")]
    lines.append("  server said: " + (server_said.strip() or "(nothing)").replace("\n", "\n    "))

    if port is not None:
        held = port_holder(port)
        lines.append(f"  port {port} " + (f"held by {held}" if held else "held by no process"))

    lines.extend(
        (
            f"  live suite substrates: {len(entries)}" + "".join(f"\n    {e}" for e in entries),
            f"  MemAvailable: {available_mb()} MiB",
        ),
    )

    return "\n".join(lines)


def tail(path: Path, lines: int = 20) -> str:
    """Return the last lines of a server log, or a note that there is none."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"({path.name} unreadable: {exc.strerror})"

    return "\n".join(text.splitlines()[-lines:])


def port_holder(port: int) -> str | None:
    """Name the process listening on a local TCP port, read from /proc alone."""

    inodes = set()

    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue

        for row in rows:
            fields = row.split()

            if fields[3] == _TCP_LISTEN and int(fields[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(fields[9])

    if not inodes:
        return None

    unreadable = 0

    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue

        try:
            links = {os.readlink(fd) for fd in (proc / "fd").iterdir()}
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
        except OSError:
            unreadable += 1
            continue

        if any(f"socket:[{inode}]" in links for inode in inodes):
            return f"pid {proc.name}: {cmdline}"

    return f"an unreadable process ({unreadable} /proc entries could not be read)"


def available_mb() -> int | None:
    """Return MemAvailable in MiB, or None where /proc/meminfo is unreadable."""

    try:
        match = _MEM_AVAILABLE_RE.search(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except OSError:
        return None

    return int(match.group(1)) // 1024 if match is not None else None


def stop_postgres(handle: Mapping[str, str], run_as_postgres: Callable[..., Any]) -> None:
    """Stop a stranded cluster the way its fixture would, then remove its directory."""

    data_dir = handle["path"]
    pg_ctl = str(Path(handle["bin_dir"]) / "pg_ctl")
    run_as_postgres([pg_ctl, "-D", data_dir, "-m", "immediate", "stop"], check=False)
    pid = _pid_from(Path(data_dir) / "postmaster.pid")

    # pg_ctl can fail against a half-started cluster, leaving the postmaster to be killed itself.
    if pid is not None and f"-D {data_dir} " in _cmdline_of(pid) + " ":
        _kill(pid, signal.SIGKILL)

    run_as_postgres(["rm", "-rf", data_dir], check=False)


def stop_mariadb(handle: Mapping[str, str], _owner: Owner) -> None:
    """Stop a stranded MariaDB server the way its fixture would, then remove its directory."""

    data_dir = handle["path"]
    pid = _pid_from(Path(data_dir) / "mariadb.pid")

    if pid is not None and f"--datadir={data_dir}" in _cmdline_of(pid):
        _kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10

        while process_start(pid) is not None and time.monotonic() < deadline:
            time.sleep(0.1)

        if process_start(pid) is not None:
            _kill(pid, signal.SIGKILL)

    shutil.rmtree(data_dir, ignore_errors=True)


def stop_bigquery(handle: Mapping[str, str], owner: Owner) -> None:
    """Stop a stranded emulator container, only when its label names the same owner."""

    container = handle["container"]
    inspected = subprocess.run(
        [
            "podman",
            "inspect",
            "--format",
            '{{ index .Config.Labels "' + OWNER_LABEL + '" }}',
            container,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if inspected.returncode == 0 and inspected.stdout.strip() == owner.label():
        subprocess.run(["podman", "stop", "-t", "0", container], capture_output=True, check=False)


def stop_directory(handle: Mapping[str, str], _owner: Owner) -> None:
    """Remove a stranded scratch directory; nothing runs from it."""

    shutil.rmtree(handle["path"], ignore_errors=True)


def _reclaim(
    marker: Path,
    entry: dict[str, Any],
    here: Owner,
    stoppers: Mapping[str, Callable[[Mapping[str, str], Owner], None]],
) -> str | None:
    owner = Owner(**entry["owner"])
    description = (
        f"{entry['kind']} {entry['handle'].get('path') or entry['handle'].get('container')}"
    )
    dead = owner_is_dead(owner, here, process_start)

    if dead is None:
        return f"left alone, owner {owner.pid} in another PID namespace: {description}"

    if not dead:
        return None

    if _REAPING_INFIX in marker.name:
        reaper = Owner(**entry["reaper"]) if "reaper" in entry else None

        if reaper is not None and owner_is_dead(reaper, here, process_start) is not True:
            return None

    claimed = marker.with_name(marker.name.split(_REAPING_INFIX)[0] + f"{_REAPING_INFIX}{here.pid}")

    try:
        marker.rename(claimed)
    except FileNotFoundError:
        return None

    _write_json(claimed, {**entry, "reaper": asdict(here)})
    stoppers[entry["kind"]](entry["handle"], owner)
    claimed.unlink(missing_ok=True)

    return f"reclaimed from dead owner {owner.pid}: {description}"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(payload), encoding="utf-8")
    staging.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if path.name.endswith(".tmp"):
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_from(pid_file: Path) -> int | None:
    try:
        first = pid_file.read_text(encoding="utf-8").split("\n", 1)[0].strip()
    except OSError:
        return None

    return int(first) if first.isdigit() else None


def _cmdline_of(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def _kill(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
