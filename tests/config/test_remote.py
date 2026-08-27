"""Git-address `--project` locators: offline parsing, then clone/refresh against a local repo.

`materialize` runs against a real local git repository - never a network host.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from dbprint.config import remote as remote_module
from dbprint.config.project import ConfigError
from dbprint.config.remote import RemoteAddress, materialize, parse_address, watch_for_refresh


# Passed to every git subprocess call; only `commit` actually reads author/committer.
_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "botanist",
    "GIT_AUTHOR_EMAIL": "botanist@seedbank.example",
    "GIT_COMMITTER_NAME": "botanist",
    "GIT_COMMITTER_EMAIL": "botanist@seedbank.example",
}


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_IDENTITY_ENV},
    )


def _bare_repo(tmp_path: Path, *, config_at: str = "") -> tuple[Path, Path]:
    """A bare repo whose `main` branch holds `.dbprint.yaml` (+ a print) at `config_at`.

    Returns `(bare_path, work_path)` - `work_path` stays so a test can commit and push updates.
    """

    work = tmp_path / "work"
    work.mkdir()
    _run_git(["init", "--initial-branch=main"], cwd=work)

    target = (work / config_at) if config_at else work
    target.mkdir(parents=True, exist_ok=True)
    (target / ".dbprint.yaml").write_text(
        "connections:\n  primary:\n    adapter: postgres\n    output: prints\n",
    )
    (target / "prints" / "primary").mkdir(parents=True)
    (target / "prints" / "primary" / "manifest.yaml").write_text(
        "format_version: 1\ntables: {}\n",
    )
    _run_git(["add", "."], cwd=work)
    _run_git(["commit", "-m", "initial"], cwd=work)

    bare = tmp_path / "bare.git"
    _run_git(["clone", "--bare", "--quiet", str(work), str(bare)], cwd=tmp_path)

    return bare, work


def _push_update(
    work: Path,
    bare: Path,
    relpath: str,
    content: str,
    *,
    branch: str = "main",
) -> None:
    path = work / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _run_git(["add", relpath], cwd=work)
    _run_git(["commit", "-m", "update"], cwd=work)
    _run_git(["push", "--quiet", str(bare), f"{branch}:{branch}"], cwd=work)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own `~/.dbprint/cache`, never the real one."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))


class TestParseAddress:
    """Pure string grammar - no network call, no forge API."""

    def test_bare_https_remote(self) -> None:
        assert parse_address("https://github.com/acme/demo") == RemoteAddress(
            remote="https://github.com/acme/demo",
        )

    def test_bare_ssh_remote(self) -> None:
        assert parse_address("git@github.com:acme/demo.git") == RemoteAddress(
            remote="git@github.com:acme/demo.git",
        )

    def test_bare_https_remote_with_a_gitlab_subgroup(self) -> None:
        """GitLab alone nests groups arbitrarily deep - the bare form must accept that too."""

        assert parse_address("https://gitlab.com/group/subgroup/repo") == RemoteAddress(
            remote="https://gitlab.com/group/subgroup/repo",
        )

    def test_github_blob_directory_form(self) -> None:
        addr = parse_address("https://github.com/acme/demo/blob/main/project1/module7/")
        assert addr == RemoteAddress(
            remote="https://github.com/acme/demo",
            ref="main",
            subpath="project1/module7",
        )

    def test_github_blob_file_form_names_the_config_directly(self) -> None:
        addr = parse_address(
            "https://github.com/acme/demo/blob/main/project1/module7/.dbprint.yaml",
        )
        assert addr == RemoteAddress(
            remote="https://github.com/acme/demo",
            ref="main",
            subpath="project1/module7/.dbprint.yaml",
        )

    def test_gitlab_blob_form_with_a_nested_group(self) -> None:
        addr = parse_address(
            "https://gitlab.com/group/subgroup/repo/-/blob/main/project1/module7/",
        )
        assert addr == RemoteAddress(
            remote="https://gitlab.com/group/subgroup/repo",
            ref="main",
            subpath="project1/module7",
        )

    def test_bitbucket_src_form(self) -> None:
        addr = parse_address("https://bitbucket.org/acme/demo/src/main/project1/module7/")
        assert addr == RemoteAddress(
            remote="https://bitbucket.org/acme/demo",
            ref="main",
            subpath="project1/module7",
        )

    def test_explicit_grammar(self) -> None:
        addr = parse_address("git@github.com:acme/demo.git#release-2:project1/module7")
        assert addr == RemoteAddress(
            remote="git@github.com:acme/demo.git",
            ref="release-2",
            subpath="project1/module7",
        )

    def test_explicit_grammar_with_no_subpath_means_the_repository_root(self) -> None:
        addr = parse_address("git@github.com:acme/demo.git#release-2:")
        assert addr == RemoteAddress(remote="git@github.com:acme/demo.git", ref="release-2")

    def test_local_absolute_path_is_not_an_address(self) -> None:
        assert parse_address("/srv/analytics") is None

    def test_local_relative_path_is_not_an_address(self) -> None:
        assert parse_address("./project") is None

    def test_a_hash_with_nothing_ref_shaped_after_it_is_not_an_address(self) -> None:
        assert parse_address("./weird#nocolon") is None


class TestMaterialize:
    def test_clones_a_bare_remote_to_the_repository_root(self, tmp_path: Path) -> None:
        bare, _work = _bare_repo(tmp_path)

        local = materialize(RemoteAddress(remote=str(bare)))

        assert (local / ".dbprint.yaml").is_file()

    def test_resolves_a_subpath_under_the_clone(self, tmp_path: Path) -> None:
        bare, _work = _bare_repo(tmp_path, config_at="project1/module7")

        local = materialize(RemoteAddress(remote=str(bare), subpath="project1/module7"))

        assert (local / ".dbprint.yaml").is_file()
        assert local.name == "module7"

    def test_ref_checks_out_the_named_branch(self, tmp_path: Path) -> None:
        bare, work = _bare_repo(tmp_path)
        _run_git(["checkout", "-b", "release-2"], cwd=work)
        _push_update(
            work,
            bare,
            "prints/primary/manifest.yaml",
            "format_version: 1\ntables: {marker: {}}\n",
            branch="release-2",
        )

        local = materialize(RemoteAddress(remote=str(bare), ref="release-2"))

        assert "marker" in (local / "prints" / "primary" / "manifest.yaml").read_text()

    def test_same_address_reuses_the_same_cache_directory(self, tmp_path: Path) -> None:
        bare, _work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare))

        assert materialize(address) == materialize(address)

    def test_distinct_refs_of_the_same_remote_do_not_collide(self, tmp_path: Path) -> None:
        bare, _work = _bare_repo(tmp_path)

        default = materialize(RemoteAddress(remote=str(bare)))
        named = materialize(RemoteAddress(remote=str(bare), ref="main"))

        assert default != named

    def test_reuses_within_the_ttl_without_a_git_call(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bare, _work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare))
        materialize(address)

        def _fail(_args: list[str]) -> None:
            raise AssertionError("git ran again inside the cache TTL")

        monkeypatch.setattr(remote_module, "_run_git", _fail)

        materialize(address)  # must not raise

    def test_refreshes_once_the_ttl_has_elapsed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bare, work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare))
        first = materialize(address)
        assert "marker" not in (first / "prints" / "primary" / "manifest.yaml").read_text()

        _push_update(
            work,
            bare,
            "prints/primary/manifest.yaml",
            "format_version: 1\ntables: {marker: {}}\n",
        )
        monkeypatch.setattr(remote_module, "CACHE_TTL_SECONDS", 0)

        second = materialize(address)

        assert second == first
        assert "marker" in (second / "prints" / "primary" / "manifest.yaml").read_text()

    def test_missing_git_binary_raises_naming_git(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bare, _work = _bare_repo(tmp_path)
        monkeypatch.setattr(remote_module.shutil, "which", lambda _binary: None)

        with pytest.raises(ConfigError, match="git binary not found"):
            materialize(RemoteAddress(remote=str(bare)))

    def test_an_unreachable_remote_raises_config_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.git"

        with pytest.raises(ConfigError, match="git failed"):
            materialize(RemoteAddress(remote=str(missing)))


class TestWatchForRefresh:
    def test_starts_one_daemon_thread_and_returns_immediately(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The sleep is stubbed to block forever, so only the thread's own start is under test."""

        monkeypatch.setattr(remote_module.time, "sleep", lambda _seconds: threading.Event().wait())

        before = set(threading.enumerate())
        watch_for_refresh(RemoteAddress(remote="unused"))
        after = set(threading.enumerate())

        new_threads = after - before
        assert len(new_threads) == 1
        assert next(iter(new_threads)).daemon is True
