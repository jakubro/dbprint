# dbprint CLI reference

Complete `--help` for every command, captured verbatim. This file is generated
from the CLI itself - do not edit it by hand. Run `just docs` to regenerate it
after changing a command's docstring, options, or help sections.

## `dbprint`

```text
 Usage: dbprint [OPTIONS] COMMAND [ARGS]...

 dbprint - offline database prints (DDL + column statistics) for AI agents.
 A print is a portable, git-committed snapshot of a database's structure and column-level data
 distributions, consumable offline by humans, AI coding agents, and CI.

 The commands below scaffold a project, profile the database, and verify or consume the committed
 prints.

 Typical workflow: init -> generate -> diff (ad-hoc) or check (CI gate).

 Run dbprint COMMAND --help for per-command usage. The on-disk format is specified in SPEC.md,
 which ships inside the package and is published at
 https://github.com/jakubro/dbprint/blob/main/docs/format/v1/SPEC.md

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --version  Show the version and exit.                                                            │
│ --debug    Print full tracebacks on error.                                                       │
│ --help     Show this message and exit.                                                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────────────────╮
│ check     Verify committed prints are well-formed, fresh, and meet assertions.                   │
│ context   Emit an agent-ready context fragment for committed tables.                             │
│ diff      Compare committed prints against the live database (read-only).                        │
│ docs      Browse a committed print as an HTML site: serve it live, or build it static.           │
│ generate  Profile the live database; write prints and a structured diff.                         │
│ init      Scaffold a new dbprint project in the current directory.                               │
│ list      Summarise committed prints offline (no database connection).                           │
│ serve     Run a read-only MCP server over the committed prints.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## `dbprint init`

```text
 Usage: dbprint init [OPTIONS]

 Scaffold a new dbprint project in the current directory.
 Writes .dbprint.yaml (project config), creates the prints/ output root, and writes a
 ~/.dbprint/connections.yaml credentials template when one does not already exist. Idempotent:
 .dbprint.yaml is kept unless --force is given; the credentials stub is always kept once it exists,
 --force or not, since it is machine-wide rather than project-local. Prints one outcome line per
 file (wrote / kept / created).

 Exit codes:

  • 0: always (init has no failure path)

 Examples:

  • dbprint init: scaffold; keep anything that already exists
  • dbprint init --force: overwrite .dbprint.yaml; credentials untouched

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --force  Overwrite an existing .dbprint.yaml. Never touches the creds stub -                     │
│          ~/.dbprint/connections.yaml is shared by every project on the host and is written only  │
│          when absent.                                                                            │
│ --help   Show this message and exit.                                                             │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## `dbprint generate`

