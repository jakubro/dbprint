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

### What every agent reads on connect

The server states three things unprompted, as its own MCP `instructions` on every handshake:

> Reads committed dbprint prints - a database's structure and per-column statistics, captured offline. Three things decide whether an answer drawn from them is right.
>
> **Scope.** A column carrying a population marker was sampled - a count describes the rows that were scanned, not always the whole table, and MAY be scaled up to a rough table-wide figure by multiplying it by `row_count / rows_scanned`. A ratio, a bound, a percentile, or an aggregate like `sum` or `mean` is not: none of them scales with population size the way a count does, and rescaling one assumes the sample is representative, which the artifact never asserts.
>
> **Inference.** Everything under `inferred` is the producer's guess, not the database's assertion - `candidate_key`, `looks_like`, `sensitivity`, and any relationship marked `detection: inferred`. `looks_like` publishes the `sampled`/`matched` evidence it rests on; `candidate_key`'s own verdict is recomputable from `cardinality_ratio`; `sensitivity` publishes no evidence at all, and its absence never means safe to publish.
>
> **Absence.** A missing field means the producer did not or could not measure it - never that the value is zero, none, or safe to assume.
>
> Start from search_columns to locate a fact across the print; the reading guide resource covers the rest.

**Get the rescaling direction right — it inverted between releases.** A count on a column carrying a population marker scales to table grain by `count * (row_count / rows_scanned)`. A ratio, a bound, a percentile or an aggregate is not scalable at all, under any formula. An earlier release stated the reciprocal ratio and also licensed rescaling ratios the current server forbids — both numerically plausible, both wrong for the current server, and not something reading alone will catch, since the two ratios are reciprocals of each other. If your own rules file or prompt still says `rows_scanned / row_count`, or that ratios may be rescaled, it predates this and needs updating.

The inference paragraph distinguishes three cases rather than treating every `inferred` field alike: `looks_like` publishes the evidence it rests on, `candidate_key` is independently recomputable, and `sensitivity` publishes no evidence at all — an agent that has learned to trust `looks_like`'s published evidence should not extend the same trust to a `sensitivity` flag with nothing behind it.

### Tools — six

| Tool | Answers |
|---|---|
| `get_table_context` | everything known about one table, as an assembled fragment, inside a token budget |
| `list_tables` | what is in this print |
| `search_columns` | which columns match a name or a shape |
| `get_manifest` | the index, its freshness thresholds and its provenance |
| `get_diff` | what changed at the last generate |
| `get_reference` | the format specification, served from the package |

`get_table_context` is the one to reach for first: it assembles DDL, description, annotations and per-column notes into a single fragment and trims to a budget by dropping whole sections in priority order, never truncating mid-section, rather than making the agent stitch four files together. `search_columns` is the advertised entry point for a broader question — a name glob plus `classification`/`sql_type`/`sensitivity`/`looks_like`/`redacted` filters and a `candidate_key` match, six predicates ANDed, with `rows_scanned` and `row_count` both returned on a scoped match so a caller can tell a sampled number from a table-wide one without a second call.

A tool call never surfaces a bare protocol error: a fault comes back as a normal result with `is_error: true` and a readable message, so a client does not need special-case handling to show the agent what went wrong.

The full URI scheme, every tool signature, and the multi-connection rules are in the [MCP server specification](../MCP.md).

### Resources — two shapes

Most artifacts are per-connection, at `dbprint://<connection>/...`, so a client that prefers resources over tools gets the raw YAML. Two resources are the exception: the format specification and the assertion grammar live at `dbprint:///reference/spec` and `dbprint:///reference/assertions` — an empty-authority URI carrying no connection at all, listed once for the whole server rather than once per connection, since neither document is connection-specific.

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

Three things are worth stating in your own rules file, because they are the misreadings that produce confident wrong answers:

- **A sampled table's ratios are denominated in `rows_scanned`, not in `row_count`.** A `null_rate` under a `scope` block describes the sample. [Choosing what to profile](scoping.md) covers the block; the print's own `reading.md` says the same thing to whoever opens it.
- **An absent field is not a zero.** The format distinguishes "measured and absent" from "never measured", and [SPEC 7](../format/v1/SPEC.md#7-reading-an-absence) is written from the reader's side specifically for this.
- **Only a count rescales to table grain, and only by multiplying.** See "Get the rescaling direction right" above — this is the one an agent trained on an older release is most likely to get backwards.
