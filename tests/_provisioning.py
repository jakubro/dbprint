"""Shared test-environment provisioning for DB-backed fixtures.

Locates a server binary (initdb, mariadbd, ...); a container apt-installs what is missing and
retries, while a host miss raises rather than mutating the developer's system. apt runs with
`APT::Sandbox::User=root`, since `_apt` cannot write its temp files in some images.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from glob import glob
from pathlib import Path


# One fixed path, so every xdist worker in the run agrees on the same lock file.
INSTALL_LOCK_PATH = Path("/tmp/dbprint--provisioning.lock")


def in_container() -> bool:
    """True when running inside an OCI / Docker container."""

    return Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()


def discover_or_install(
    binary: str,
    apt_packages: tuple[str, ...],
    candidate_globs: tuple[str, ...] = (),
    host_install_hint: str = "",
) -> Path:
    """Locate `binary`; apt-install on miss inside a container; raise on host miss.

    Resolution order is PATH, then `candidate_globs`, then (in a container) apt-install
    and re-resolve. Raises RuntimeError carrying `host_install_hint` on a host miss.
    """

    found = _locate(binary, candidate_globs)

    if found is not None:
        return found

    if in_container():
        with _install_lock():
            found = _locate(binary, candidate_globs)

            if found is None:
                _apt_install(apt_packages)
                found = _locate(binary, candidate_globs)

        if found is not None:
            return found

        raise RuntimeError(
            f"{binary!r} still not found after installing {', '.join(apt_packages)}.",
        )

    raise RuntimeError(
        host_install_hint or f"{binary!r} not found on PATH. Install: {', '.join(apt_packages)}.",
    )


def _locate(binary: str, candidate_globs: tuple[str, ...]) -> Path | None:
    """Return the binary path from PATH or the candidate globs, else None."""

    on_path = shutil.which(binary)

    if on_path:
        return Path(on_path)

    for pattern in candidate_globs:
        for candidate in sorted(glob(pattern)):
            path = Path(candidate)

            if path.is_file() and os.access(path, os.X_OK):
                return path

    return None


@contextmanager
def _install_lock() -> Iterator[None]:
    """Hold an exclusive file lock for the duration of an install.

    pytest-xdist fans fixtures out across processes, and apt fails outright on a held
    `/var/lib/apt/lists/lock` rather than waiting, so every worker but one would error at
    setup. The caller re-locates inside the lock and skips apt if a peer already installed.
    """

    INSTALL_LOCK_PATH.touch(exist_ok=True)

    with INSTALL_LOCK_PATH.open("r+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)

        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _apt_install(apt_packages: tuple[str, ...]) -> None:
    """apt-get update + install the given packages inside the container."""

    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    base = ["apt-get", "-y", "-o", "APT::Sandbox::User=root"]

    for argv in (base + ["update"], base + ["install", *apt_packages]):
        result = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)

        if result.returncode != 0:
            raise RuntimeError(
                f"`{' '.join(argv)}` failed (exit {result.returncode}): "
                f"{result.stderr.strip()[:500]}",
            )
