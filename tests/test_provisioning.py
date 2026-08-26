"""`_provisioning.py` - the install path every DB-backed fixture shares."""

from __future__ import annotations

import threading
import time
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
