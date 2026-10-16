# dbprint skills - install guide

This directory contains a markdown skill that teaches an AI agent to open a committed print before it queries a database: which tool answers which question, and what the number it gets back does and does not cover. It names the MCP tools, and falls back to `dbprint context` in a shell where no server is connected.

## When to use this vs. the MCP server

| Surface | Use when |
|---|---|
| **Skill (this directory)** | The client supports markdown rules / skills / custom instructions. It states when to read the print before querying the database, and how to read a scoped or inferred number — guidance no tool description can carry. |
| **MCP server (`dbprint serve`)** | The client supports MCP and you want the tools themselves: native tool / resource primitives, multi-connection routing, and the token-budgeted `get_table_context`. |

They are complements rather than alternatives — the skill tells an agent which tool to call, the server provides the tools. Installed together, the skill's tool names resolve; installed alone, its shell route still works.

## Installing in Claude Code

1. Copy the file to `.claude/skills/dbprint/SKILL.md` in the project root, or to `~/.claude/skills/dbprint/SKILL.md` to install it for every project.
2. Claude Code surfaces it as a discoverable skill when the user asks database-related questions.

## Installing in Cursor

1. Add the contents of `dbprint.md` to your project's `.cursor/rules/` directory (Cursor reads rules per project).
2. Alternatively, paste it into the global Cursor rules under Settings -> Rules for AI.

## Installing in Cline

1. Open Cline settings and locate the Custom Instructions section.
2. Paste the contents of `dbprint.md`. Cline applies it on every session.

## The annotation skill

`dbprint-annotate.md` is a second skill, for writing a print rather than reading it: it has an agent write `description.md` and the annotation files from the code that writes each table and read-only queries, put every checkable statement into `claims`, and finish by running `dbprint annotate <table> --check` until no annotation finding remains. `dbprint annotate <table>` prints the same steps with one table's context and open questions attached, ready to pipe into an agent CLI. Install it the same way as the reading skill, in a `dbprint-annotate` skill directory of its own in Claude Code, or beside `dbprint.md` in any other client. It needs read-only database access, which the reading skill does not.

## Installing in any other client

Most agent clients accept markdown instructions in some shape (system prompt, custom instructions, project rules). The skill file is small and self-contained, and names nothing outside a print and the commands that read one, so it works in any client unchanged.

The tools it points at are specified in [`../../MCP.md`](../../MCP.md); serve them with `dbprint serve`.
