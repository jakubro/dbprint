# Giving a print to an agent

A committed print is already readable by an agent that can open files in the workspace — that is the point of putting it in the repository as plain text. What this page adds is the two ways to make the agent *reliably* reach for it, and to read it correctly when it does.

Pick by what the client supports:

| Surface | Use when |
|---|---|
| **The packaged skill** | The client takes markdown rules, skills or custom instructions. Nothing to run |
| **The MCP server** | The client speaks MCP and you want native tool and resource primitives, multi-connection routing, and a token-budgeted context tool |

Both read the same files on disk. Neither opens a database connection.

## The markdown skill

The repository carries a skill that tells an agent where a print's files live, which to open for a given question, and to follow the print's own `reading.md` for how to interpret what it finds. It is one file, and installing it is copying that file into wherever the client keeps its rules:

| Client | Where |
|---|---|
| Claude Code | `.claude/skills/dbprint.md` in the project, or `~/.claude/skills/` for every project |
| Cursor | the project's `.cursor/rules/` directory, or the global rules under Settings |
| Cline | Custom Instructions in settings |
| Anything else | wherever that client reads persistent instructions from |

The skill itself and its install notes are published here: [installing it](../examples/skill/README.md) and [the skill itself](../examples/skill/dbprint.md).

## The MCP server

```console
$ pip install 'dbprint[mcp]'
```

The extra pulls in the MCP SDK; without it `dbprint serve` exits with an install hint rather than attempting a handshake. No adapter extra is needed — the server is read-only over committed prints and opens no database connection.

Most clients take a JSON block naming the command:

```json
{
  "mcpServers": {
    "dbprint": {
      "command": "dbprint",
      "args": ["serve", "--project", "/path/to/your/repo"]
    }
  }
}
```

`--project` is the part worth getting right. Without it the project resolves from the working directory, and the working directory of an editor-launched server is whatever the client happened to start it in. Naming the project explicitly makes the server independent of that. It accepts a directory whose direct child is `.dbprint.yaml`, that file itself, or a git address — so a print committed to another repository can be served without cloning it by hand.

For a local socket instead of stdio, `--transport http --port 8765`. The bind address is loopback and cannot be widened.

### What the server exposes

Six tools:

| Tool | Answers |
|---|---|
| `get_table_context` | everything known about one table, as an assembled fragment, inside a token budget |
| `list_tables` | what is in this print |
| `search_columns` | which columns match a name or a shape |
| `get_manifest` | the index, its freshness thresholds and its provenance |
| `get_diff` | what changed at the last generate |
| `get_reference` | the format specification, served from the package |

Plus every artifact as a readable resource under `dbprint://<connection>/...`, so a client that prefers resources over tools gets the raw YAML.

`get_table_context` is the one to reach for first: it assembles DDL, description, annotations and per-column notes into a single fragment and trims to a budget, rather than making the agent stitch four files together.

The full URI scheme, every tool signature, and the multi-connection rules are in the [MCP server specification](../MCP.md).

### Connections, when there is more than one

The server resolves what it serves at startup: a single connection is served without being named, and so is every connection marked `auto: true`. With two or more served and no default, a tool call that omits `conn` returns an error rather than guessing. Passing a name — `dbprint serve warehouse` — makes that one the default.

## Without either

`dbprint context` writes the same assembled fragment to stdout, which is enough for a client that takes pasted text or a pipeline that builds a prompt:

```console
$ dbprint context seedbank.accession
$ dbprint context 'seedbank.*' --budget 4000
```

When the fragment is over budget, dropping a whole section usually beats letting `--budget` truncate, because you choose what goes:

| Flag | Drops |
|---|---|
| `--no-ddl` | the `CREATE TABLE` — the largest section on a wide table, and the one an agent reading migrations already has |
| `--no-stats` | every per-column measurement, leaving structure and prose |
| `--no-relationships` | the foreign keys, declared and inferred |
| `--no-annotations` | human-written notes and claims |
| `--no-description` | the table's `description.md` |

`--all` covers every table in the manifest instead of a pattern, and `--output FILE` writes to a file rather than stdout, for a pipeline assembling a prompt. `--format json` and `--format yaml` give structured output instead of Markdown; both omit each column's sketch payload, which no prompt has a use for.

Note that connection-level notes from `manifest.annotations.yaml` ride the document header, which only a render covering two or more tables has. A fact a reader of one table needs belongs in that table's own annotation file — see [annotating a print](annotations.md).

## What to expect the agent to get wrong

Two things are worth stating in your own rules file, because they are the misreadings that produce confident wrong answers:

- **A sampled table's ratios are denominated in `rows_scanned`, not in `row_count`.** A `null_rate` under a `scope` block describes the sample. [Choosing what to profile](scoping.md) covers the block; the print's own `reading.md` says the same thing to whoever opens it.
- **An absent field is not a zero.** The format distinguishes "measured and absent" from "never measured", and [SPEC 7](../format/v1/SPEC.md#7-reading-an-absence) is written from the reader's side specifically for this.