```text
 Usage: dbprint generate [OPTIONS] [CONN]

 Profile the live database; write prints and a structured diff.
 Connects to each resolved connection, scans the tables matched by the include/exclude selectors,
 extracts DDL + column statistics + relationships, and writes one print per table plus a
 prints/<conn>/diff.yaml describing what changed. Per-table writes are atomic and a user-authored
 description.md or statistics.annotations.yaml is never touched. Auto connections run sequentially,
 each isolated so one failure does not block the rest. Writes one run log to
 ~/.dbprint/logs/<project-slug>/, keeping the 3 most recent.

 Selector patterns are fnmatch globs over lowercased FQNs (* spans dots, ? matches one character);
 --include intersects and --exclude unions, so both only ever narrow scope.

 Arguments:

  • CONN: connection to profile; resolved from .dbprint.yaml when omitted (the auto: true set, or
    the sole connection).

 Exit codes:

  • 0: ok (also when every matched table was already current, so nothing was profiled)
  • 1: generic
  • 3: schema drift (the database's shape moved relative to the baseline - a table, column,
    relationship, index or comment). Statistics that moved are recorded in diff.yaml but do not set
    this code; dbprint check --online reports both
  • 4: connection
  • 5: partial (some tables failed, others succeeded or were skipped; or every table succeeded but
    the sketch pass that runs after them did not)
  • 7: total failure (no table was profiled)

 Examples:

  • dbprint generate: all auto connections
  • dbprint generate warehouse: one connection
  • dbprint generate --include 'public.*': narrow scope for this run
  • dbprint generate --dry-run: preview plan + diff, write nothing
  • dbprint generate --fail-fast: stop at the first table failure

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --project           TEXT  Exact project locator: a directory whose direct child is               │
│                           .dbprint.yaml, that .dbprint.yaml file itself, or a git address (a     │
│                           forge URL, an SSH remote, or <git-url>#<ref>:<subpath>). No upward     │
│                           walk, no downward scan. Omit it to walk up from the working directory  │
│                           instead.                                                               │
│ --force                   Re-profile every matched table, bypassing the freshness skip.          │
│ --dry-run                 Compute everything; write nothing to disk.                             │
│ --include           TEXT  Narrow scope to tables also matching PATTERN (intersects config        │
│                           include); repeatable. e.g. --include 'public.*'                        │
│ --exclude           TEXT  Also drop tables matching PATTERN (unions config exclude); repeatable. │
│                           e.g. --exclude '*.audit_*'                                             │
│ --fail-fast               Stop at the first table failure instead of profiling the rest. Use     │
│                           when a target is failing systemically, to avoid repeating one doomed   │
│                           query per table.                                                       │
│ --tui/--no-tui            Force TTY (Rich) or piped (plain-text) rendering.                      │
│ --quiet         -q        Silence stdout progress (footer / tree / streaming / summary) -        │
│                           generate writes nothing else to stdout.                                │
│ --help                    Show this message and exit.                                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## `dbprint diff`

```text
 Usage: dbprint diff [OPTIONS] [CONN]

 Compare committed prints against the live database (read-only).
 Re-extracts the live schema + statistics and diffs them against the committed prints, emitting the
 same structured diff that generate writes - without touching disk on either side. Differences are
 reported, not failures: a successful comparison always exits 0. Progress goes to stderr so the
 diff payload on stdout stays clean for piping. Writes one run log to
 ~/.dbprint/logs/<project-slug>/, keeping the 3 most recent, unaffected by --quiet (which silences
 stderr progress only).

 Selector patterns are fnmatch globs over lowercased FQNs (* spans dots); --include intersects and
 --exclude unions, so both only ever narrow scope.

 Arguments:

  • CONN: connection to compare; resolved from .dbprint.yaml when omitted (the auto: true set, or
    the sole connection).

 Exit codes:

  • 0: ran (differences are not failures)
  • 1: no baseline or invalid connection
  • 4: connection
  • 5: partial extraction

 Examples:

  • dbprint diff: compare all auto connections
  • dbprint diff warehouse --format json: machine-readable diff
  • dbprint diff --threshold 0.05: ignore statistic moves under 5%

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --project           TEXT               Exact project locator: a directory whose direct child is  │
│                                        .dbprint.yaml, that .dbprint.yaml file itself, or a git   │
│                                        address (a forge URL, an SSH remote, or                   │
│                                        <git-url>#<ref>:<subpath>). No upward walk, no downward   │
│                                        scan. Omit it to walk up from the working directory       │
│                                        instead.                                                  │
│ --include           TEXT               Narrow scope to tables also matching PATTERN (intersects  │
│                                        config include); repeatable. e.g. --include 'public.*'    │
│ --exclude           TEXT               Also drop tables matching PATTERN (unions config          │
│                                        exclude); repeatable.                                     │
│ --format            [human|json|yaml]  Output format.                                            │
│                                        [default: human]                                          │
│ --output            FILE               Write the diff to FILE instead of stdout.                 │
│ --threshold         FLOAT              Minimum relative drift (0-1) before a statistic counts as │
│                                        changed; raises this run's noise floor. Human format      │
│                                        only. e.g. 0.05 = ignore moves under 5%.                  │
│ --tui/--no-tui                         Force TTY (Rich) or piped (plain-text) rendering; also    │
│                                        drives stderr progress.                                   │
│ --quiet         -q                     Silence stderr progress (footer / tree / streaming /      │
│                                        summary); stdout payload unaffected.                      │
│ --help                                 Show this message and exit.                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## `dbprint list`

```text
 Usage: dbprint list [OPTIONS] [CONN]

 Summarise committed prints offline (no database connection).
 Reads prints/<conn>/manifest.yaml and reports connection metadata, table and schema counts,
 freshness buckets (live / stale / dormant) relative to each table's own max_age_days, and how many
 tables carry a user-authored description.md. Never connects to the database.

 Arguments:

  • CONN: connection to summarize; resolved from .dbprint.yaml when omitted (the auto: true set, or
    the sole connection).

 Exit codes:

  • 0: ok
  • 1: a connection could not be summarised - its manifest is missing or unparseable, or its rules
    narrow one of its tables both by a predicate and by a fraction. Other connections are still
    summarised

 Examples:

  • dbprint list: all auto connections
  • dbprint list warehouse: one connection

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --project       TEXT  Exact project locator: a directory whose direct child is .dbprint.yaml,    │
│                       that .dbprint.yaml file itself, or a git address (a forge URL, an SSH      │
│                       remote, or <git-url>#<ref>:<subpath>). No upward walk, no downward scan.   │
│                       Omit it to walk up from the working directory instead.                     │
│ --tui/--no-tui        Force TTY (Rich) or piped (plain-text) rendering.                          │
│ --help                Show this message and exit.                                                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## `dbprint check`

```text
 Usage: dbprint check [OPTIONS] [CONN]

 Verify committed prints are well-formed, fresh, and meet assertions.
 CI gate over the committed prints.

  • Offline (default): the manifest is present and conformance-valid, every print is within
    --max-age, and statistic assertions hold.
  • Online (--online): additionally re-extracts the live database to detect drift - both a change
    of shape and a moved statistic - and evaluate SQL assertions.

 The reported exit code is the worst across every evaluated check. --online writes one run log to
 ~/.dbprint/logs/<project-slug>/, keeping the 3 most recent; the offline default writes nothing.

 Arguments:

  • CONN: connection to check; resolved from .dbprint.yaml when omitted (the auto: true set, or the
    sole connection).

 Exit codes:

  • 0: ok
  • 1: generic - a malformed print, or a table whose rules narrow it both by a predicate and by a
    fraction, which this command refuses to judge
  • 2: staleness
  • 3: drift (--online) - the committed print no longer matches the database, including a statistic
    that moved (generate sets this code for a change of shape only)
  • 4: connection (--online, the database could not be reached)
  • 5: partial extraction (--online) - the connection was reached but some tables could not be
    re-extracted; the ones that did are still compared and reported normally
  • 6: assertion failure

 Examples:

  • dbprint check: offline CI gate (no credentials)
  • dbprint check --max-age 24h: fail if any print is older than a day
  • dbprint check --online: add live drift + SQL assertions

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --project           TEXT               Exact project locator: a directory whose direct child is  │
│                                        .dbprint.yaml, that .dbprint.yaml file itself, or a git   │
│                                        address (a forge URL, an SSH remote, or                   │
│                                        <git-url>#<ref>:<subpath>). No upward walk, no downward   │
│                                        scan. Omit it to walk up from the working directory       │
│                                        instead.                                                  │
│ --max-age           TEXT               Max staleness before a print is stale (exit 2), applied   │
│                                        to every table. Duration Nd/Nh/Nm/Ns - e.g. 7d, 12h, 30m; │
│                                        no compound forms like 1d12h. Default: the threshold each │
│                                        table's own print records, falling back to what its rules │
│                                        resolve to for a print that records none - offline, only  │
│                                        rules matching by name apply.                             │
│ --online                               Verify against the live database: schema and statistics   │
│                                        drift + SQL assertions.                                   │
│ --format            [human|json|yaml]  Output format.                                            │
│                                        [default: human]                                          │
│ --tui/--no-tui                         Force TTY (Rich) or piped (plain-text) progress           │
│                                        rendering, on stderr.                                     │
│ --quiet         -q                     Silence stderr progress (footer / tree / streaming /      │
│                                        summary); stdout payload unaffected.                      │
│ --help                                 Show this message and exit.                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## `dbprint context`

```text
 Usage: dbprint context [OPTIONS] [TARGET] [CONN]

 Emit an agent-ready context fragment for committed tables.
 Assembles per-table artifacts (DDL, statistics, relationships, description, annotations) into a
 single prompt-ready block and writes it to stdout or --output. Offline - reads only committed
 prints. Select one table by FQN, a set by fnmatch pattern, or every table with --all. Markdown by
 default; --budget caps the output and stops at the first section that would overflow.

 Arguments:

  • TARGET: table FQN (e.g. arboretum.seedbank.accession), an fnmatch pattern (e.g. public.*), or
    omit and pass --all for every table.
  • CONN: connection scope; resolved from .dbprint.yaml when omitted (the auto: true set, or the
    sole connection).

 Exit codes:

  • 0: ok
  • 1: no match, missing manifest, or budget too small

 Examples:

  • dbprint context arboretum.seedbank.accession: one table, full Markdown
  • dbprint context 'public.*': every public table (pattern)
  • dbprint context --all --no-ddl: every table, skip DDL
  • dbprint context users --budget 4000: cap output near 4000 tokens

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --project           TEXT            Exact project locator: a directory whose direct child is     │
│                                     .dbprint.yaml, that .dbprint.yaml file itself, or a git      │
│                                     address (a forge URL, an SSH remote, or                      │
│                                     <git-url>#<ref>:<subpath>). No upward walk, no downward      │
│                                     scan. Omit it to walk up from the working directory instead. │
│ --all                               Render every table in the manifest.                          │
│ --format            [md|json|yaml]  Output format. json and yaml omit each column's sketch       │
│                                     payload; the table's own statistics.yaml carries it.         │
│                                     [default: md]                                                │
│ --no-ddl                            Omit the DDL section.                                        │
│ --no-relationships                  Omit the Relationships section.                              │
│ --no-description                    Omit the Description section.                                │
│ --no-annotations                    Omit the Annotations section.                                │
│ --no-stats                          Omit the Cardinality table.                                  │
│ --budget            INTEGER         Soft output cap in tokens (approx chars/4); stop at the      │
│                                     first section that would overflow. e.g. 4000                 │
│ --output            FILE            Write output to FILE instead of stdout.                      │
│ --help                              Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## `dbprint serve`

```text
 Usage: dbprint serve [OPTIONS] [CONN]

 Run a read-only MCP server over the committed prints.
 Exposes the committed prints as Model Context Protocol resources and tools for editor and agent
 integration. Requires the [mcp] extra (pip install dbprint[mcp]); exits 1 with an install hint
 when it is missing. Serves the resolved connection set read-only - no database connection. stdio
 transport by default; HTTP binds to loopback only. The project resolves from the working directory
 unless --project names another.

 Arguments:

  • CONN: connection(s) to serve; resolved from .dbprint.yaml when omitted (the auto: true set, or
    the sole connection).

 Exit codes:

  • 0: clean shutdown
  • 1: missing [mcp] extra, bad transport args, or unresolved connection

 Examples:

  • dbprint serve: stdio (editor / agent)
  • dbprint serve --transport http --port 8765: loopback HTTP server
  • dbprint serve --project /srv/analytics: a project outside the working directory

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --project                   TEXT          Exact project locator: a directory whose direct child  │
│                                           is .dbprint.yaml, that .dbprint.yaml file itself, or a │
│                                           git address (a forge URL, an SSH remote, or            │
│                                           <git-url>#<ref>:<subpath>). No upward walk, no         │
│                                           downward scan. Omit it to walk up from the working     │
│                                           directory instead.                                     │
│ --transport                 [stdio|http]  Wire transport. stdio for editor/agent integration;    │
│                                           http for local sockets.                                │
│                                           [default: stdio]                                       │
│ --host                      TEXT          HTTP transport bind address. Must be loopback          │
│                                           (127.0.0.1, ::1, or localhost).                        │
│                                           [default: 127.0.0.1]                                   │
│ --port                      INTEGER       HTTP transport TCP port. Required when --transport     │
│                                           http.                                                  │
│ --read-only/--no-read-only                Read-only over committed prints; no other mode is      │
│                                           supported.                                             │
│                                           [default: read-only]                                   │
│ --help                                    Show this message and exit.                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## `dbprint docs`

```text
 Usage: dbprint docs [OPTIONS] COMMAND [ARGS]...

 Browse a committed print as an HTML site: serve it live, or build it static.
 Renders the whole print - column statistics, relationships, DDL, human annotations - as pages a
 reader clicks through, rather than opening statistics.yaml by hand. Requires the [docs] extra (pip
 install dbprint[docs]); both subcommands exit 1 with an install hint when it is missing.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --help  Show this message and exit.                                                              │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────────────────╮
│ build  Write the docs site as static files - servable by any host that resolves path/index.html. │
│ serve  Serve the docs site live over HTTP, re-reading the print on every request.                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## `dbprint docs serve`

```text
 Usage: dbprint docs serve [OPTIONS] [CONN]

 Serve the docs site live over HTTP, re-reading the print on every request.
 Binds loopback only. Re-reads every artifact from disk on each request, so a page reflects the
 latest generate without restarting the server.

 Arguments:

  • CONN: connection(s) to serve; resolved from .dbprint.yaml when omitted (the auto: true set, or
    the sole connection). Pass --all for completeness instead.

 Exit codes:

  • 0: clean shutdown
  • 1: missing [docs] extra, a non-loopback --host, or an unresolved connection

 Examples:

  • dbprint docs serve: the resolved connection set on 127.0.0.1:8765
  • dbprint docs serve --all --port 9000: every connection, custom port

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --project  TEXT     Exact project locator: a directory whose direct child is .dbprint.yaml, that │
│                     .dbprint.yaml file itself, or a git address (a forge URL, an SSH remote, or  │
│                     <git-url>#<ref>:<subpath>). No upward walk, no downward scan. Omit it to     │
│                     walk up from the working directory instead.                                  │
│ --all               Serve every connection.                                                      │
│ --host     TEXT     Bind address. Must be loopback (127.0.0.1, ::1, or localhost).               │
│                     [default: 127.0.0.1]                                                         │
│ --port     INTEGER  TCP port to bind.                                                            │
│                     [default: 8765]                                                              │
│ --help              Show this message and exit.                                                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## `dbprint docs build`

```text
 Usage: dbprint docs build [OPTIONS] [CONN]

 Write the docs site as static files - servable by any host that resolves path/index.html.
 Recreates --output from scratch on every run, so a page for a table the print no longer has never
 lingers. Refuses to recreate a directory it did not itself create, unless --force is passed.

 Arguments:

  • CONN: connection(s) to build; resolved from .dbprint.yaml when omitted (the auto: true set, or
    the sole connection). Pass --all for completeness instead.

 Exit codes:

  • 0: ok
  • 1: missing [docs] extra, an unresolved connection, or --output exists without this tool's
    marker and --force was not passed

 Examples:

  • dbprint docs build: the resolved connection set to ./dbprint-docs/
  • dbprint docs build --all --output /tmp/site: every connection to a chosen path

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --project  TEXT       Exact project locator: a directory whose direct child is .dbprint.yaml,    │
│                       that .dbprint.yaml file itself, or a git address (a forge URL, an SSH      │
│                       remote, or <git-url>#<ref>:<subpath>). No upward walk, no downward scan.   │
│                       Omit it to walk up from the working directory instead.                     │
│ --all                 Build every connection.                                                    │
│ --output   DIRECTORY  Output directory. Recreated from scratch each run.                         │
│                       [default: dbprint-docs]                                                    │
│ --force               Recreate --output even if it exists without a prior build's marker.        │
│ --help                Show this message and exit.                                                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```
