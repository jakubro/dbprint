# dbprint context skill - install guide

This directory contains a markdown skill that teaches an AI agent to open a committed print before it queries a database: which tool answers which question, and what the number it gets back does and does not cover. It names the MCP tools, and falls back to `dbprint context` in a shell where no server is connected.

## When to use this vs. the MCP server

| Surface | Use when |
|---|---|
| **Skill (this directory)** | The client supports markdown rules / skills / custom instructions. It supplies the judgement — when to reach for a print, and how to read a scoped or inferred number — which no tool description can carry. |
| **MCP server (`dbprint serve`)** | The client supports MCP and you want the tools themselves: native tool / resource primitives, multi-connection routing, and the token-budgeted `get_table_context`. |

They are complements rather than alternatives — the skill tells an agent what to reach for, the server is what it reaches. Installed together, the skill's tool names resolve; installed alone, its shell route still works.

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

Most agent clients accept markdown instructions in some shape (system prompt, custom instructions, project rules). The skill file is small and self-contained, and names nothing outside a print and the commands that read one, so it transplants cleanly.

The tools it points at are specified in [`../../MCP.md`](../../MCP.md); serve them with `dbprint serve`.
