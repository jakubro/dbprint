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

Three banners can appear above everything else, and they are deliberately distinct because absence and corruption read differently: `Missing: <kinds>` names a declared artifact that isn't on disk; `Unreadable: <kinds>` names one that is on disk but failed to parse; a catalog-only notice explains that no query was issued at all, so cardinality is not measured on this page.

### Timeline and Depends on

Two sections in the overview panel read the newer per-table fields directly:

- **Timeline** — heading `Timeline - <column> by <unit>`, the anchor column linked to its own row. The hedge states the bucketed share as a floored percentage (never rounded, so a coverage under 1.0 can never read `100.0%`) and reads `A missing bucket is a gap - no rows fell in that <unit>`, so an absent row between two present ones is a real gap, not a rendering skip. The block only appears when the file carries `timeline` at all — a scoped table or an empty one never shows it, since neither computed one.
- **Depends on** — hedged `Catalog-derived, direct dependencies only - a different relation than a foreign key`. Three states, and the page tells them apart: a non-empty list renders links to each object; an empty list renders `Reads nothing else printed`; the field omitted entirely (the producer could not ask) renders no section at all. The same names double as diagram edges, drawn with an open-circle head and a muted dashed style so a dependency edge can never be mistaken for a foreign key even without color.

### The column table's degenerate census

Beside the null-rate bar, a column carrying `zero_count`, `negative_count` or `empty_count` shows a dot-separated line — `<n> zero`, `<n> negative`, `<n> empty` — under the null figure. Each sub-count is gated on being truthy, so an explicit `0` renders nothing there, reading the same as a field the classification forbids or the run never measured. If you need to tell those apart, read `statistics.yaml` directly for that column.

Further down the same cell: `mean`/`sum` render as `mean X · sum Y` whenever either is present, independent of whether the column also carries a redacted range — a redacted `numeric` column can show `bounds withheld` and its mean and sum together, because redaction is cell-level and aggregates are governed by their own rule (mean, sum and length are withheld only where the scanned set holds at most one non-null value; above that they are never touched). A `length` block renders as `length <min>-<max> · avg <avg> · p95 <p95>`.

### The relationships panel

Every edge gets a Detection badge with one of three verdicts: `declared` (a real foreign key), `measured` (proposed from two columns' own sketches, never a schema fact), or `inferred` (a naming guess) — an edge carrying no `detection` at all reads as the weakest of the three, `inferred`, never defaulted upward. `declared` and `measured` are two different claims and the page never blurs them: a declared edge is a constraint the database enforces; a measured one is evidence the data happens to join cleanly today.

Detail beside the badge carries whatever the edge can state — `on_delete`, a constraint name, the annotation path for a human-added edge — and, where the observed join was measured, its fanout, target coverage and containment, or `scopes not comparable` when the two sides' scopes can't be compared at all. A verdict a human annotation rejected renders `REJECTED by human annotation` on both the referencing and the referenced side.

## What a `catalog_only` table shows

No query was issued, so the columns carry only catalog facts:

- The catalog-only banner renders at the top of the page.
- The related, data-through, sensitive, redacted, cardinality and completeness cards all vanish together — every one of them needs a query this table never ran.
- The rows card still shows the catalog's own row count, with no scanned-share line.
- The Overview still shows Description, Grain, Timeline, Depends on, Physical layout and Dependencies wherever the statistics file happens to carry them — a plain view is exactly where `depends_on` is expected to be present.
- The columns table still renders from the catalog-declared column list, with an empty null-rate figure and `n/a` cardinality on every row.

## When a route fails to build

`dbprint docs build` writes every page it can and collects the routes it could not into a `failed: <route>` line on stderr, then exits `1`. Each page is written the moment it renders, before the next route is attempted, and the static assets and marker file go down last, after every route has been tried — so a partial build still leaves every page that did render usable — a failure narrows what's on disk, it does not corrupt what's there.

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
