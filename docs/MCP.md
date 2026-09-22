# dbprint MCP server — v1

User-facing specification for dbprint's Model Context Protocol (MCP) surface. The MCP server exposes committed prints as native AI-tool primitives — Resources for the per-table artifacts, Tools for computed and search operations. Any MCP-aware client (editor, agent runtime, custom integration) can consume dbprint output through this protocol.

Any tool that implements this surface MUST comply with the requirements below. Conformance MUST be mechanically verifiable against the resource enumeration, tool signatures, and error model.

---

## 0. Scope and terminology

### 0.1 What this spec covers

- Server identity advertised at MCP handshake.
- The Resource URI scheme and per-artifact resource enumeration.
- Tool signatures, input/output schemas, and return shapes.
- Multi-connection model and the default-connection rules for tool calls.
- Transport options (stdio default, HTTP loopback).
- Lifecycle behaviors (file freshness, shutdown, capability flags).
- Error model mapped to JSON-RPC standard codes.

### 0.2 What this spec does NOT cover

- The implementation of `dbprint serve` (the CLI wrapper and SDK integration).
- The format of `manifest.yaml` / `diff.yaml` / per-table artifacts — covered by [`format/v1/SPEC.md`](format/v1/SPEC.md).
- Authentication, authorization, or remote deployment models.
- Write operations via MCP.
- The MCP Prompts primitive.
- Resource subscriptions or live-change notifications.

### 0.3 Terminology

- **MCP**: Model Context Protocol — the open protocol clients use to discover and invoke tools and resources from servers.
- **Resource**: an MCP primitive representing static-data-by-URI; enumerable via `resources/list`, readable via `resources/read`.
- **Tool**: an MCP primitive representing a parameterized operation; invocable via `tools/call`.
- **Transport**: the wire protocol carrying MCP frames (stdio or HTTP / SSE).
- **Capabilities**: the feature flags a server advertises at handshake.
- **Connection**: a dbprint connection name as configured in `.dbprint.yaml`. A single server may serve one or many connections.
- **Default connection**: the connection a tool call falls back to when its `conn?` parameter is omitted.

### 0.4 Requirement levels

This document uses **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** per RFC 2119.

---

## 1. Distribution

The MCP server is gated on the `[mcp]` install extra:

```sh
pip install "dbprint[mcp]"
```

The extra adds a dependency on the official Anthropic Python `mcp` SDK. Without the extra, `dbprint serve` MUST exit with code 1 and a clear install hint at command invocation time; no MCP handshake is attempted.

The server is read-only against committed prints — no database connection is opened. No adapter extra is required for `dbprint serve`.

---

## 2. Server identity and handshake

At MCP handshake the server MUST advertise:

```json
{
  "serverInfo": {
    "name": "dbprint",
    "version": "<package version>"
  },
  "instructions": "Reads committed dbprint prints ... (see dbprint.mcp.server.SERVER_DESCRIPTION for the full text)",
  "capabilities": {
    "resources": { "subscribe": false, "listChanged": false },
    "tools": { "listChanged": false }
  }
}
```

MCP's `InitializeResult` carries no `description` field - `serverInfo` is name/version only, and free-text server description travels as the top-level `instructions` string instead. `instructions` is delivered unprompted on every connect, so it carries the floor a consumer needs before any tool call: which population a number describes (`scope`), that `inferred` fields are guesses rendered like measurements, and that an absent field was never measured rather than safe to assume zero. It then states the two rules for the task most agents open the server for - read `get_table_context` with `purpose: query` before writing SQL and join through its Joins list, which carries the edges the catalog never declared beside the DDL's own keys, and call `resolve_value` before writing a literal the context does not list in full - and points at `search_columns` as the entry point and the `reading` resource for the rest. The shipped reading guide states the same two facts in the print's own files and fields, for a reader holding the print and no server. Some clients ignore `instructions` entirely, so every tool description below stands alone rather than depending on it having been read.

`version` MUST equal the installed dbprint package version (`importlib.metadata.version("dbprint")`).

Capability values are normative:

| Capability | Value | Meaning |
|---|---|---|
| `resources.subscribe` | `false` | No per-URI subscription support |
| `resources.listChanged` | `false` | No notifications when the resource set changes |
| `tools.listChanged` | `false` | No notifications when the tool set changes |

The server MUST NOT advertise `prompts` or `sampling` capabilities.

---

## 3. Resources

### 3.1 URI scheme

Every resource URI is of the form:

```
dbprint://<connection>/<rest>
```

where `<connection>` is a connection name from `.dbprint.yaml` and `<rest>` identifies the artifact. The URI scheme `dbprint://` is reserved for this server.

