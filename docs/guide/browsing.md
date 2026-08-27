# Browsing a print

A print is plain text, and reading it in an editor works. But a print of any size is a lot of YAML, and the questions people actually arrive with — what is in this table, what joins to it, which columns are mostly null — are faster to answer by clicking than by scrolling.

`dbprint docs` renders a committed print as a small site: one page per table, one per schema, and an index. It needs no database, because everything it shows is already on disk.

```console
$ pip install 'dbprint[docs]'
$ dbprint docs serve
```

That binds `127.0.0.1:8765` and serves the connections the project resolves — the same resolution `generate` uses, so a bare invocation works wherever a bare `generate` does. `--host` accepts loopback addresses only; there is nothing to authenticate because there is nothing to reach.

## What the pages show

| Page | Holds |
|---|---|
| Index | every connection served, its schemas, and when each print was taken |
| Schema | the tables in it, with row counts and column counts |
| Table | the full per-column surface, its relationships, and a diagram of the edges into and out of it |

The table page is the one worth knowing. It carries what `statistics.yaml` carries — classification, null rate, cardinality, value lists, ranges, percentiles — beside the prose from that table's `description.md` and `statistics.annotations.yaml`, so a measurement and the note correcting it appear together rather than in two files.

The relationship diagram is rendered from the print's own edges, and marks which are declared foreign keys and which dbprint inferred from naming. An inferred edge is a guess; the diagram says so rather than drawing it like a constraint.

`serve` re-reads every artifact on each request, so a page reflects the last `dbprint generate` without restarting anything.

## Publishing it

To hand the site to people who will not install dbprint, build it as static files:

```console
$ dbprint docs build --output dbprint-docs
Wrote 6 pages to dbprint-docs
```

The output is self-contained — no CDN, no network at render time — so it works from a file share, an artifact store, or any static host.

`build` writes a `.dbprint-docs` marker into the directory it creates, and refuses to recreate a directory that does not carry one unless you pass `--force`. That is the guard against pointing `--output` at a directory holding something else. Add the output directory to `.gitignore`: it is derived from the print, and the print is what you commit.

## When to reach for this rather than the alternatives

Three surfaces read the same print, and they answer to different readers.

| Use | When |
|---|---|
| `dbprint docs` | a human is exploring, or onboarding, and does not know what they are looking for yet |
| `dbprint context` | a human or a pipeline needs a text fragment to paste into a prompt |
| the MCP server | an agent should fetch what it needs on its own — see [giving a print to an agent](agents.md) |

The site is the only one of the three built for browsing. The other two produce text for something else to consume.
