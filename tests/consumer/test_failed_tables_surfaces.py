"""Every surface that lists tables or rejects a table name says which ones the last run could not
profile (SPEC 2.5), over one print holding a never-profiled and a carried failed table.
"""

from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result

from dbprint.adapters import MockAdapter, MockTable
from dbprint.cli.main import main
from dbprint.docs import web
from dbprint.engine import Engine, GenerateRequest
from dbprint.mcp import ServedConnections, dispatch
from dbprint.mcp import resources as mcp_resources
from dbprint.mcp.errors import McpError
from tests._curator import conn_config, curator_fixture
from tests._failing import Failing
from tests._regenerate import assert_leaves_regenerating_to_the_maintainer


NEVER = "public.extra"
CARRIED = "public.herbarium"
PHRASE = "could not profile"

_PROJECT_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
    profile:
      freshness:
        max_age_days: 7
"""


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("failed_tables")
    fixture = curator_fixture()
    conn = conn_config(root / "prints")
    Engine(MockAdapter(fixture), conn, root).generate()

    extra = replace(fixture[CARRIED], namespace_path=("public", "extra"))
    with_extra = {**fixture, NEVER: extra}
    failing = _Both(with_extra)
    Engine(failing, conn, root).generate(GenerateRequest(force=True))
    (root / ".dbprint.yaml").write_text(_PROJECT_YAML)

    return root


class _Both(Failing):
    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture, CARRIED, "extraction")

    def extract_ddl(self, fqn: str) -> str:
        if fqn == NEVER:
            raise RuntimeError("simulated extraction failure")

        return super().extract_ddl(fqn)


def _cli(project: Path, *args: str) -> tuple[int, str]:
    result = _run(project, *args)

    return result.exit_code, result.output


def _run(project: Path, *args: str) -> Result:
    old = Path.cwd()
    os.chdir(project)

    try:
        return CliRunner().invoke(main, list(args))
    finally:
        os.chdir(old)


def _state(project: Path) -> ServedConnections:
    conn = replace(conn_config(project / "prints"), name="primary")

    return ServedConnections(served={"primary": conn}, default="primary")


def _mcp_error(call: Callable[[], object]) -> str:
    with pytest.raises(McpError) as raised:
        call()

    return str(raised.value)


def test_offline_check_fails_the_run_naming_both(project: Path) -> None:
    result = _run(project, "check", "--format", "json")
    causes = {entry["subject"]: entry["cause"] for entry in json.loads(result.stdout)[0]["not_run"]}

    assert result.exit_code == 5
    assert NEVER in result.output and CARRIED in result.output
    assert set(causes) == {NEVER, CARRIED}

    for cause in causes.values():
        assert_leaves_regenerating_to_the_maintainer(cause)


def test_check_names_an_unprofiled_table_as_context_refuses_it(project: Path) -> None:
    check = _run(project, "check", "--format", "json")
    context = _run(project, "context", NEVER)
    cause = next(
        e["cause"] for e in json.loads(check.stdout)[0]["not_run"] if e["subject"] == NEVER
    )

    assert cause in context.output


def test_list_names_both(project: Path) -> None:
    code, output = _cli(project, "list", "--format", "json")

    assert code == 0
    assert json.loads(output)[0]["failed_tables"] == [NEVER, CARRIED]


def test_context_refuses_the_never_profiled_one_by_its_cause(project: Path) -> None:
    code, output = _cli(project, "context", NEVER)

    assert code != 0
    assert PHRASE in output
    assert "Did you mean" not in output
    assert_leaves_regenerating_to_the_maintainer(output)


@pytest.mark.parametrize("fmt", ["md", "json", "yaml"])
def test_context_serves_the_carried_one_with_the_phrase(project: Path, fmt: str) -> None:
    result = _run(project, "context", CARRIED, "--format", fmt)

    assert result.exit_code == 0
    assert PHRASE in result.output
    assert_leaves_regenerating_to_the_maintainer(_unprofiled_note(result.stdout, fmt))


@pytest.mark.parametrize("selection", [("public.*",), ("--all",)], ids=["glob", "all"])
def test_context_over_many_tables_names_the_never_profiled_one(
    project: Path,
    selection: tuple[str, ...],
) -> None:
    code, output = _cli(project, "context", *selection)
    note = next(line for line in output.splitlines() if "not profiled by the last run" in line)

    assert code == 0
    assert NEVER in note
    assert CARRIED not in note
    assert_leaves_regenerating_to_the_maintainer(note)


def test_mcp_get_manifest_filters_the_list_by_its_pattern(project: Path) -> None:
    herbarium = dispatch(_state(project), "get_manifest", {"pattern": "public.herb*"})
    curator = dispatch(_state(project), "get_manifest", {"pattern": "public.curator"})

    assert isinstance(herbarium, dict) and isinstance(curator, dict)
    assert herbarium["failed_tables"] == [CARRIED]
    assert "failed_tables" not in curator


def test_mcp_list_tables_names_both(project: Path) -> None:
    reply = dispatch(_state(project), "list_tables", {})

    assert isinstance(reply, dict)
    assert reply["failed_tables"] == [NEVER, CARRIED]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_table_context", {"table": NEVER}),
        ("resolve_value", {"table": NEVER, "column": "id", "text": "x"}),
    ],
)
def test_an_mcp_tool_refuses_the_never_profiled_one_by_its_cause(
    project: Path,
    tool: str,
    arguments: dict[str, str],
) -> None:
    refusal = _mcp_error(lambda: dispatch(_state(project), tool, arguments))

    assert PHRASE in refusal
    assert_leaves_regenerating_to_the_maintainer(refusal)


def test_an_mcp_resource_refuses_the_never_profiled_one_by_its_cause(project: Path) -> None:
    uri = "dbprint://primary/public/extra/statistics"

    refusal = _mcp_error(lambda: mcp_resources.read(_state(project), uri))

    assert PHRASE in refusal
    assert_leaves_regenerating_to_the_maintainer(refusal)


def test_the_mcp_context_of_the_carried_one_carries_the_phrase(project: Path) -> None:
    reply = dispatch(_state(project), "get_table_context", {"table": CARRIED, "format": "md"})

    assert isinstance(reply, str)
    assert PHRASE in reply
    assert_leaves_regenerating_to_the_maintainer(_unprofiled_note(reply, "md"))


def test_the_docs_site_names_both(project: Path) -> None:
    client = web.create_app(
        [replace(conn_config(project / "prints"), name="primary")],
    ).test_client()
    index = client.get("/").data.decode()
    page = client.get(f"/t/primary/{CARRIED}").data.decode()

    listing = re.search(r"Not profiled by the last run.*?</p>", index, re.DOTALL)
    notice = re.search(r'<div class="notice">(.*?)</div>', page, re.DOTALL)

    assert NEVER in index and CARRIED in index
    assert PHRASE in page
    assert listing is not None and notice is not None
    assert_leaves_regenerating_to_the_maintainer(" ".join(html.unescape(listing.group(0)).split()))
    assert_leaves_regenerating_to_the_maintainer(html.unescape(notice.group(1)))


def _unprofiled_note(reply: str, fmt: str) -> str:
    if fmt == "md":
        return next(line for line in reply.splitlines() if line.startswith("Unprofiled:"))

    return (json.loads(reply) if fmt == "json" else yaml.safe_load(reply))["_unprofiled"]
