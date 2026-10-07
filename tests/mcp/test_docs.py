"""docs/MCP.md's per-tool inputSchema blocks are generated from TOOL_DEFINITIONS.

Golden: the committed file must equal a fresh build, so schema and doc cannot drift.
"""

from __future__ import annotations

from tests._scripts import load_script


gen = load_script("gen_mcp_docs")


def test_committed_doc_matches_a_fresh_build() -> None:
    assert gen.DOCS_PATH.read_text() == gen.build_document()


def test_every_tool_definition_produces_a_rendered_block() -> None:
    """Adding a tool to TOOL_DEFINITIONS with no matching MCP.md block fails loudly."""

    from dbprint.mcp.tools import TOOL_DEFINITIONS

    text = gen.build_document()

    for tool in TOOL_DEFINITIONS:
        assert f'"name": "{tool.name}"' in text
