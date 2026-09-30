"""Path-valued credentials: every adapter declares its file keys, and build_engine anchors them."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest

from dbprint.adapters import Adapter
from dbprint.cli import engine_setup
from dbprint.cli.adapter_registry import ADAPTERS
from dbprint.config.project import ConnectionConfig, DiffConfig, StatisticsConfig


_FILE_SUFFIXES = ("_file", "_path", "_dir", "_dirname")
_FILE_VALUED = {"duckdb": {"database"}}
_NOT_A_FILESYSTEM_PATH = {"http_path"}


@pytest.mark.parametrize("kind", sorted(ADAPTERS))
def test_every_path_like_credential_key_is_declared(kind: str) -> None:
    adapter = ADAPTERS[kind]
    keys = {*adapter.REQUIRED_KEYS, *adapter.OPTIONAL_KEYS}
    file_like = {k for k in keys if k.endswith(_FILE_SUFFIXES)} - _NOT_A_FILESYSTEM_PATH

    assert file_like | _FILE_VALUED.get(kind, set()) == set(adapter.PATH_KEYS)


_SEEN: dict[str, str] = {}


def _recording(real: type[Adapter]) -> type[Adapter]:
    base: Any = real

    class Recording(base):
        def __init__(self, credentials: dict[str, str], **_: Any) -> None:
            _SEEN.clear()
            _SEEN.update(credentials)

    return Recording


def _required_env(kind: str, path_key: str, value: str) -> dict[str, str]:
    adapter = ADAPTERS[kind]
    env = {f"DBPRINT_C_{k.upper()}": "x" for k in adapter.REQUIRED_KEYS}
    env[f"DBPRINT_C_{path_key.upper()}"] = value

    return env


def _conn(kind: str, root: Path) -> ConnectionConfig:
    return ConnectionConfig(
        name="c",
        adapter=cast(Any, kind),
        auto=False,
        output=root / "prints",
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
    )


_PAIRS = [(kind, key) for kind in sorted(ADAPTERS) for key in ADAPTERS[kind].PATH_KEYS]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / "sub").mkdir(parents=True)
    (root / "keys").mkdir()
    (root / "keys" / "credential").write_text("x")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".credential").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(root / "sub")

    for name in list(os.environ):
        if name.startswith("DBPRINT_C_"):
            monkeypatch.delenv(name)

    return root


def _build(
    kind: str,
    root: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    recording = _recording(ADAPTERS[kind])
    monkeypatch.setattr(engine_setup, "get_adapter_class", lambda _: recording)

    for name, value in env.items():
        monkeypatch.setenv(name, value)

    engine_setup.build_engine(_conn(kind, root), root)

    return dict(_SEEN)


@pytest.mark.parametrize(("kind", "key"), _PAIRS)
def test_a_relative_path_resolves_against_the_project_root(
    kind: str,
    key: str,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _build(kind, project, _required_env(kind, key, "keys/credential"), monkeypatch)

    assert seen[key] == str(project / "keys" / "credential")


@pytest.mark.parametrize(("kind", "key"), _PAIRS)
def test_a_home_relative_path_expands(
    kind: str,
    key: str,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _build(kind, project, _required_env(kind, key, "~/.credential"), monkeypatch)

    assert seen[key] == str(Path(os.environ["HOME"]) / ".credential")


@pytest.mark.parametrize(("kind", "key"), _PAIRS)
def test_an_absolute_path_passes_through(
    kind: str,
    key: str,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absolute = str(project / "keys" / "credential")
    seen = _build(kind, project, _required_env(kind, key, absolute), monkeypatch)

    assert seen[key] == absolute


@pytest.mark.parametrize(("kind", "key"), _PAIRS)
def test_a_missing_file_is_refused_naming_the_resolved_path(
    kind: str,
    key: str,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(engine_setup.ConnectionSetupError) as caught:
        _build(kind, project, _required_env(kind, key, "missing.bin"), monkeypatch)

    assert str(project / "missing.bin") in str(caught.value)
    assert not (project / "sub" / "missing.bin").exists()


@pytest.mark.parametrize(
    "value",
    [":memory:", ":memory:named", "md:warehouse", "s3://bucket/x.duckdb"],
)
def test_a_value_naming_no_local_file_passes_through(
    value: str,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _build("duckdb", project, _required_env("duckdb", "database", value), monkeypatch)

    assert seen["database"] == value
