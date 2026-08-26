"""Regenerate docs/MCP.md's per-tool inputSchema blocks from TOOL_DEFINITIONS.

Only the fenced json {"name": ..., "inputSchema": ...} block under each `### 4.N` heading is
generated; the surrounding prose stays hand-written. Run via `just docs`; golden-tested by
tests/mcp/test_docs.py so the schema and the doc cannot silently diverge.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from dbprint.mcp.tools import TOOL_DEFINITIONS, ToolDef


DOCS_PATH = Path(__file__).resolve().parents[1] / "docs" / "MCP.md"

# Matches one tool's fenced json block: ```json\n{ ... "name": "<tool>" ... }\n```
_BLOCK_RE = re.compile(r"```json\n(\{.*?\n\})\n```", re.DOTALL)


def render_tool_block(tool: ToolDef) -> str:
    """The fenced ```json block for one tool - name + inputSchema, nothing else."""

    schema = tool.input_schema
    inner: list[str] = [f'    "type": {json.dumps(schema["type"])}']
    properties = schema.get("properties")

    if properties:
        prop_lines = [
            f'      "{name}": {_render_compact(value)}' for name, value in properties.items()
        ]
        inner.append('    "properties": {\n' + ",\n".join(prop_lines) + "\n    }")

    if "required" in schema:
        inner.append(f'    "required": {_render_compact(schema["required"])}')

    schema_body = ",\n".join(inner)
    body = f'{{\n  "name": {json.dumps(tool.name)},\n  "inputSchema": {{\n{schema_body}\n  }}\n}}'

    return f"```json\n{body}\n```"


def _render_compact(value: object) -> str:
    """Single-line JSON for one property value or a `required` array, space-padded."""

    if isinstance(value, dict):
        parts = [f"{json.dumps(k)}: {_render_compact(v)}" for k, v in value.items()]

        return "{ " + ", ".join(parts) + " }"

    if isinstance(value, list):
        return "[" + ", ".join(_render_compact(v) for v in value) + "]"

    return json.dumps(value)


def build_document() -> str:
    """Return docs/MCP.md's full text with every tool's inputSchema block regenerated."""

    text = DOCS_PATH.read_text()
    by_name = {t.name: t for t in TOOL_DEFINITIONS}
    matches = list(_BLOCK_RE.finditer(text))
    out = []
    cursor = 0

    for match in matches:
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue

        tool = by_name.get(parsed.get("name") if isinstance(parsed, dict) else None)

        if tool is None:
            continue

        out.append(text[cursor : match.start()])
        out.append(render_tool_block(tool))
        cursor = match.end()

    out.append(text[cursor:])

    return "".join(out)


def write_document() -> None:
    """Regenerate and write docs/MCP.md in place."""

    DOCS_PATH.write_text(build_document())


if __name__ == "__main__":
    write_document()
    print(f"wrote {DOCS_PATH}")
