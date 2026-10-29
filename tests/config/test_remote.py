"""Git-address `--project` locators: offline parsing, then clone/refresh against a local repo.

`materialize` runs against a real local git repository - never a network host.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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


def _rewrite_remote(work: Path, bare: Path, content: str, *, branch: str = "main") -> None:
    """Replace the tip commit and force-push it - the move `pull --ff-only` cannot follow."""

    (work / "prints" / "primary" / "manifest.yaml").write_text(content)
    _run_git(["add", "."], cwd=work)
    _run_git(["commit", "--amend", "-m", "rewritten"], cwd=work)
    _run_git(["push", "--force", "--quiet", str(bare), f"{branch}:{branch}"], cwd=work)


def _head_sha(work: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _tag(work: Path, bare: Path, name: str, *, force: bool = False) -> None:
    """Point `name` at the working repository's tip and publish it to `bare`."""

    _run_git(["tag", "-f", name] if force else ["tag", name], cwd=work)
    _run_git(["push", "--force", "--quiet", str(bare), name], cwd=work)


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

    @pytest.mark.parametrize(
        ("value", "refusal"),
        [
            ("--upload-pack=touch seed#main:.", "a remote may not begin with '-'"),
            ("git@github.com:acme/demo.git#-dormant:.", "a ref may not begin with '-'"),
            ("https://github.com/acme/demo/blob/-dormant/project1/", "a ref may not begin"),
            ("git@github.com:acme/demo.git#main:/srv/seedbank", "must be relative"),
            ("git@github.com:acme/demo.git#main:../seedbank", "must be relative"),
            ("git@github.com:acme/demo.git#main:project1/../../seedbank", "must be relative"),
            ("git@github.com:acme/demo.git#main:C:\\seedbank", "must be relative"),
            ("git@github.com:acme/demo.git#main:\\seedbank", "must be relative"),
            ("https://github.com/acme/demo/blob/main/../seedbank/", "must be relative"),
        ],
    )
    def test_a_part_that_could_reach_git_as_an_option_or_leave_the_clone_is_refused(
        self,
        value: str,
        refusal: str,
    ) -> None:
        with pytest.raises(ConfigError, match=re.escape(refusal)) as raised:
            parse_address(value)

        assert repr(value) in str(raised.value)

    @pytest.mark.parametrize(
        ("value", "subpath"),
        [
            ("git@github.com:acme/demo.git#main:.", "."),
            ("git@github.com:acme/demo.git#main:seed..bank/plot", "seed..bank/plot"),
            ("git@github.com:acme/demo.git#main:project1/.dbprint.yaml", "project1/.dbprint.yaml"),
        ],
    )
    def test_a_subpath_inside_the_repository_parses(self, value: str, subpath: str) -> None:
        address = parse_address(value)

        assert address is not None
        assert address.subpath == subpath


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

    def test_reuses_within_the_ttl_without_a_fetch(self, tmp_path: Path) -> None:
        bare, work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare))
        materialize(address)
        _push_update(
            work,
            bare,
            "prints/primary/manifest.yaml",
            "format_version: 1\ntables: {marker: {}}\n",
        )

        local = materialize(address)

        assert "marker" not in (local / "prints" / "primary" / "manifest.yaml").read_text()

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

    def test_a_rewritten_history_is_followed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A force-push leaves the cache on a commit the remote no longer descends from."""

        bare, work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare))
        materialize(address)

        _rewrite_remote(work, bare, "format_version: 1\ntables: {rewritten: {}}\n")
        monkeypatch.setattr(remote_module, "CACHE_TTL_SECONDS", 0)

        local = materialize(address)

        assert "rewritten" in (local / "prints" / "primary" / "manifest.yaml").read_text()

    def test_a_tag_ref_refreshes_once_its_ttl_has_elapsed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tag checkout is detached, where `git pull` cannot run at all."""

        bare, work = _bare_repo(tmp_path)
        _tag(work, bare, "v1.2.0")
        address = RemoteAddress(remote=str(bare), ref="v1.2.0")
        materialize(address)
        monkeypatch.setattr(remote_module, "CACHE_TTL_SECONDS", 0)

        local = materialize(address)

        assert (local / ".dbprint.yaml").is_file()

    def test_a_moved_tag_is_followed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bare, work = _bare_repo(tmp_path)
        _tag(work, bare, "v1.2.0")
        address = RemoteAddress(remote=str(bare), ref="v1.2.0")
        materialize(address)

        _push_update(
            work,
            bare,
            "prints/primary/manifest.yaml",
            "format_version: 1\ntables: {moved: {}}\n",
        )
        _tag(work, bare, "v1.2.0", force=True)
        monkeypatch.setattr(remote_module, "CACHE_TTL_SECONDS", 0)

        local = materialize(address)

        assert "moved" in (local / "prints" / "primary" / "manifest.yaml").read_text()

    def test_an_unreachable_remote_over_a_usable_cache_reports_and_serves_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A network that is down is no reason to refuse a print fifteen minutes old."""

        bare, _work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare))
        materialize(address)
        shutil.rmtree(bare)
        monkeypatch.setattr(remote_module, "CACHE_TTL_SECONDS", 0)
        reported: list[str] = []

        local = materialize(address, on_degraded=reported.append)

        assert (local / ".dbprint.yaml").is_file()
        assert reported and str(bare) in reported[0]

    def test_the_warning_ages_the_content_not_the_attempt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The freshness stamp advances on a failed attempt, so it cannot date the copy."""

        bare, _work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare))
        local = materialize(address)
        shutil.rmtree(bare)
        monkeypatch.setattr(remote_module, "CACHE_TTL_SECONDS", 0)

        fresh = local / remote_module._FRESH_FILENAME
        aged = time.time() - 3 * 60 * 60
        os.utime(fresh, (aged, aged))
        reported: list[str] = []

        materialize(address, on_degraded=reported.append)

        assert "180 minutes ago" in reported[0], reported

    def test_a_failed_refresh_records_the_attempt(self, tmp_path: Path) -> None:
        """The stamp bounds attempts, not successes - otherwise every later run retries."""

        bare, _work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare))
        local = materialize(address)
        shutil.rmtree(bare)
        aged = time.time() - 60 * 60
        os.utime(local / remote_module._STAMP_FILENAME, (aged, aged))
        materialize(address, on_degraded=lambda _message: None)
        reported: list[str] = []

        local = materialize(address, on_degraded=reported.append)

        assert reported == []
        assert (local / ".dbprint.yaml").is_file()

    def test_a_full_sha_ref_is_checked_out(self, tmp_path: Path) -> None:
        """What a forge's permalink carries; `clone --branch` cannot take it."""

        bare, work = _bare_repo(tmp_path)
        pinned = _head_sha(work)
        _push_update(
            work,
            bare,
            "prints/primary/manifest.yaml",
            "format_version: 1\ntables: {later: {}}\n",
        )

        local = materialize(RemoteAddress(remote=str(bare), ref=pinned))

        assert "later" not in (local / "prints" / "primary" / "manifest.yaml").read_text()

    def test_a_short_sha_ref_is_checked_out(self, tmp_path: Path) -> None:
        bare, work = _bare_repo(tmp_path)
        pinned = _head_sha(work)[:7]

        local = materialize(RemoteAddress(remote=str(bare), ref=pinned))

        assert (local / ".dbprint.yaml").is_file()

    def test_a_branch_named_like_a_hash_resolves_as_the_branch(self, tmp_path: Path) -> None:
        """git owns the ambiguity; a "looks like a SHA" predicate would take this branch away."""

        bare, work = _bare_repo(tmp_path)
        _run_git(["checkout", "-b", "abc1234"], cwd=work)
        _push_update(
            work,
            bare,
            "prints/primary/manifest.yaml",
            "format_version: 1\ntables: {branch_named_like_a_hash: {}}\n",
            branch="abc1234",
        )

        local = materialize(RemoteAddress(remote=str(bare), ref="abc1234"))

        manifest = (local / "prints" / "primary" / "manifest.yaml").read_text()
        assert "branch_named_like_a_hash" in manifest

    def test_a_commit_pinned_cache_is_never_refreshed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A commit cannot move, so a refresh could only refetch what is already on disk."""

        bare, work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare), ref=_head_sha(work))
        materialize(address)
        shutil.rmtree(bare)
        monkeypatch.setattr(remote_module, "CACHE_TTL_SECONDS", 0)
        reported: list[str] = []

        local = materialize(address, on_degraded=reported.append)

        assert reported == []
        assert (local / ".dbprint.yaml").is_file()

    def test_an_unresolvable_ref_names_the_ref(self, tmp_path: Path) -> None:
        bare, _work = _bare_repo(tmp_path)

        with pytest.raises(ConfigError, match="'no-such-ref' could not be resolved"):
            materialize(RemoteAddress(remote=str(bare), ref="no-such-ref"))

    def test_a_ref_that_names_a_path_is_refused(self, tmp_path: Path) -> None:
        """Without `--`, git reads a name no ref carries as a pathspec and exits 0 on the
        default branch - a wrong clone served with nothing said.
        """

        bare, _work = _bare_repo(tmp_path)

        with pytest.raises(ConfigError, match="'prints' could not be resolved"):
            materialize(RemoteAddress(remote=str(bare), ref="prints"))

    def test_a_failed_checkout_leaves_no_cache_behind(self, tmp_path: Path) -> None:
        """Half a clone would otherwise be served, stamped and fresh, on every later run."""

        bare, _work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare), ref="no-such-ref")

        with pytest.raises(ConfigError):
            materialize(address)

        assert not remote_module._cache_dir_for(address).exists()

    def test_a_cache_directory_that_cannot_be_read_is_re_cloned(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """What a clone killed mid-flight leaves behind: a stamped directory git cannot read."""

        bare, _work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare))
        local = materialize(address)
        shutil.rmtree(local / ".git")
        (local / ".dbprint.yaml").unlink()
        monkeypatch.setattr(remote_module, "CACHE_TTL_SECONDS", 0)

        again = materialize(address)

        assert (again / ".dbprint.yaml").is_file()


class TestGitNeverReadsAPartAsAnOption:
    """Every positional a locator supplies sits past `--`; a subpath never leaves the clone."""

    @pytest.fixture
    def argv(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []
        real = remote_module._run_git

        def recording(args: list[str]) -> str:
            calls.append(args)

            return real(args)

        monkeypatch.setattr(remote_module, "_run_git", recording)

        return calls

    def test_clone_and_fetch_take_their_positionals_after_a_separator(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[list[str]],
    ) -> None:
        bare, _work = _bare_repo(tmp_path)
        address = RemoteAddress(remote=str(bare), ref="main")
        materialize(address)
        monkeypatch.setattr(remote_module, "CACHE_TTL_SECONDS", 0)
        materialize(address)

        clone = next(call for call in argv if "clone" in call)
        fetch = next(call for call in argv if "fetch" in call)

        assert clone[clone.index("clone") + 1 :] == [
            "--quiet",
            "--",
            str(bare),
            str(remote_module._cache_dir_for(address)),
        ]
        assert fetch[fetch.index("fetch") + 1 :] == ["--quiet", "--", "origin", "main"]

    def test_a_subpath_through_a_symlink_out_of_the_clone_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        bare, work = _bare_repo(tmp_path)
        outside = tmp_path / "seedbank"
        outside.mkdir()
        (outside / ".dbprint.yaml").write_text("connections: {}\n")
        (work / "escape").symlink_to(outside)
        (work / "alias").symlink_to("prints")
        _run_git(["add", "escape", "alias"], cwd=work)
        _run_git(["commit", "-m", "links"], cwd=work)
        _run_git(["push", "--quiet", str(bare), "main:main"], cwd=work)

        with pytest.raises(ConfigError, match="'escape' resolves outside the cloned repository"):
            materialize(RemoteAddress(remote=str(bare), subpath="escape"))

        inside = materialize(RemoteAddress(remote=str(bare), subpath="alias"))

        assert (inside / "primary" / "manifest.yaml").is_file()


class TestWatchForRefresh:
    def test_starts_one_daemon_thread_and_returns_immediately(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The sleep is stubbed to block forever, so only the thread's own start is under test."""

        clock = SimpleNamespace(sleep=lambda _seconds: threading.Event().wait())
        monkeypatch.setattr(remote_module, "time", clock)

        before = set(threading.enumerate())
        watch_for_refresh(RemoteAddress(remote="unused"))
        after = set(threading.enumerate())

        new_threads = after - before
        assert len(new_threads) == 1
        assert next(iter(new_threads)).daemon is True
