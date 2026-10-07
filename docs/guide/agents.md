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

The server states which tool answers which question, then how to read an answer, as its own MCP `instructions` on every handshake:

> Serves committed dbprint prints: a database's structure and per-column statistics, captured offline. Answer from these tools, not from the print's files: a file read directly carries none of the scope, redaction and unmeasured handling the tools apply.
>
> Which tool answers what:
> - Writing SQL: get_table_context with purpose: query for every table the query touches - DDL, the Joins list, the data dictionary, value lists.
> - A filter value whose stored spelling is not listed in full there: resolve_value; use every spelling it returns.
> - Which table or column holds a fact, by name, type, shape (email, phone) or what its notes say: search_columns (`text` searches the notes).
> - Which tables exist, their row counts, whether their statistics are stale: list_tables with detail: true.
> - What changed since the previous run: get_diff.
> - What a field or a finding's spec_ref means: get_reference; how to read json/yaml fields: get_reference document: guide.
> - The raw manifest index: get_manifest.
>
> Reading an answer:
> - Scope. A table with a scope block was read in part, by a sample or a row filter; its statistics describe the rows scanned, not the table. Under a sample a row count - nulls, a value's count, a true/false split - MAY be multiplied by row_count / rows_scanned for a rough table-wide figure; under a filter nothing rescales, and a distinct count, ratio, bound, percentile, sum or mean never does.
> - Inference. Fields under inferred, and relationships whose detection is inferred or measured, are dbprint's guesses, not database constraints. No sensitivity on a column means nothing was detected, not that the column is safe to publish.
> - Absence. A missing field is not zero. A field named in an unmeasured list was not measured this run: its value is unknown, not none.

**Get the rescaling direction right.** Under a sample, a row count — nulls, a value's count, a true/false split — on a column carrying a population marker scales to table grain by `count * (row_count / rows_scanned)`; under a filter nothing rescales. A distinct count, a ratio, a bound, a percentile or an aggregate is not scalable at all, under any formula.

`looks_like` publishes the `sampled`/`matched` draw it rests on and `candidate_key` is recomputable from `cardinality_ratio`; `sensitivity` publishes no evidence at all — an agent that has learned to trust `looks_like`'s published evidence should not extend the same trust to a `sensitivity` flag with nothing behind it.

### Tools — seven

| Tool | Answers |
|---|---|
| `get_table_context` | everything known about one table, as an assembled fragment, inside a token budget |
| `list_tables` | what is in this print, and under `failed_tables` what the last run could not profile — a table listed there exists and is unprofiled; with `detail: true`, each table's row count and whether its statistics are stale |
| `search_columns` | which columns match a name, a shape, or what their notes say |
| `resolve_value` | how a phrase from a question is spelled in one column's values |
| `get_manifest` | the index, its freshness thresholds and its provenance |
| `get_diff` | what changed at the last generate |
| `get_reference` | the format specification, served from the package |

`get_table_context` is the one to reach for first: it assembles DDL, description, annotations and per-column notes into a single fragment and trims to a budget by dropping whole sections in priority order, never truncating mid-section, rather than making the agent stitch four files together. `search_columns` is the first call for a broader question — a name glob, a `text` search over column notes, `classification`/`sql_type`/`sensitivity`/`looks_like`/`redacted` filters and a `candidate_key` match, all ANDed, with `rows_scanned` and `row_count` both returned on a scoped match so a caller can tell a sampled number from a table-wide one without a second call.

A tool call never surfaces a bare protocol error: a fault comes back as a normal result with `is_error: true` and a readable message, so a client does not need special-case handling to show the agent what went wrong.

The full URI scheme, every tool signature, and the multi-connection rules are in the [MCP server specification](../MCP.md).

### Resources — two shapes

Most artifacts are per-connection, at `dbprint://<connection>/...`, so a client that prefers resources over tools gets the raw YAML. Two resources are the exception: the format specification and the assertion grammar live at `dbprint:///reference/spec` and `dbprint:///reference/assertions` — an empty-authority URI carrying no connection at all, listed once for the whole server rather than once per connection, since neither document is connection-specific.

### Connections, when there is more than one

The server resolves what it serves at startup: a single connection is served without being named, and so is every connection marked `auto: true`. With two or more served and no default, a tool call that omits `connection` returns an error rather than guessing. Passing a name — `dbprint serve warehouse` — makes that one the default.

## Without either

`dbprint context` writes the same assembled fragment to stdout, which is enough for a client that takes pasted text or a pipeline that builds a prompt:

```console
$ dbprint context arboretum.seedbank.accession
$ dbprint context 'arboretum.seedbank.*' --budget 4000
```

### What the fragment is for

`--purpose` selects the fragment, and the choice is between describing the data and querying it:

| `--purpose` | Sections | Read it when |
|---|---|---|
| `profile` (default) | Header, Terms, DDL, Description, Annotations, Cardinality table, Relationships | You want to know what is in the table and how much of it was measured |
| `query` | Header, Terms, DDL, Joins, Data dictionary, Column values | You are about to write SQL against the table |

`query` drops every statistic but each nullable column's null share — the cue that a predicate needs an `IS NULL` arm — and renders instead what a query writer needs a literal from: the columns whose value list a predicate can be written from, each with its counts, and a coverage cell saying whether that list is the column's whole domain or, as a percentage, the share of it the five most frequent values cover. A value with a note in `statistics.annotations.yaml` carries it inline, so what a code means sits beside the code itself. Every value and number is spelled so it reads back as itself — `NULL` is a genuine null, `'NULL'` the stored string; the [MCP server specification](../MCP.md#41-get_table_context) states the full rule. The join paths are the `## Joins` list: the DDL's foreign keys and the edges the print inferred or measured, each with its detection, so a table whose catalog declares no key still says what it joins to.

When the fragment is over budget, dropping a whole section usually beats letting `--budget` truncate, because you choose what goes:

| Flag | Drops |
|---|---|
| `--no-ddl` | the `CREATE TABLE` — the largest section on a wide table, and the one an agent reading migrations already has |
| `--no-stats` | every per-column measurement, leaving structure and prose |
| `--no-relationships` | the foreign keys, declared and inferred — the Relationships section, or the Joins list under `--purpose query` |
| `--no-annotations` | human-written notes and claims |
| `--no-description` | the table's `description.md` |

`--all` covers every table in the manifest instead of a pattern, and `--output FILE` writes to a file rather than stdout, for a pipeline assembling a prompt. `--format json` and `--format yaml` give structured output instead of Markdown; both omit each column's sketch payload, which no prompt has a use for.

Note that connection-level notes from `manifest.annotations.yaml` ride the document header, which only a render covering two or more tables has. A fact a reader of one table needs belongs in that table's own annotation file — see [annotating a print](annotations.md).

## What to expect the agent to get wrong

Three things are worth stating in your own rules file, because they are the misreadings that produce confident wrong answers:

- **A sampled table's ratios are denominated in `rows_scanned`, not in `row_count`.** A `null_rate` under a `scope` block describes the sample. [Choosing what to profile](scoping.md) covers the block; the print's own `reading.md` says the same thing to whoever opens it.
- **An absent field is not a zero.** The format distinguishes "measured and absent" from "never measured", and [SPEC 7](../format/v1/SPEC.md#7-reading-an-absence) is written from the reader's side specifically for this.
- **Only a row count rescales to table grain, and only by multiplying.** See "Get the rescaling direction right" above — the two ratios are reciprocals, so an agent that reaches for the wrong one gets a plausible number rather than an obvious error.
