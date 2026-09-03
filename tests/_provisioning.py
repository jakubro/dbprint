"""Shared test-environment provisioning for DB-backed fixtures.

Locates a server binary (initdb, mariadbd, ...); a container apt-installs what is missing and
retries, while a host miss raises rather than mutating the developer's system. apt runs with
`APT::Sandbox::User=root`, since `_apt` cannot write its temp files in some images.

Run directly (`python -m tests._provisioning`, wired into `just install`) to warm Delta Lake's
Maven/Ivy jar resolution once at install time - see `warm_delta_ivy_cache()`.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from glob import glob
from pathlib import Path


# One fixed path, so every xdist worker in the run agrees on the same lock file.
INSTALL_LOCK_PATH = Path("/tmp/dbprint--provisioning.lock")

# One fixed path, shared by `warm_delta_ivy_cache()` and every xdist worker's Databricks fixture -
# a per-process tempdir would make each worker re-resolve Delta's Maven jars from scratch.
SPARK_IVY_CACHE_PATH = Path("/tmp/dbprint--spark-ivy-cache")


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


def ensure_java() -> Path:
    """Locate a JVM, apt-installing a headless JRE on miss inside a container."""

    return discover_or_install(
        "java",
        ("default-jre-headless",),
        host_install_hint=(
            "'java' not found on PATH, and pyspark ships no JVM of its own. "
            "Install: default-jre-headless."
        ),
    )


def warm_delta_ivy_cache() -> int:
    """Resolve and cache Delta Lake's Maven/Ivy jars once, returning the process exit code.

    `delta-spark` ships no jars, so warming here turns a Maven outage into a clear install-time
    error. Exit 0 where no JVM can be found or installed - the Databricks tests skip on that too.
    """

    try:
        ensure_java()
    except RuntimeError as exc:
        print(f"warm_delta_ivy_cache: {exc} Skipping - Databricks tests will skip too.")

        return 0

    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    SPARK_IVY_CACHE_PATH.mkdir(parents=True, exist_ok=True)
    builder = (
        SparkSession.builder.master("local[1]")
        .appName("dbprint-ivy-warm")
        .config("spark.jars.ivy", str(SPARK_IVY_CACHE_PATH))
        .config("spark.ui.enabled", "false")
    )

    try:
        spark = configure_spark_with_delta_pip(builder).getOrCreate()
    except Exception as exc:  # noqa: BLE001 - any resolution failure is the one thing to report
        print(f"warm_delta_ivy_cache: could not resolve Delta's Maven jars: {exc}", file=sys.stderr)

        return 1

    spark.stop()
    print("warm_delta_ivy_cache: Delta's Maven jars resolved and cached")

    return 0


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

    with INSTALL_LOCK_PATH.open("r+", encoding="utf-8") as lock_file:
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


if __name__ == "__main__":
    raise SystemExit(warm_delta_ivy_cache())