A second form carries an empty authority:

```
dbprint:///reference/<document>
```

This addresses a server-global resource that belongs to no connection - §3.2's two reference documents. An empty authority MUST NOT be read as a zero-length connection name: a connection name is never empty in practice, and this form is checked ahead of any connection-scoped one, so it can never collide with one. Do not "simplify" it into a reserved connection name (e.g. a connection literally named `reference`) - the empty authority is what guarantees no collision is possible, a reserved name would not.

### 3.2 Per-artifact resource list

The server MUST expose the following resources for every served connection:

| URI pattern | mimeType | Source file |
|---|---|---|
| `dbprint://<conn>/manifest` | `application/yaml` | `prints/<conn>/manifest.yaml` |
| `dbprint://<conn>/diff` | `application/yaml` | `prints/<conn>/diff.yaml` |
| `dbprint://<conn>/reading` | `text/markdown` | `prints/<conn>/reading.md` |
| `dbprint://<conn>/manifest_annotations` | `application/yaml` | `prints/<conn>/manifest.annotations.yaml` (present only when authored) |
| `dbprint://<conn>/<fqn>/ddl` | `application/sql` | per-table `ddl.sql` |
| `dbprint://<conn>/<fqn>/statistics` | `application/yaml` | per-table `statistics.yaml` |
| `dbprint://<conn>/<fqn>/relationships` | `application/yaml` | per-table `relationships.yaml` |
| `dbprint://<conn>/<fqn>/description` | `text/markdown` | per-table `description.md` (present only when authored) |
| `dbprint://<conn>/<fqn>/statistics_annotations` | `application/yaml` | per-table `statistics.annotations.yaml` (present only when authored) |
| `dbprint://<conn>/<fqn>/relationships_annotations` | `application/yaml` | per-table `relationships.annotations.yaml` (present only when authored) |

`<fqn>` is the dotted fully-qualified name (`arboretum.seedbank.accession`), NOT the slash-delimited filesystem path. Agents already know FQNs from prompts; the URI keeps that mental model intact.

The server MUST also expose these two server-global resources, one per document, regardless of which or how many connections are served:

| URI pattern | mimeType | Source |
|---|---|---|
| `dbprint:///reference/spec` | `text/markdown` | the packaged format specification, whole |
| `dbprint:///reference/assertions` | `text/markdown` | the packaged assertion DSL specification, whole |

§4.6's `get_reference` tool addresses a section of either document directly; these two resources exist for browsing the whole thing.

### 3.3 Enumeration

`resources/list` MUST enumerate every resource for every served connection:

- `manifest`, `diff` and `reading` resources for each connection (exactly 3 per connection).
- `manifest_annotations` is listed ONLY when `manifest.annotations.yaml` is present at the connection root (§2.7.3 of the format spec).
- Per-table resources for every table in each connection's `manifest.yaml` `tables` map.
- `description`, `statistics_annotations` and `relationships_annotations` resources are listed ONLY when the corresponding per-table file (`description.md` / `statistics.annotations.yaml` / `relationships.annotations.yaml`) is present.
- The two reference resources are listed exactly once each - server-global, never once per connection, and present even when the resolved connection set is empty.

Resource entries returned by `resources/list` include `uri`, `name` (human-readable label), `description` (one-line summary), and `mimeType`. Ordering MUST be deterministic: the two reference resources first (`spec` then `assertions`), then by connection name, then by FQN, then by artifact name (`ddl` -> `statistics` -> `relationships` -> `description` -> `statistics_annotations` -> `relationships_annotations`).

### 3.4 Reading

`resources/read` returns the file content with the matching mimeType. Behaviors:

- The server MUST re-read the file from disk on every call (see §6.1 for the freshness contract).
- Reading a missing `description.md` or `statistics.annotations.yaml` MUST return JSON-RPC error `-32602 InvalidParams` with detail explaining the resource is optional and not authored for this table.
- Reading a manifest-listed artifact whose file is missing from disk MUST return JSON-RPC error `-32603 InternalError` with detail recommending `dbprint generate`.
- YAML parse failures MUST surface as `-32603 InternalError` with the parser's error message and the affected file path.

---

## 4. Tools

### 4.1 `get_table_context`

Returns an assembled context fragment for one table, formatted for direct insertion into an LLM prompt. A budgeted call may omit sections to fit and never returns empty on success; the truncation marker in the result names what was dropped, down to the whole table when nothing fits.

`purpose` selects what the fragment is for, and is the first thing a caller decides:

