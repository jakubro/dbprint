"""`_provisioning.py` - the install path every DB-backed fixture shares."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests import _provisioning


BINARY = "dbprint-fake-server"
PACKAGES = ("dbprint-fake-server",)
WORKERS = 4


def test_concurrent_callers_install_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """apt runs for the first caller; the rest find what it installed."""

    target = tmp_path / BINARY
    calls: list[tuple[str, ...]] = []
    counter = threading.Lock()

    def fake_apt(apt_packages: tuple[str, ...]) -> None:
        with counter:
            calls.append(apt_packages)

        time.sleep(0.1)
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)

    monkeypatch.setattr(_provisioning, "in_container", lambda: True)
    monkeypatch.setattr(_provisioning, "_apt_install", fake_apt)

    barrier = threading.Barrier(WORKERS)
    found: list[Path] = []

    def worker() -> None:
        barrier.wait()
        found.append(_provisioning.discover_or_install(BINARY, PACKAGES, (str(target),)))

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert calls == [PACKAGES]
    assert found == [target] * WORKERS


def test_an_install_that_produces_nothing_still_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An apt that exits clean without providing the binary is still a failure."""

    monkeypatch.setattr(_provisioning, "in_container", lambda: True)
    monkeypatch.setattr(_provisioning, "_apt_install", lambda apt_packages: None)

    with pytest.raises(RuntimeError, match="still not found"):
        _provisioning.discover_or_install(BINARY, PACKAGES, (str(tmp_path / BINARY),))


@pytest.fixture
def ivy_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    cache = tmp_path / "ivy"
    cache.mkdir()
    monkeypatch.setattr(_provisioning, "_DELTA_RESOLVED_MARKER", cache / "dbprint-resolved")
    monkeypatch.setattr(_provisioning, "_DELTA_RESOLUTION_LOCK_PATH", tmp_path / "ivy.lock")

    return cache


def test_concurrent_starts_on_a_cold_cache_resolve_once(ivy_cache: Path) -> None:
    """One start resolves alone; the others wait for it, then start side by side."""

    unresolved: list[int] = []
    inside = 0
    counter = threading.Lock()
    waiters = threading.Barrier(WORKERS - 1, timeout=2)
    met: list[bool] = []

    def start() -> None:
        nonlocal inside

        with _provisioning.delta_resolution():
            with counter:
                inside += 1
                resolved = _provisioning.delta_jars_resolved()

                if not resolved:
                    unresolved.append(inside)

            if resolved:
                met.append(_meets(waiters))
            else:
                time.sleep(0.1)

            with counter:
                inside -= 1

    _in_parallel(start)

    assert unresolved == [1]
    assert met == [True] * (WORKERS - 1)
    assert _provisioning.delta_jars_resolved()


def test_starts_on_a_resolved_cache_run_side_by_side(ivy_cache: Path) -> None:
    with _provisioning.delta_resolution():
        pass

    together = threading.Barrier(WORKERS, timeout=2)
    met: list[bool] = []

    def start() -> None:
        with _provisioning.delta_resolution():
            met.append(_meets(together))

    _in_parallel(start)

    assert met == [True] * WORKERS


def test_a_failed_resolution_leaves_the_cache_unresolved(ivy_cache: Path) -> None:
    with pytest.raises(RuntimeError, match="unreachable"), _provisioning.delta_resolution():
        raise RuntimeError("Maven Central unreachable")

    assert not _provisioning.delta_jars_resolved()

    with _provisioning.delta_resolution():
        pass

    assert _provisioning.delta_jars_resolved()


def _in_parallel(start: Callable[[], None]) -> None:
    barrier = threading.Barrier(WORKERS)

    def worker() -> None:
        barrier.wait()
        start()

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()


def _meets(barrier: threading.Barrier) -> bool:
    try:
        barrier.wait()
    except threading.BrokenBarrierError:
        return False

    return True
