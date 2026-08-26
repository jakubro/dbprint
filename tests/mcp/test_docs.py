"""docs/MCP.md's per-tool inputSchema blocks are generated from TOOL_DEFINITIONS.

Golden: the committed file must equal a fresh build, so schema and doc cannot drift.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_generator():
    """Import scripts/gen_mcp_docs.py so the test shares the generator's render path."""

    path = Path(__file__).resolve().parents[2] / "scripts" / "gen_mcp_docs.py"
    spec = importlib.util.spec_from_file_location("gen_mcp_docs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


gen = _load_generator()


def test_committed_doc_matches_a_fresh_build() -> None:
    assert gen.DOCS_PATH.read_text() == gen.build_document()


def test_every_tool_definition_produces_a_rendered_block() -> None:
    """Adding a tool to TOOL_DEFINITIONS with no matching MCP.md block fails loudly."""

    from dbprint.mcp.tools import TOOL_DEFINITIONS

    text = gen.build_document()

    for tool in TOOL_DEFINITIONS:
        assert f'"name": "{tool.name}"' in text