| `purpose` | Sections | For |
|---|---|---|
| `profile` (default) | Header, DDL, Description, Annotations, Cardinality table, Relationships | Describing the data: what was measured and how much of it |
| `query` | Header (identity + scope), DDL, Joins, Data dictionary, Column values | Writing SQL against the table: what the columns mean, what they join to, and which literals they hold |

Under `query` the fragment carries no statistics, no null patterns and no physical layout. The join paths are the `## Joins` list: every edge in `relationships.yaml`, one line each as `<column> -> <table>.<column> (<detection>)` or `<column> <- <table>.<column> (<detection>)`, so an edge the print inferred or measured on a table whose catalog declares no key is still on the surface - and nothing measured about an edge (fan-out, coverage, referential actions) rides along. An edge a human rejected in `relationships.annotations.yaml` carries the rejection marker. The section is absent on a table with no edge.

The value table is what licenses an exact-match predicate, and it carries only the lists a predicate can be written from: a column whose `values_coverage` is `1.0` is rendered in full, one `<value> (<count>)` entry per value, with the coverage cell stating that the list is the column's whole domain; a column with a coverage below `1.0` shows its five most frequent values (a spelling group counting as one), with the coverage cell stating the share of the column those five cover and that they are a sample. A column with no `values_coverage` - `numeric` and `temporal`, whose list is a frequency sample and never a domain (SPEC 2.2.3) - has no row. A value carrying a note in `statistics.annotations.yaml` renders it inline (`<value> (<count>) = <note>`); a redacted column publishes its counts and no literal; a scoped table's exhaustive list says so over the rows scanned, never over the table.

`include_ddl`, `include_description`, `include_annotations` and `include_relationships` narrow the `query` selection the way they narrow `profile` - `include_relationships` governs the Joins list; `include_stats` has nothing to drop there.

```json
{
  "name": "get_table_context",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "table": { "type": "string", "minLength": 1, "description": "Fully-qualified table name" },
      "conn": { "type": "string", "description": "Optional; falls back to default connection" },
      "purpose": { "type": "string", "enum": ["profile", "query"], "default": "profile", "description": "profile: the table described - statistics, relationships, notes. query: what to read before writing SQL - DDL, the Joins list, data dictionary, and the value lists with counts and coverage, and nothing measured" },
      "format": { "type": "string", "enum": ["md", "json", "yaml"], "default": "md", "description": "md renders the chosen purpose as Markdown - under `profile`, a per-column Notes summary rather than the raw statistics fields json and yaml carry. All three omit each column's sketch payload; the verbatim statistics.yaml, sketch included, is reachable as the dbprint://<conn>/<fqn>/statistics resource." },
      "include_stats": { "type": "boolean", "default": true, "description": "Include the Cardinality table (md) or statistics object (json/yaml); no effect under `query`, which carries neither" },
      "include_relationships": { "type": "boolean", "default": true, "description": "Include the Relationships section (md) or relationships object (json/yaml); under `query`, the Joins list" },
      "include_description": { "type": "boolean", "default": true, "description": "Include the table's description.md, when authored" },
      "include_annotations": { "type": "boolean", "default": true, "description": "Include statistics.annotations.yaml notes and claims, when authored" },
      "budget_tokens": { "type": "integer", "minimum": 1, "description": "Soft cap in tokens, defaulting to 8000. Sections drop whole, never truncated mid-section: the table's identity is charged first, then each section in priority order is measured against what is left, so one that does not fit is skipped rather than closing the door behind it" }
    },
    "required": ["table"]
  }
}
```

Return:

