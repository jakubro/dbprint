"""Git-address `--project` locators: offline parsing, then clone-or-refresh into a local cache."""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .project import ConfigError


CACHE_ROOT = Path("~/.dbprint/cache")
CACHE_TTL_SECONDS = 15 * 60

_STAMP_FILENAME = ".dbprint-cache-stamp"
_FRESH_FILENAME = ".dbprint-cache-fresh"
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


def materialize(
    address: RemoteAddress,
    *,
    on_degraded: Callable[[str], None] | None = None,
) -> Path:
    """Clone or refresh `address` into the local cache; return the path to resolve against.

    The clone IS the prefetch: one fetch at first use, then at most one per `CACHE_TTL_SECONDS`.
    `on_degraded` reports a refresh that failed over a readable cache; without it, silence.
    """

    _ensure_git_available()
    cache_dir = _cache_dir_for(address)
    stamp = cache_dir / _STAMP_FILENAME

    if cache_dir.is_dir() and stamp.is_file():
        age_seconds = time.time() - stamp.stat().st_mtime

        if age_seconds >= CACHE_TTL_SECONDS and not _is_pinned(cache_dir, address):
            _refresh_or_degrade(cache_dir, address, on_degraded)
            # Touched on either outcome: it bounds refresh ATTEMPTS to one per TTL, and a
            # failure that left it untouched would retry - and pay git's timeout - every run.
            stamp.touch()
    else:
        _replace_clone(cache_dir, address)
        stamp.touch()
        (cache_dir / _FRESH_FILENAME).touch()

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
    """Clone, then check the ref out - one path for a branch, a tag, a short or a full SHA.

    `clone --branch` takes a ref NAME, and a commit id - what a permalink carries - is not one.
    """

    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git([_GIT_BIN, "clone", "--quiet", address.remote, str(cache_dir)])

    if address.ref is None:
        return

    try:
        # `--` or git reads a ref that names a path as a pathspec: it would restore those
        # files, exit 0, and leave the cache on the default branch with nothing said.
        _run_git([*_git_in(cache_dir), "checkout", "--quiet", address.ref, "--"])
    except ConfigError as exc:
        # A half-populated cache would be served on every later run, so it goes with the failure.
        shutil.rmtree(cache_dir, ignore_errors=True)

        raise ConfigError(
            f"ref {address.ref!r} could not be resolved in {address.remote}: {exc}",
        ) from exc


def _replace_clone(cache_dir: Path, address: RemoteAddress) -> None:
    """Discard whatever sits at `cache_dir` and clone afresh; a failed clone is a hard error."""

    if cache_dir.exists():
        shutil.rmtree(cache_dir)

    _clone(cache_dir, address)


def _refresh_or_degrade(
    cache_dir: Path,
    address: RemoteAddress,
    on_degraded: Callable[[str], None] | None,
) -> None:
    """Refresh in place, falling back to the cached clone when the remote cannot be reached.

    A cache that can no longer answer a read is re-cloned instead, and a failure there raises.
    """

    fresh = cache_dir / _FRESH_FILENAME

    try:
        _refresh(cache_dir, address)
    except ConfigError as exc:
        if not _can_be_read(cache_dir):
            _replace_clone(cache_dir, address)
            fresh.touch()

            return

        if on_degraded is not None:
            on_degraded(
                f"could not refresh the cached clone of {address.remote}: {exc}; "
                f"reading the copy fetched {_content_age(fresh)}",
            )

        return

    fresh.touch()


def _content_age(fresh: Path) -> str:
    """How old the cached content is, from the last fetch that actually landed.

    The freshness stamp beside it advances on a failed attempt too, so it cannot answer this.
    """

    if not fresh.is_file():
        return "at an unknown time"

    minutes = int((time.time() - fresh.stat().st_mtime) // 60)

    return f"{minutes} minutes ago"


def _refresh(cache_dir: Path, address: RemoteAddress) -> None:
    """Fetch the address's ref and reset onto it, whatever it did upstream.

    The reset is safe only because nothing writes into the cache: every write command refuses
    a remote locator before a clone happens at all (`cli.options.refuse_if_remote`).
    """

    _run_git([*_git_in(cache_dir), "fetch", "--quiet", "origin", address.ref or "HEAD"])
    _run_git([*_git_in(cache_dir), "reset", "--hard", "--quiet", "FETCH_HEAD"])


def _git_in(cache_dir: Path) -> list[str]:
    """Pin git to the cache's own repository.

    `-C` would let git discover an enclosing checkout and reset THAT repository instead.
    """

    return [_GIT_BIN, "--git-dir", str(cache_dir / ".git"), "--work-tree", str(cache_dir)]


def _is_pinned(cache_dir: Path, address: RemoteAddress) -> bool:
    """Whether the address names a commit rather than a ref that can move.

    git decides, not a regex: `rev-parse --symbolic-full-name` names a branch or a tag and
    says nothing for a commit id, so a branch named like a hash is no pin.
    """

    if address.ref is None:
        return False

    try:
        named = _run_git([*_git_in(cache_dir), "rev-parse", "--symbolic-full-name", address.ref])
    except ConfigError:
        return False

    return not named


def _can_be_read(cache_dir: Path) -> bool:
    """Whether the cached clone still resolves a commit - one local call, no network."""

    try:
        _run_git([*_git_in(cache_dir), "rev-parse", "--verify", "HEAD"])
    except ConfigError:
        return False

    return True


def _run_git(args: list[str]) -> str:
    """Run one git command and return its stdout; any failure becomes a `ConfigError`."""

    try:
        return subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)

        raise ConfigError(f"git failed: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConfigError(f"git timed out after {_GIT_TIMEOUT_SECONDS}s: {' '.join(args)}") from exc
