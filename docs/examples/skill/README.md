# dbprint context skill - install guide

This directory contains a markdown skill that teaches an AI agent where a dbprint print's files live and which to open, then points it at that print's own `reading.md` for how to interpret them. Useful for clients that don't speak MCP (or where you prefer markdown-instruction-style integration over a running server).

## When to use this vs. the MCP server

| Surface | Use when |
|---|---|
| **Skill (this directory)** | The client supports markdown rules / skills / custom instructions. The project's `prints/` is committed and the agent can read files in the workspace directly. Zero processes to run. |
| **MCP server (`dbprint serve`)** | The client supports MCP and you want native tool / resource primitives, multi-connection routing, and the bundled token-budgeted `get_table_context` tool. |

Both surfaces read the same on-disk artifacts; pick whichever fits the client and workflow best.

## Installing in Claude Code

1. Place the skill at `.claude/skills/dbprint.md` in the project root, OR drop `dbprint.md` directly into your global `~/.claude/skills/` directory.
2. Claude Code surfaces it as a discoverable skill when the user asks database-related questions.

## Installing in Cursor

1. Add the contents of `dbprint.md` to your project's `.cursor/rules/` directory (Cursor reads rules per project).
2. Alternatively, paste it into the global Cursor rules under Settings -> Rules for AI.

## Installing in Cline

1. Open Cline settings and locate the Custom Instructions section.
2. Paste the contents of `dbprint.md`. Cline applies it on every session.

## Installing in any other client

Most agent clients accept markdown instructions in some shape (system prompt, custom instructions, project rules). The skill file is small, self-contained, and references only files inside `prints/`, so it transplants cleanly.

For larger setups (multi-connection projects, token-budgeted context assembly, structured tool calls), prefer `dbprint serve` and the MCP integration documented in [`../../MCP.md`](../../MCP.md).