- `format: "md"` -> a markdown string carrying the chosen purpose's sections.
- `format: "json"` -> a structured object. Under `profile`: `table`, `ddl`, `description`, `annotations`, `statistics`, `relationships`, `relationship_annotations`. Under `query`: `table`, `ddl`, `values` (per column: the `entries` the Markdown shows with counts and notes, the column's `coverage`, the same coverage statement the Markdown renders, and for a sampled list `shown_coverage` - the share the shown entries cover), `joins` (`refers_to` and `referenced_by`, each edge as its columns, its table and its `detection`, plus `rejected` where a human overruled it), `dictionary` (column -> note) and `description`.
- `format: "yaml"` -> the same structured object emitted as YAML.

`budget_tokens` is a soft cap. The table's identity is charged first, then each remaining section in priority order is measured against what is left, so one that does not fit is skipped rather than closing the door behind it. Sections drop whole, never truncated mid-section. Token counting MAY be approximate. Under `query` that priority is DDL, then the value table, then the Joins list, then the data dictionary - identity is not ranked among them, being pinned instead.

`format: "json"` or `"yaml"` carries a `_corrupted` field naming every declared artifact (`statistics`, `relationships`, `statistics_annotations`, `relationships_annotations`) that failed to parse, mapped to the parse-error message; absent when nothing was corrupt. `format: "md"` prepends the same information as a note before the rendered sections. A corrupt artifact still degrades that one section rather than failing the call - this field is what tells a corrupt file from one the object's type never had.

`format: "json"` or `"yaml"` also carries a `_missing` field: an array naming every artifact kind the manifest declares for this table whose file is absent from disk. `format: "md"` states the same information as a `Missing: ...` line in the table's own header, distinct from `_corrupted`'s prepended note - a declared-but-absent artifact and a declared-and-broken one are different gaps, and a caller needs to tell "nobody promised this" from "something promised is gone" from "the bytes are there but will not parse". Absent when nothing declared is missing.

### 4.2 `list_tables`

Returns the FQNs of tables matching a pattern.

```json
{
  "name": "list_tables",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "conn": { "type": "string", "description": "Optional; falls back to default connection" },
      "pattern": { "type": "string", "minLength": 1, "description": "fnmatch glob; defaults to '*'" },
      "detail": { "type": "boolean", "default": false, "description": "Project each entry's type/row_count/columns/profiled_at from the manifest; false returns bare FQN strings, unchanged" }
    }
  }
}
```

Return: `{ "tables": ["arboretum.seedbank.accession", "arboretum.seedbank.germination_trial", ...] }`. Sorted lexicographically; deterministic across calls.

**Capped at 500 entries.** A reply the cap cut carries `truncated: true` and the `total` it was cut from; narrow with `pattern` to reach past it. A bare `truncated` would say a caller missed something without saying whether it missed ten entries or ten thousand.

`detail: true` returns `{ "tables": [{ "fqn": ..., "type": ..., "row_count": ..., "columns": ..., "profiled_at": ... }, ...] }` instead - the manifest entry's own fields, projected alongside the FQN, still sorted lexicographically by FQN. `row_count` is absent for a plain view, the same as in the manifest itself.

### 4.3 `search_columns`

The entry point for locating a fact across the print - a name glob plus optional classification/sql_type/sensitivity/looks_like/redacted glob filters and a candidate_key match, ANDed. `pattern` alone reproduces the original by-name search; any predicate may be used alone or combined with the rest. Every glob filter needs the field present to match at all - `sensitivity: "*"` finds every column carrying any detection, never a column with none. A plain view carries a catalog-only `statistics.yaml` (SPEC 2.2.15), so `pattern`, `classification` and `sql_type` reach every column its catalog read; `sensitivity`, `looks_like`, `redacted` and `candidate_key` never match a view column, since the marker forbids every field those filters test.

```json
{
  "name": "search_columns",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "pattern": { "type": "string", "minLength": 1, "description": "fnmatch glob over column names; optional - omit to filter by the other predicates alone" },
      "classification": { "type": "string", "description": "fnmatch glob against the column's classification (boolean, categorical, foreign_key_candidate, json, numeric, temporal, text, unsupported)" },
      "sql_type": { "type": "string", "description": "fnmatch glob against the column's sql_type" },
      "sensitivity": { "type": "string", "description": "fnmatch glob against inferred.sensitivity (contact, credential, date_of_birth, demographic, employment, financial_account, geolocation, health, national_id, online_identifier, personal_name, postal_address) - a glob of '*' sweeps every column carrying any detection. A detection, never a verdict; its absence on a column is not an assertion that the column is safe" },
      "looks_like": { "type": "string", "description": "fnmatch glob against inferred.looks_like (base64, bic, card_number, content_type, country_code, currency_code, ean, email, filename, hex, iban, imei, ip, isbn, iso8601_date, iso8601_datetime, iso8601_duration, json, jwt, latlon, mac_address, numeric_string, path, phone, postal_code, prose, semver, timezone, url, urn, uuid, vin)" },
      "redacted": { "type": "string", "description": "fnmatch glob against the column's redacted marker (drop, hash, mask)" },
      "candidate_key": { "type": "boolean", "description": "Exact match against inferred.candidate_key" },
      "limit": { "type": "integer", "minimum": 1, "description": "Cap on returned matches; a capped response carries `truncated: true`" },
      "conn": { "type": "string", "description": "Optional; falls back to default connection" }
    }
  }
}
```

Return:

```json
{
  "matches": [
    {
      "table_fqn": "arboretum.seedbank.accession",
      "column": "email",
      "sql_type": "VARCHAR(320)",
      "classification": "text",
      "row_count": 1000,
      "rows_scanned": 250,
      "looks_like": "email",
      "sampled": 250,
      "matched": 248,
      "sensitivity": "contact",
      "redacted": "hash",
      "candidate_key": true,
      "annotation": "Always lowercased on write."
    }
  ]
}
```

`annotation` is present only when the column has one in `statistics.annotations.yaml`; `sql_type` and `classification` are empty strings for a column known only through its annotation (a view with no `statistics.yaml`). Matches are ordered by (table FQN, column name) lexicographic.

`row_count` and `rows_scanned` are present whenever the manifest/column carry them - `rows_scanned` only when the table's file carries a `scope` block (SPEC 2.2.8), so its absence beside a present `row_count` means the column's own count already covers the whole table. `looks_like`/`sampled`/`matched` ride together, only on a column carrying a `looks_like` verdict (SPEC 4.1.3) - a verdict drawn from two values reads identically to one drawn from ten thousand without the draw size beside it. `sensitivity`, `redacted` and `candidate_key` (with `candidate_key_exception` when the ratio falls short of 1.0) ride the same way: only on a column carrying the field, so a caller filtering on any of these four sees the matched category, not just the column name.

`limit` caps the number of matches (tables are walked in FQN order, columns within a table in name order, so which matches survive a cap is deterministic); a capped response carries `truncated: true`. A glob matching no real value (e.g. `classification: "nope"`) returns an empty match list, never an error - the enumerated values in each filter's description are the format's own, not a validated allowlist.

`unreadable_tables` is present only when at least one table's `statistics.yaml` or `statistics.annotations.yaml` failed to parse, naming every such table's FQN, sorted. A `statistics.yaml` failure means that table's columns are absent from `matches` entirely, since the column list itself could not be read; a `statistics.annotations.yaml` failure alone still returns the table's columns, only without the `annotation` field an annotation would have carried. Either way a corrupt file costs what it alone was needed for, not the whole call, but the gap is never silent.

### 4.4 `get_manifest`

Returns the parsed `manifest.yaml` content as a JSON object - an index of tables and their artifacts, not a semantic catalogue of what they mean.

```json
{
  "name": "get_manifest",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "conn": { "type": "string", "description": "Optional; falls back to default connection" },
      "pattern": { "type": "string", "minLength": 1, "description": "fnmatch glob over the FQN keys of `tables`, the same spelling `list_tables` takes; filters that map only" }
    }
  }
}
```

Return: the parsed manifest dict per [SPEC §2.5](format/v1/SPEC.md#25-manifestyaml).

**The `tables` map is capped at 500 entries; every other key of the document is returned whole.** `pattern` filters that map by the same fnmatch spelling `list_tables` takes, so one spelling means one thing across the surface. A capped reply carries `truncated: true` and the `total` the cap was taken from. A reply that dropped a header key to save bytes would be a different artifact, not a truncated one.

### 4.5 `get_diff`

Returns the parsed `diff.yaml` content as a JSON object - a per-column reliability signal for which statistics are stable and which drift run to run.

```json
{
  "name": "get_diff",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "conn": { "type": "string", "description": "Optional; falls back to default connection" },
      "table": { "type": "string", "minLength": 1, "description": "Keep only changes naming this fully-qualified table, including the relationship events that name it as source or target" },
      "kind": { "type": "string", "enum": ["table_added", "table_removed", "column_added", "column_removed", "column_type_changed", "column_nullable_changed", "column_default_changed", "statistic_changed", "table_row_count_changed", "grain_changed", "physical_layout_changed", "depends_on_changed", "relationship_added", "relationship_removed", "relationship_modified", "index_added", "index_removed", "index_modified", "comment_changed"], "description": "Keep only changes of this kind" }
    }
  }
}
```

Return: the parsed diff dict per [SPEC §2.6](format/v1/SPEC.md#26-diffyaml). The diff is the one produced by the last successful `dbprint generate` for the connection.

**The `changes` list is capped at 500 events; every other key of the document is returned whole.** Two filters narrow it, ANDed: `table` keeps the events naming that fully-qualified table - including the three relationship events, which carry `source_table` and `target_table` where every other event carries `table`, so a filter reading one field alone would silently drop them - and `kind` keeps one event kind, declared as the packaged `diff.schema.json` enum so the list cannot drift from the format. A capped reply carries `truncated: true` and the `total` the cap was taken from.

**The schema guarantees none of those fields.** `$defs/Change` requires `kind` alone and no variant declares a table field, so these are producer facts: an event carrying none of the three is simply not matched by a `table` filter.

### 4.6 `get_reference`

Returns a slice of the format spec or the assertion DSL spec, addressed by section number. Depends on no connection and no print; `conn` is not a parameter.

```json
{
  "name": "get_reference",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "document": { "type": "string", "enum": ["assertions", "spec"], "description": "Which specification - the format spec, or the assertion DSL" },
      "section": { "type": "string", "minLength": 1, "description": "A section number in the document's own scheme (e.g. '3', '2.2.4'), or a spec_ref citation copied verbatim from a finding ('\u00a72.2.4', 'ASSERTIONS.md \u00a71.4') - any heading depth. Omit for the table of contents." }
    },
    "required": ["document"]
  }
}
```

Return: a markdown string.

- With `section`, the matching heading and everything up to the next heading at the same or a shallower level - one rule for every depth, `##`/`###`/`####` alike.
- Without `section`, the document's heading tree instead of the whole document.
- `section` MUST accept the document's own bare numbering scheme (`"3"`, `"2.2.4"`) AND a `spec_ref` citation copied verbatim from a finding (`"§2.2.4"`, `"ASSERTIONS.md §1.4"`, per conformance/issue.py's own convention) - a caller never strips the citation prefix by hand.
- `section` naming a number no heading in that document carries MUST fail (`isError: true`, §8.2), detail naming the section numbers that document actually has.

`document` is a closed enum of exactly two values; a third document is a decision for a future revision of this spec, not an extension point a server infers.

---

### 4.7 `resolve_value`

Resolves a phrase, a code or a spelling against one column's published values — what a caller reads before writing a literal into a filter. Offline: the column's `values` list, its counts, and any `values[].note` in `statistics.annotations.yaml`. No database, and no second artifact.

```json
{
  "name": "resolve_value",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "table": { "type": "string", "minLength": 1, "description": "Fully-qualified table name" },
      "column": { "type": "string", "minLength": 1, "description": "Column name as the print spells it" },
      "text": { "type": "string", "minLength": 1, "description": "The phrase, code or spelling to resolve" },
      "conn": { "type": "string", "description": "Optional; falls back to default connection" }
    },
    "required": ["table", "column", "text"]
  }
}
```

The reply carries `table`, `column`, `text` and a `match`, which is the first of these that applies:

| `match` | Meaning | Carries |
|---|---|---|
| `stored` | `text` is a listed value, or folds to one under the normalization key (SPEC 2.2.4: trimmed, lower-cased) | `spellings` — every listed spelling that folds to it, each with its count and its note. Several means one category stored several ways: a predicate needs all of them |
| `definition` | `text` names what a value's note says it means, matched on word boundaries either way round | `candidates` — every value whose note matches, with counts and notes |
| `nearest` | Neither, but listed values resemble `text` | `candidates` — up to five, ranked by trigram similarity above a floor, each carrying its `score` |
| `none` | Nothing resembled it | — |
| `unavailable` | The column publishes no values, or publishes them redacted | `reason` |

Every reply carries `coverage` (the column's `values_coverage`, `null` where the classification publishes none), `exhaustive` (whether the list carries every distinct value the column has - `values_coverage` of `1.0`, or on a `numeric`/`temporal` column `frequencies.listed` equal to an exact `cardinality`, SPEC 2.2.5) and `listed` (how many entries the print carries). Where the list is not exhaustive the reply also carries `sample_caveat`, a fixed sentence stating that a spelling absent from a sample is not evidence it is absent from the column. Where the list IS exhaustive and at most fifty entries long, the reply carries `domain`: the whole list with counts and notes, so a small vocabulary needs exactly one call.

A `match: "none"` is an answer, not an error. An unknown table fails per §8.2 as `get_table_context` does; an unknown column fails the same way, its detail naming the columns that table's statistics do carry. A `statistics.yaml` the manifest declares but that is absent from disk, or that does not parse, fails with the same errors §8.2 lists for those conditions - never as an unknown column; a `statistics.annotations.yaml` that does not parse fails the same way, since the notes it would carry are part of the answer. A table that declares no statistics artifact answers `match: "unavailable"` with that reason.

---

## 5. Multi-connection model

### 5.1 Default connection resolution

`dbprint serve [CONN]` resolves the served connection set and default at startup:

| Invocation | Served connections | Default for tool calls |
|---|---|---|
| `dbprint serve` with single connection in `.dbprint.yaml` | The single connection | The single connection |
| `dbprint serve` with `auto: true` set on exactly one of >= 2 connections | That connection | That connection |
| `dbprint serve` with `auto: true` set on >= 2 connections | Every `auto: true` connection | None — tool calls without `conn?` MUST return an error |
| `dbprint serve <name>` | `<name>` | `<name>` |
| `dbprint serve` with no `auto: true` and >= 2 connections | (rejected at startup) | Server exits code 1 with hint listing valid names |

### 5.2 `conn?` parameter on tool calls

Every tool accepts an optional `conn` parameter:

- When provided, the tool MUST evaluate against that connection. If the connection is not in the served set, the tool MUST return `-32602 InvalidParams` - naming it as configured-but-unserved when `.dbprint.yaml` declares it and this instance simply was not started with it, or as unconfigured when `.dbprint.yaml` does not declare it at all (§8.1/8.2).
- When omitted, the tool MUST use the default connection. When no default exists (multi-connection serve without `auto`), the tool MUST return `-32602 InvalidParams` with detail listing valid connection names.

### 5.3 Resource URIs always carry the connection

Resource URIs include the connection as the host segment (`dbprint://<conn>/...`). Resource enumeration and reads are always unambiguous regardless of how many connections the server exposes.

---

## 6. Transport

### 6.1 stdio (default)

Default transport for editor/agent integration. The server reads MCP frames from stdin and writes responses to stdout. Logging and warnings MUST be routed to stderr; mixing them into stdout corrupts the protocol.

Selected with `--transport stdio` (or by omitting the `--transport` flag).

### 6.2 HTTP / SSE

Local-only HTTP transport for clients that prefer network sockets over stdio.

| Flag | Default | Effect |
|---|---|---|
| `--transport http` | (off) | Switches to HTTP / SSE |
| `--host HOST` | `127.0.0.1` | Listen address; must be loopback |
| `--port PORT` | (required when `--transport http`) | TCP port |

The server MUST bind only to loopback addresses (`127.0.0.1` or `::1`). Binding to other interfaces (`0.0.0.0`, public IPs) MUST be rejected at startup with a clear error.

There is no authentication, and no remote deployment model.

---

## 7. Lifecycle

### 7.1 File freshness

The server MUST re-read every print file from disk on every tool call and every `resources/read` call. There is no in-memory cache; the protocol overhead dominates the file-read cost for YAML files of typical size.

Consequence: a `dbprint generate` run completing while the server is running is visible to the next tool call. No restart required.

### 7.2 Capabilities

Per §2, the server advertises `subscribe: false` and `listChanged: false`. The server MUST NOT emit `notifications/resources/updated` or `notifications/resources/listChanged`.

### 7.3 Shutdown

The server MUST handle SIGTERM and SIGINT cleanly:

- Stop accepting new requests.
- Allow in-flight requests to complete.
- Flush stderr.
- Exit with code 0.

For stdio transport, EOF on stdin is equivalent to SIGTERM — the server MUST exit cleanly when the client closes the connection.

---

## 8. Error model

Two channels, not one. `resources/read` failures are genuine JSON-RPC protocol errors — the codes below reach the wire in the response's `error` field. `tools/call` failures are not: a tool fault is a successful RPC carrying `isError: true`, with the detail string as the result's text content, per the MCP tool-call idiom. An internal `McpError`'s JSON-RPC code still classifies its kind (bad input vs internal inconsistency) but is never sent for a tool call. Detail strings are intended for LLM consumers either way — verbose, structured, and actionable.

### 8.1 Resource errors (`resources/read`) — JSON-RPC protocol errors

| Trigger | JSON-RPC code | Detail format example |
|---|---|---|
| Reading an absent optional file (e.g., missing `description.md` / `statistics.annotations.yaml`) | `-32602 InvalidParams` | `"description.md is optional and not authored for table 'arboretum.seedbank.accession'."` |
| Reading a resource for an unconfigured connection | `-32602 InvalidParams` | `"connection 'bar' not in .dbprint.yaml. Configured: ['a', 'b', 'c']"` |
| Reading a resource for a connection configured but not served by this server instance | `-32602 InvalidParams` | `"connection 'staging' is configured but not served by this instance. Served: ['arboretum']. Restart the server naming it to serve it."` |
| Reading a resource for an unknown table | `-32602 InvalidParams` | `"table 'foo' not found in connection 'bar'. Run dbprint list bar for valid names."` |
| Reading a kind the manifest never declares for that table (e.g. `relationships` for a plain view declaring none) | `-32602 InvalidParams` | `"relationships is not declared for table 'arboretum.seedbank.v' - check the object's type before requesting it; the manifest does not declare every kind for every object."` |
| Manifest references a file that is absent on disk | `-32603 InternalError` | `"manifest references statistics.yaml but file is absent at <path>. Re-run dbprint generate."` |
| Manifest parses as YAML but is not the shape every reader below it walks | `-32603 InternalError` | `"<path>: <reason the shape is wrong>"` |
| YAML parse error in a print file (any YAML-typed artifact, not manifest.yaml alone) | `-32603 InternalError` | `"<path>: YAML parse error: <message>"` |
| Reading `diff` with no committed diff for the connection | `-32603 InternalError` | `"no diff available at <path>. Run dbprint diff or dbprint generate first."` |
| Reading `reading` with no reading.md for the connection | `-32603 InternalError` | `"no reading guide available at <path>. Run dbprint generate first."` |
| Resource requested with malformed URI | `-32602 InvalidParams` | `"URI 'foo' does not match the dbprint:// scheme."` |

### 8.2 Tool errors (`tools/call`) — `isError: true`, not a protocol error

Every call is checked against the tool's own advertised `inputSchema` before the tool body sees it, so an argument fault names the key, the value that arrived, and what the tool accepts. Two arguments fold case before that check - `get_table_context`'s `format` and `purpose` - so `MD` and `QUERY` are accepted; `get_reference`'s `document` does not fold, and the asymmetry is deliberate rather than an oversight. A non-string value is a type fault, never an enum one: `str(7).lower()` would name the wrong failure.

| Trigger | Detail format example |
|---|---|
| Unknown `table` | `"table 'foo' not found in connection 'bar'. Run dbprint list bar for valid names."` |
| Unknown `conn` | `"connection 'bar' not in .dbprint.yaml. Configured: ['a', 'b', 'c']"` |
| `conn` configured but not served by this server instance | `"connection 'staging' is configured but not served by this instance. Served: ['arboretum']. Restart the server naming it to serve it."` |
| A key the tool does not declare | `"list_tables takes no argument 'schema'. Accepted: ['conn', 'detail', 'pattern']."` |
| A required key absent | `"get_table_context requires 'table'."` |
| A key of the wrong type | `"pattern 7 must be of type string."` |
| An empty string where one is required | `"table '' must be a non-empty string."` |
| A value outside a declared enum | `"format 'yml' must be one of ['md', 'json', 'yaml']."` |
| A value below a declared minimum | `"budget_tokens 0 must be an integer >= 1."` |
| `resolve_value` called with a `column` the table's statistics do not carry | `"column 'rnk' not found in table 'seedbank.taxon'. Columns: rank, scientific_name, taxon_id."` |
| `resolve_value` on a table whose declared `statistics.yaml` is absent from disk | `"manifest references statistics.yaml but file is absent at <path>. Re-run dbprint generate."` |
| `resolve_value` on a table whose `statistics.yaml` or `statistics.annotations.yaml` does not parse | `"<path>: YAML parse error: <message>"` |
| No `conn` given when no default exists | `"no default connection; pass conn explicitly. Configured: ['a', 'b', 'c']"` |
| Manifest references a file that is absent on disk | `"manifest references statistics.yaml but file is absent at <path>. Re-run dbprint generate."` |
| `get_diff` with no committed diff for the connection | `"no diff available at <path>. Run dbprint diff or dbprint generate first."` |
| Manifest parses as YAML but is not the shape every reader below it walks | `"<path>: <reason the shape is wrong>"` |
| YAML parse error in a print file | `"<path>: YAML parse error: <message>"` |
| Tool requested with unknown name | `"unknown tool 'foo'. Available: ['get_diff', 'get_manifest', ...]"` |
| `get_reference` called with `document` outside its declared enum | `"document 'readme' must be one of ['assertions', 'spec']."` |
| `get_reference` called with a `section` no heading in that document carries | `"section '9.9' not found in spec. Available: ['0', '0.1', '0.2', ...]"` |

### 8.3 Error responses are not exceptions

Neither channel crashes the connection. The server MUST NOT terminate on an application-level error, in either form. Crashes are reserved for genuine internal-state corruption that prevents serving the next request safely.

---

## 9. Forward compatibility

Consumers MUST tolerate:

- **Unknown tool names** — clients SHOULD ignore unknown tool entries in `tools/list` rather than treating them as protocol violations.
- **Unknown resource URIs** matching the `dbprint://` scheme but with new patterns — clients SHOULD accept entries returned by `resources/list` without enumerating expected patterns.
- **New capability flags** added under `resources` or `tools` — clients SHOULD ignore unknown flags.
- **New mimeTypes** on resources — clients SHOULD treat unknown mimeTypes as opaque text.
- **New optional fields** in tool return shapes — clients SHOULD ignore unknown fields.

The tool set and resource URI patterns MAY grow in MINOR releases (additive only). Existing tool signatures and resource URI patterns MUST NOT change within a MAJOR version.

---

## Cross-references

- [`format/v1/SPEC.md`](format/v1/SPEC.md) — format specification for `manifest.yaml`, `diff.yaml`, and per-table artifacts
- [`ASSERTIONS.md`](ASSERTIONS.md) — assertion DSL specification for `dbprint check --online`
