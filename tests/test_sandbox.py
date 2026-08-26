"""The decision to re-exec the suite under a sandbox, and the mounts it asks for.

The sandbox itself only runs outside a container, so what is exercised here is the choice
and the command - never a real `bwrap`.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests import conftest


@pytest.fixture
def recorded_exec(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture what the re-exec would have replaced this process with."""

    calls: list[list[str]] = []

    def record(path: str, argv: list[str], *rest: Any) -> None:
        del path, rest
        calls.append(argv)

    monkeypatch.setattr(conftest.os, "execv", record)
    monkeypatch.delenv(conftest._SANDBOX_MARKER, raising=False)

    return calls


class TestWhenTheSandboxIsSkipped:
    def test_a_container_run_is_left_alone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recorded_exec: list[list[str]],
    ) -> None:
        monkeypatch.setattr(conftest, "in_container", lambda: True)

        conftest._reexec_under_sandbox()

        assert recorded_exec == []

    def test_an_already_sandboxed_process_does_not_nest(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recorded_exec: list[list[str]],
    ) -> None:
        monkeypatch.setattr(conftest, "in_container", lambda: False)
        monkeypatch.setenv(conftest._SANDBOX_MARKER, "1")

        conftest._reexec_under_sandbox()

        assert recorded_exec == []


class TestOnAHost:
    def test_a_missing_bwrap_refuses_rather_than_continuing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recorded_exec: list[list[str]],
    ) -> None:
        monkeypatch.setattr(conftest, "in_container", lambda: False)
        monkeypatch.setattr(conftest.shutil, "which", lambda _: None)

        with pytest.raises(RuntimeError, match="bubblewrap"):
            conftest._reexec_under_sandbox()

        assert recorded_exec == []

    def test_the_suite_is_re_execed_inside_the_sandbox(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recorded_exec: list[list[str]],
    ) -> None:
        monkeypatch.setattr(conftest, "in_container", lambda: False)
        monkeypatch.setattr(conftest.shutil, "which", lambda _: "/usr/bin/bwrap")

        conftest._reexec_under_sandbox()

        argv = recorded_exec[0]
        tail = argv[argv.index("--die-with-parent") + 1 :]

        assert len(recorded_exec) == 1
        assert argv[0] == "/usr/bin/bwrap"
        assert tail[:3] == [conftest.sys.executable, "-m", "pytest"]

    def test_the_mounts_leave_one_writable_tree_and_no_network(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recorded_exec: list[list[str]],
    ) -> None:
        monkeypatch.setattr(conftest, "in_container", lambda: False)
        monkeypatch.setattr(conftest.shutil, "which", lambda _: "/usr/bin/bwrap")

        conftest._reexec_under_sandbox()
        argv = recorded_exec[0]

        assert argv[argv.index("--ro-bind") : argv.index("--ro-bind") + 3] == [
            "--ro-bind",
            "/",
            "/",
        ]
        assert argv[argv.index("--tmpfs") : argv.index("--tmpfs") + 2] == ["--tmpfs", "/tmp"]
        assert argv[argv.index("--perms") : argv.index("--perms") + 2] == ["--perms", "1777"]
        assert "--unshare-net" in argv
        assert "--die-with-parent" in argv

    def test_the_child_is_marked_so_it_does_not_sandbox_itself(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recorded_exec: list[list[str]],
    ) -> None:
        monkeypatch.setattr(conftest, "in_container", lambda: False)
        monkeypatch.setattr(conftest.shutil, "which", lambda _: "/usr/bin/bwrap")

        conftest._reexec_under_sandbox()

        assert conftest.os.environ[conftest._SANDBOX_MARKER] == "1"
        assert recorded_exec != []
