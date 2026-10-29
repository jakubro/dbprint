# dbprint skills - install guide

This directory holds two markdown skills in the Agent Skills layout, each a file whose frontmatter `description` tells an agent when to load it:

- **`dbprint.md`**, the reading skill, teaches an agent to open a committed print before it queries a database: which tool answers which question, and what the number it gets back does and does not cover. It names the MCP tools, and falls back to `dbprint context` in a shell where no server is connected.
- **`dbprint-annotate.md`**, the annotation skill, is for writing a print rather than reading it, described in its own section below.

## When to use this vs. the MCP server

| Surface | Use when |
|---|---|
| **Skill (this directory)** | The client loads skills or markdown rules. Its description reaches the agent before any dbprint tool does — while it is about to query a database, write SQL or trust a claim about the data — and sends it to the print first. Installed without the server, its shell route still works. |
| **MCP server (`dbprint serve`)** | The client supports MCP and you want the tools themselves: native tool / resource primitives, multi-connection routing, and paged replies. The server's own instructions carry the reading rules to every client that connects. |

They are complements rather than alternatives — the skill tells an agent which tool to call, the server provides the tools. Installed together, the skill's tool names resolve; installed alone, its shell route still works.

## Installing in Claude Code, Cursor and Cline

All three load each skill from a directory named for it under `skills/`, as `skills/dbprint/SKILL.md` for this one, and list its description to the agent, which loads the rest when a task matches.

1. Copy `dbprint.md` to `.claude/skills/dbprint/SKILL.md` in the project root. Claude Code, Cursor and Cline all read project skills from `.claude/skills/`.
2. To install it for every project instead, copy it to `~/.claude/skills/dbprint/SKILL.md`, which Claude Code and Cursor read, and to `~/.cline/skills/dbprint/SKILL.md` for Cline.

In Claude Code, `/dbprint` also loads it by hand.

## The annotation skill

`dbprint-annotate.md` has an agent write `description.md` and the annotation files from the code that writes each table and read-only queries, put every checkable statement into `claims`, and finish by running `dbprint annotate <table> --check` until no annotation finding remains. `dbprint annotate <table>` prints the same steps with one table's context and open questions attached, ready to pipe into an agent CLI. Install it the same way as the reading skill, as `skills/dbprint-annotate/SKILL.md` beside `skills/dbprint/`. It needs read-only database access, which the reading skill does not.

## Installing in any other client

Most agent clients accept markdown instructions in some shape (system prompt, custom instructions, project rules). Each skill file is small and self-contained, and names nothing outside a print and the commands that read one, so its body — everything below the frontmatter — works in any client unchanged.

The tools it points at are specified in [`../../MCP.md`](../../MCP.md); serve them with `dbprint serve`.
