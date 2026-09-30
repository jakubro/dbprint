"""`_substrates.py` - which stranded substrates a sweep may reclaim, and that it reclaims each once."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from tests import _substrates
from tests._substrates import Owner


HERE = Owner(pid=100, start=5000, pid_ns="pid:[4026531836]", boot_id="boot-a")


class TestOwnerIsDead:
    def test_an_owner_from_another_boot_is_dead(self) -> None:
        owner = replace(HERE, boot_id="boot-b")

        assert _substrates.owner_is_dead(owner, HERE, lambda _: 5000) is True

    def test_an_owner_in_another_pid_namespace_cannot_be_judged(self) -> None:
        owner = replace(HERE, pid_ns="pid:[4026532999]")

        assert _substrates.owner_is_dead(owner, HERE, lambda _: None) is None

    def test_a_running_owner_is_alive(self) -> None:
        assert _substrates.owner_is_dead(HERE, HERE, lambda _: 5000) is False

    def test_a_missing_pid_is_dead(self) -> None:
        assert _substrates.owner_is_dead(HERE, HERE, lambda _: None) is True

    def test_a_recycled_pid_is_dead(self) -> None:
        assert _substrates.owner_is_dead(HERE, HERE, lambda _: 7777) is True


class TestProcessStart:
    def test_it_reads_this_process_the_same_twice(self) -> None:
        first = _substrates.process_start(os.getpid())

        assert first is not None
        assert _substrates.process_start(os.getpid()) == first

    def test_a_pid_that_does_not_exist_has_none(self) -> None:
        assert _substrates.process_start(2**22 + 1) is None


class TestRegistered:
    def test_the_marker_exists_while_the_body_runs_and_not_after(self, tmp_path: Path) -> None:
        with _substrates.registered(tmp_path, "dbprint-x", "spark-warehouse", path="p") as marker:
            seen = json.loads(marker.read_text())

        assert seen["kind"] == "spark-warehouse"
        assert seen["handle"] == {"path": "p"}
        assert seen["owner"]["pid"] == os.getpid()
        assert not marker.exists()

    def test_a_body_that_raises_still_removes_its_marker(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError), _substrates.registered(tmp_path, "x", "k") as marker:
            raise RuntimeError

        assert not marker.exists()


class TestShared:
    def test_the_first_caller_starts_it_and_the_rest_reuse_its_handle(self, tmp_path: Path) -> None:
        starts: list[str] = []

        def start() -> dict[str, str]:
            starts.append("started")
            _substrates.register(tmp_path, "dbprint-x", "postgres", HERE, port="5999")

            return {"port": "5999"}

        first = _substrates.shared(tmp_path, "postgres", HERE, start)
        second = _substrates.shared(tmp_path, "postgres", HERE, start)

        assert starts == ["started"]
        assert first == second == {"port": "5999"}

    def test_another_runs_substrate_is_never_reused(self, tmp_path: Path) -> None:
        _substrates.register(tmp_path, "dbprint-other", "postgres", _dead_owner(), port="1")

        def start() -> dict[str, str]:
            return {"port": "2"}

        assert _substrates.shared(tmp_path, "postgres", HERE, start) == {"port": "2"}

    def test_concurrent_processes_start_it_once(self, tmp_path: Path) -> None:
        code = textwrap.dedent(
            """
            import sys
            from pathlib import Path
            from tests import _substrates

            root = Path(sys.argv[1])
            owner = _substrates.Owner(pid=1, start=2, pid_ns="ns", boot_id="boot")

            def start():
                with (root / "starts").open("a") as log:
                    log.write("x")
                _substrates.register(root, "m", "k", owner, port="1")
                return {"port": "1"}

            print(_substrates.shared(root, "k", owner, start)["port"])
            """,
        )
        lib_root = Path(__file__).resolve().parent.parent
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(tmp_path)],
                cwd=lib_root,
                stdout=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        outputs = [process.communicate()[0].strip() for process in processes]

        assert outputs == ["1", "1", "1", "1"]
        assert (tmp_path / "starts").read_text() == "x"


class TestStopOwned:
    def test_only_the_owners_substrates_are_stopped_and_unregistered(self, tmp_path: Path) -> None:
        ours = _substrates.register(tmp_path, "dbprint-ours", "spark-warehouse", HERE, path="a")
        theirs = _plant(tmp_path, "dbprint-theirs", _dead_owner(), path="b")
        stopped: list[str] = []

        _substrates.stop_owned(
            tmp_path,
            {"spark-warehouse": lambda handle, _owner: stopped.append(handle["path"])},
            HERE,
        )

        assert stopped == ["a"]
        assert not ours.exists()
        assert theirs.exists()


class TestSweep:
    def test_a_dead_owners_directory_is_reclaimed_once(self, tmp_path: Path) -> None:
        stranded = tmp_path / "dbprint-spark-warehouse-dead"
        stranded.mkdir()
        _plant(tmp_path, "dbprint-spark-warehouse-dead", _dead_owner(), path=str(stranded))
        stoppers = {"spark-warehouse": _substrates.stop_directory}

        first = _substrates.sweep(tmp_path, stoppers)
        second = _substrates.sweep(tmp_path, stoppers)

        assert not stranded.exists()
        assert len(first) == 1
        assert first[0].startswith("reclaimed from dead owner")
        assert second == []
        assert list(_substrates.registry(tmp_path).iterdir()) == []

    def test_a_live_owners_entry_is_never_touched(self, tmp_path: Path) -> None:
        live = tmp_path / "dbprint-spark-warehouse-live"
        live.mkdir()
        _plant(
            tmp_path,
            "dbprint-spark-warehouse-live",
            _substrates.current_owner(),
            path=str(live),
        )

        lines = _substrates.sweep(tmp_path, {"spark-warehouse": _unreachable})

        assert live.exists()
        assert lines == []

    def test_another_namespaces_entry_is_reported_and_left(self, tmp_path: Path) -> None:
        other = replace(_substrates.current_owner(), pid_ns="pid:[1]")
        _plant(tmp_path, "dbprint-spark-warehouse-ns", other, path=str(tmp_path / "gone"))

        lines = _substrates.sweep(tmp_path, {"spark-warehouse": _unreachable})

        assert len(lines) == 1
        assert "another PID namespace" in lines[0]

    def test_an_entry_another_sweep_already_claimed_is_skipped(self, tmp_path: Path) -> None:
        marker = _plant(tmp_path, "dbprint-x", _dead_owner(), path=str(tmp_path / "gone"))
        claimed = marker.with_name(marker.name + ".reaping-1")
        entry = json.loads(marker.read_text())
        claimed.write_text(json.dumps({**entry, "reaper": asdict(_substrates.current_owner())}))
        marker.unlink()

        assert _substrates.sweep(tmp_path, {"spark-warehouse": _unreachable}) == []
        assert claimed.exists()

    def test_a_dead_reapers_claim_is_taken_over(self, tmp_path: Path) -> None:
        stranded = tmp_path / "dbprint-spark-warehouse-half"
        stranded.mkdir()
        marker = _plant(tmp_path, "dbprint-spark-warehouse-half", _dead_owner(), path=str(stranded))
        claimed = marker.with_name(marker.name + ".reaping-1")
        entry = json.loads(marker.read_text())
        claimed.write_text(json.dumps({**entry, "reaper": asdict(_dead_owner())}))
        marker.unlink()

        lines = _substrates.sweep(tmp_path, {"spark-warehouse": _substrates.stop_directory})

        assert not stranded.exists()
        assert len(lines) == 1
        assert list(_substrates.registry(tmp_path).iterdir()) == []

    def test_a_suite_named_directory_without_a_marker_is_reported_not_removed(
        self,
        tmp_path: Path,
    ) -> None:
        unmarked = tmp_path / "dbprint-test-postgres-0badc0de"
        unmarked.mkdir()

        lines = _substrates.sweep(tmp_path, {})

        assert unmarked.exists()
        assert lines == [f"left alone, no marker: {unmarked}"]

    def test_nothing_stranded_reports_nothing(self, tmp_path: Path) -> None:
        assert _substrates.sweep(tmp_path, {}) == []


class TestStartFailure:
    def test_it_names_the_server_the_substrates_and_the_memory(self, tmp_path: Path) -> None:
        _plant(tmp_path, "dbprint-x", _dead_owner(), path="/nowhere")

        message = _substrates.start_failure("postgres", "could not bind", tmp_path, port=1)

        assert message.splitlines()[0] == "postgres could not start on 127.0.0.1:1"
        assert "server said: could not bind" in message
        assert "live suite substrates: 1" in message
        assert "(dead)" in message
        assert "MemAvailable:" in message


class TestPortHolder:
    def test_a_listening_socket_is_traced_to_its_process(self) -> None:
        code = (
            "import socket, sys; s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen();"
            "print(s.getsockname()[1], flush=True); sys.stdin.read()"
        )
        server = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

        try:
            assert server.stdout is not None
            port = int(server.stdout.readline())

            assert _substrates.port_holder(port) == f"pid {server.pid}: {sys.executable} -c {code}"
        finally:
            server.communicate("")

    def test_an_unbound_port_has_no_holder(self) -> None:
        assert _substrates.port_holder(1) is None


def _plant(root: Path, name: str, owner: Owner, **handle: str) -> Path:
    directory = _substrates.registry(root)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"{name}.json"
    marker.write_text(
        json.dumps({"kind": "spark-warehouse", "handle": handle, "owner": asdict(owner)}),
    )

    return marker


def _dead_owner() -> Owner:
    return replace(_substrates.current_owner(), pid=2**22 + 1)


def _unreachable(*_: object) -> None:
    raise AssertionError("a stopper ran for an entry that must be left alone")
