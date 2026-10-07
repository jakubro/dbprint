---
name: dbprint
description: Read the committed dbprint print of a database before touching the live database. Use for any fact about a database's tables or data — whether a table exists, whether it is empty, how many rows it holds, which column carries a fact, types, enum values, null rates, keys, join paths — before running a query through a database CLI or a database MCP server, and before trusting any claim about what data already exists. Applies wherever a .dbprint.yaml or a prints/ directory is present.
---

# Reading a database from its dbprint print

**Before any live database call, read the print.** `prints/<connection>/` carries DDL, row counts, per-column statistics, relationships and hand-authored descriptions for the printed tables. It is offline — no connection, no credentials, no database time.

A question is a database question whenever its answer lives in the database, however it is phrased and whether or not SQL is involved: does this table exist, is it empty, how many rows does it hold, are there old records this change has to stay compatible with, which column holds this fact, what values does this status take. Each of those is one tool call against the print.

The tools below are the print's MCP surface. Where they are not connected, `dbprint context <table>` renders the same material in a shell from anywhere at or below the directory holding `.dbprint.yaml`, and `pip install dbprint` supplies the command where it is missing. A server that serves several connections and declares no default needs `connection` on every call; the error it returns otherwise lists the configured names.

| Question | Tool |
|---|---|
| Does a table exist, how many rows it holds, whether it is empty | `list_tables` with `detail: true` and a `pattern` (each table's `row_count`, `profiled_at` and freshness) |
| Which table holds a fact | `search_columns` (start here), `list_tables` |
| Columns, types, nullability, DDL | `get_table_context` |
| Enum values, null rates, ranges, cardinality, key detection | `get_table_context` (Cardinality table) |
| What identifies a row | `get_table_context` (`Grain:` — declared keys at any arity, plus a measured search) |
| The filters and de-duplication a correct query needs | `get_table_context` (description + annotations) |
| Foreign keys and join paths, each stating how it was found | `get_table_context` (Relationships; the Joins list under `purpose: query`) |
| Columns found by shape rather than by name — contact data, uuid/email/phone, candidate keys | `search_columns` filters: `sensitivity`, `looks_like`, `candidate_key`, `classification`, `sql_type`, `redacted` |
| Columns found by what their notes say they hold | `search_columns` with `text` (a substring of the column name, its note or its per-value notes) |
| How a phrase in the question is actually spelled in a column | `resolve_value` (every stored spelling of it — a column holding several needs all of them in the predicate — a value whose note defines the phrase, or the nearest listed values) |
| Which statistics moved on the last run, and so which numbers are stable | `get_diff` |
| Whether the print's statistics are stale | `list_tables` with `detail: true` (each table's `freshness`: `live`, `stale` or `dormant`); in a shell, `dbprint check --max-age 7d` (offline; one unit, `Nd`/`Nh`/`Nm`/`Ns`, never `1d12h`; exit 2 = stale) |
| What a field in the print actually means | `get_reference` (`document: guide` for how to read a json/yaml field), and the `dbprint://<connection>/reading` resource |

**A reply may be one page of several.** A `list_tables` or `search_columns` reply, like every tool's, carries `next_cursor` while more remains: pass it back as `cursor`, with the same arguments, until a reply carries none — a table or column missing from the first page is not missing from the print.

**Go live only for what the print cannot answer:** an exact row (it publishes none — value lists are frequencies, not rows), a live aggregate, anything newer than the table's `profiled_at`, and a table it does not cover (absent from `list_tables`, or listed under `failed_tables`). A question that lies partly outside the print still starts in it — read what it covers, query the rest, and say which part of the answer came from where.

**Before writing SQL, read the table under `purpose: query`.** `get_table_context` with `purpose: query`, or `dbprint context <table> --purpose query` in a shell, returns the DDL, the Joins list, the data dictionary, and the value lists with their counts and coverage — what a predicate is written from. Every other row above lands in the default `profile` purpose, which carries the statistics that describe the data rather than what a query needs. Reach for `resolve_value` where a filter needs a phrase no list renders in full; where the list is already complete, the call costs a turn and adds nothing.

Resources sit alongside the tools and carry what the tools omit: `dbprint://<connection>/reading` (the traps guide), `.../manifest_annotations` (connection-wide facts — never visible through `get_table_context`, which renders one table and drops the connection header), and per-table `.../statistics`, the only place a column's sketch payload is reachable.

**Three things decide whether a number taken from the print is right**, and the number alone shows none of them:

- **Population.** A `statistics.yaml` carrying a `scope` block was not read whole. Read which cause it names: `sample` (a fraction, to bound cost) or `filter` (a row predicate, verbatim) — never both. Every field in the file except `row_count` is measured over `rows_scanned`. Under `sample`, a count may be scaled to table grain by multiplying by `row_count / rows_scanned`; under `filter` nothing rescales, because the scanned set was chosen, not drawn. A ratio, a percentile, a bound, a mean and `sum` never rescale at all. Where `row_count_method` is `approximate`, the ratio is itself an estimate.
- **Inference.** Everything under `inferred` is dbprint's guess, not the catalog's assertion — `candidate_key`, `looks_like`, `sensitivity`, `epoch_unit`. Relationships record the same distinction in `detection`: only `detection: declared` comes from the catalog, `inferred` is proposed from naming, `measured` from value containment. An absent `sensitivity` is not a statement that a column is safe.
- **Absence.** An absent field has a cause, and the causes are enumerated per field — fetch them with `get_reference` (`spec`, sections 7.2 and 7.3) rather than guessing. Three cases differ: `unmeasured` is the only marker meaning a run attempted a measurement and failed; some absences are assertions, not gaps (no `scope` means every row was read, no `null_patterns` block means no column carries a null unless the file's own `unmeasured` names that block, no `redacted` marker means no redaction rule matched); and the rest are forbidden-for-this-classification, readable off `classification` and `sql_type`.

`values_coverage: 1.0` means an exact-match predicate is complete over what was scanned; anything less is a frequent-value sample, and a value's absence from that list is not its absence from the column. A text column whose `looks_like` is `prose` publishes no value list at all — that is a rule, not a truncation.

**Freshness is not automatic.** The print is regenerated by hand (`dbprint generate`), and every field in it — DDL, row counts and relationships included — is as of that run's `profiled_at`, not as of now. Check it before relying on any of them: `list_tables` with `detail: true` and a pattern gives each table's `profiled_at` and its `freshness` verdict against the threshold it was profiled under, which is cheaper than `get_manifest`'s whole index. Schema and statistics go stale at different rates, and neither is marked stale by itself; in a shell, `dbprint check` gives the same verdict, and `dbprint check --max-age` judges every table against the one threshold it is given.
