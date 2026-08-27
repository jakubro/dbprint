"""Git-address `--project` locators: offline parsing, then clone-or-refresh into a local cache."""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .project import ConfigError


CACHE_ROOT = Path("~/.dbprint/cache")
CACHE_TTL_SECONDS = 15 * 60

_STAMP_FILENAME = ".dbprint-cache-stamp"
_GIT_BIN = "git"
_GIT_TIMEOUT_SECONDS = 120

# Forge web forms, decomposed to (owner_repo, ref, subpath). GitLab's own owner/repo segment
# may itself contain slashes (nested groups), hence the non-greedy `.+?`.
_GITHUB_BLOB_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+?)(?:\.git)?/blob/([^/]+)/(.*)$")
_GITLAB_BLOB_RE = re.compile(r"^https://gitlab\.com/(.+?)/-/blob/([^/]+)/(.*)$")
_BITBUCKET_SRC_RE = re.compile(r"^https://bitbucket\.org/([^/]+/[^/]+?)/src/([^/]+)/(.*)$")

# A bare remote - default branch, no subpath. GitLab's alternative accepts any group depth.
_BARE_HTTPS_RE = re.compile(
    r"^https://(?:"
    r"(?:github\.com|bitbucket\.org)/[^/]+/[^/]+?"
    r"|gitlab\.com/.+?"
    r")(?:\.git)?/?$",
)
_BARE_SSH_RE = re.compile(r"^git@[^:/]+:.+$")


@dataclass(frozen=True)
class RemoteAddress:
    """The repository, ref and subpath a git `--project` locator resolves to.

    `ref` of `None` means the repository's default branch; `subpath` of `None` means its root.
    """

    remote: str
    ref: str | None = None
    subpath: str | None = None


def parse_address(value: str) -> RemoteAddress | None:
    """Parse `--project`'s value as a git address; `None` means it is a local path instead.

    Offline: the explicit `<git-url>#<ref>:<subpath>` grammar, then the three forge web URL
    grammars, then a bare HTTPS or SSH remote.
    """

    explicit = _parse_explicit(value)

    if explicit is not None:
        return explicit

    for pattern in (_GITHUB_BLOB_RE, _GITLAB_BLOB_RE, _BITBUCKET_SRC_RE):
        match = pattern.match(value)

        if match:
            owner_repo, ref, subpath = match.groups()
            host = value.split("/", 3)[2]

            return RemoteAddress(
                remote=f"https://{host}/{owner_repo}",
                ref=ref,
                subpath=subpath.rstrip("/") or None,
            )

    if _BARE_HTTPS_RE.match(value) or _BARE_SSH_RE.match(value):
        return RemoteAddress(remote=value)

    return None


def _parse_explicit(value: str) -> RemoteAddress | None:
    if "#" not in value:
        return None

    git_url, _, rest = value.partition("#")

    if ":" not in rest:
        return None

    ref, _, subpath = rest.partition(":")

    if not git_url or not ref:
        return None

    return RemoteAddress(remote=git_url, ref=ref, subpath=subpath.rstrip("/") or None)


def materialize(address: RemoteAddress) -> Path:
    """Clone or refresh `address` into the local cache; return the path to resolve against.

    The clone IS the prefetch: one fetch at first use, then at most one per `CACHE_TTL_SECONDS`.
    """

    _ensure_git_available()
    cache_dir = _cache_dir_for(address)
    stamp = cache_dir / _STAMP_FILENAME

    if cache_dir.is_dir() and stamp.is_file():
        age_seconds = time.time() - stamp.stat().st_mtime

        if age_seconds >= CACHE_TTL_SECONDS:
            _refresh(cache_dir)
            stamp.touch()
    else:
        if cache_dir.exists():
            shutil.rmtree(cache_dir)

        _clone(cache_dir, address)
        stamp.touch()

    if address.subpath is None:
        return cache_dir

    return cache_dir / address.subpath


def watch_for_refresh(address: RemoteAddress) -> None:
    """Start a daemon thread that re-materializes `address` once per TTL, for the process's life.

    A long-lived server re-reads the same path per request, so refreshing it in place is what
    keeps it current; a failed fetch is suppressed so the previous clone stays servable.
    """

    def _loop() -> None:
        while True:
            time.sleep(CACHE_TTL_SECONDS)

            with contextlib.suppress(ConfigError):
                materialize(address)

    threading.Thread(target=_loop, daemon=True).start()


def _ensure_git_available() -> None:
    """Raise `ConfigError` when `git` is missing - the shape `pg_dump`'s own guard uses."""

    if shutil.which(_GIT_BIN) is None:
        raise ConfigError(
            "git binary not found on PATH. A remote --project locator needs it "
            "(Debian/Ubuntu: `apt install git`; macOS: `brew install git`).",
        )


def _cache_dir_for(address: RemoteAddress) -> Path:
    """One cache entry per (remote, ref) - distinct subpaths of the same clone share it.

    `~` expands at call time, not at `CACHE_ROOT`'s definition, so a `HOME` override is honored.
    """

    key_source = f"{address.remote}#{address.ref or 'HEAD'}"
    key = hashlib.sha256(key_source.encode()).hexdigest()[:20]

    return CACHE_ROOT.expanduser() / key


def _clone(cache_dir: Path, address: RemoteAddress) -> None:
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    args = [_GIT_BIN, "clone", "--quiet"]

    if address.ref is not None:
        args += ["--branch", address.ref]

    args += [address.remote, str(cache_dir)]
    _run_git(args)


def _refresh(cache_dir: Path) -> None:
    _run_git([_GIT_BIN, "-C", str(cache_dir), "pull", "--quiet", "--ff-only"])


def _run_git(args: list[str]) -> None:
    try:
        subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)

        raise ConfigError(f"git failed: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConfigError(f"git timed out after {_GIT_TIMEOUT_SECONDS}s: {' '.join(args)}") from exc
