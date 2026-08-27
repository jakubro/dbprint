# Gating CI

`dbprint check` is the command a pipeline runs. It answers three questions in one pass — is the print well-formed, is it fresh, and does the data still satisfy the assertions you wrote — and reports the outcome in an exit code.

## Offline by default

```console
$ dbprint check
```

No connection, no credentials, no database. It reads the committed print and verifies its structure against the format, its freshness against each table's recorded threshold, and any assertions that can be evaluated from the artifact alone. That makes it a job you can run on every pull request, including one from a fork.

```console
$ dbprint check --online
```

Adds a connection and re-extracts, so it can also report drift between the committed print and the live database. This needs credentials and costs a profiling run, so it is usually a separate, slower job — see [tracking drift](drift.md) for when each is worth running.

## Exit codes

Seven outcomes from `check`, ordered so a pipeline can decide how much each one deserves.

| Code | Meaning |
|---|---|
| `0` | ok |
| `1` | generic — a malformed print, or a table whose rules narrow it both by a predicate and by a fraction, which the command refuses to judge |
| `2` | staleness — a print is older than its own threshold |
| `3` | drift (`--online`) — the committed print no longer matches the database |
| `4` | connection (`--online`) — the database could not be reached |
| `5` | partial extraction (`--online`) — the connection was reached but some tables could not be re-extracted; the ones that were are still compared and reported |
| `6` | assertion — a data-quality assertion failed |

Across several connections the top-level exit is the highest of the per-connection codes.

One more code exists in the family and `check` never returns it: `7`, total failure, which `dbprint generate` uses when every table it touched failed. A pipeline treating any non-zero as fatal does not care; one that switches on the number should not expect `7` from a gate job.

## Which severities you actually see

This is worth knowing before you rely on the output.

**Errors are listed in full.** Each one prints its path, its code and the specification section it was raised against.

**Warnings are reported as a count.** The clean line reads `OK: conformance clean (3 warning(s))` and the codes are not named. So a `privacy.unredacted-sensitive` on a column you forgot to redact, or a `manifest.orphaned-artifact` on an annotation file created after the last generate, is present in the output as a number and nothing more.

To see which warnings a print carries, take the machine envelope:

```console
$ dbprint check --format json | jq '.[].issues[] | select(.severity == "warning")'
```

`--format json` carries every issue at both severities, each with its `path`, `code`, `detail` and `spec_ref`. The same set is available in-process from `validate_print()` — see [emitting a conforming print](../producers.md).

Only errors gate conformance; a warning never changes the exit code. [Conformance codes](../reference/conformance.md) lists every code and its severity, and [SPEC 6.1](../format/v1/SPEC.md#61-severity-model) defines what each severity means.

## A workflow job

```yaml
name: dbprint

on: [push, pull_request]

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install dbprint
      - run: dbprint check
```

No extra is needed: the offline path opens no connection, so it loads no driver.

For the online job, install the extra for your engine and supply credentials through the environment. Every credential key reads from `DBPRINT_<CONN>_<KEY>`, upper-cased, which keeps them out of the repository:

```yaml
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install 'dbprint[postgres]'
      - run: dbprint check --online
        env:
          DBPRINT_PRIMARY_HOST: ${{ secrets.DB_HOST }}
          DBPRINT_PRIMARY_USER: ${{ secrets.DB_USER }}
          DBPRINT_PRIMARY_PASSWORD: ${{ secrets.DB_PASSWORD }}
```

The online job needs a database reachable from the runner and the read privileges its adapter page — [PostgreSQL](../adapters/postgres.md), [MySQL](../adapters/mysql.md), [Snowflake](../adapters/snowflake.md) — lists. PostgreSQL additionally needs `pg_dump` on `PATH`, at least the server's major version.

It also needs the temporary-table privilege from that page if anything in scope resolves to a fraction. `--online` re-extracts, so a `sample`, a `filter`-free `max_rows_scanned` ceiling, or a connection-level ceiling all make the job write a temporary table. Without the privilege the job still passes, but each statistic for those tables is measured over its own draw.

## Exit codes from `generate`

A scheduled regenerate job switches on a different set, since `generate` connects and writes:

| Code | Meaning |
|---|---|
| `0` | ok |
| `1` | generic |
| `3` | schema drift was recorded against the previous print |
| `4` | connection — the database could not be reached, or `pg_dump` is missing |
| `5` | partial — some tables failed; the rest were written |
| `7` | total failure — every table it touched failed |

`3` reports *schema* movement only. A statistic that moved lands in `diff.yaml` without changing the
exit code, so a job that gates on data movement reads the artifact rather than the status.

## Output and progress

Both commands render differently to a terminal than to a pipe, and both take flags to force the
choice. `--no-tui` gives the plain tab-separated form a log should capture; `--tui` forces the Rich
form.

A piped `dbprint generate` looks like this — one tab-separated record per event that has
something to report, with a `start`/`done` pair per preparatory phase, a pair per table, and a
`sketched` line for each table that got a join-key sketch:

```console
$ dbprint generate | tee generate.log
primary	connecting	start
primary	connecting	done
primary	listing	start
primary	listing	done
primary	inventory	start
primary	inventory	done
primary	inventory	schema	public	4 objects	0.0s
primary	public.accession	start
primary	public.accession	ok	2,500 rows	0.2s
primary	public.collector	start
primary	public.collector	ok	120 rows	0.1s
primary	public.taxon	start
primary	public.taxon	ok	300 rows	0.1s
primary	public.taxon_names	start
primary	public.taxon_names	ok	- rows	0.1s
primary	public.accession	sketched	0.0s
primary	public.collector	sketched	0.0s
primary	public.taxon	sketched	0.0s
primary	summary	4 ok / 0 failed / 0 skipped	932ms
```

`-q` silences progress, and **which stream it silences differs by command**: on `generate` it is
stdout, which is where that command's progress goes; on `diff` and `check` it is stderr, and the
stdout payload is unaffected. A pipeline reaching for `-q` on `diff` to quieten a log still gets the
full report on stdout.

## The machine envelope

`--format json` returns an array with one object per connection:

| Key | Holds |
|---|---|
| `connection` | the connection name |
| `manifest_present` | whether a committed manifest was found at all |
| `exit_code` | this connection's own code, before the run-wide maximum is taken |
| `default_max_age_days` | the connection's configured threshold |
| `issues` | conformance findings |
| `stale_entries` | tables past their recorded threshold |
| `drift_issues` | findings from `--online` re-extraction |
| `assertion_issues` | failed assertions |
| `not_run` | tables that could not be judged, each with a `severity` |
| `summary` | per-category counts |

Every entry in `issues`, `drift_issues` and `assertion_issues` carries `path`, `code`, `severity`,
`detail` and `spec_ref`, so one `jq` filter works across all three.

## Assertions

`check` also evaluates the `assertions` block in `.dbprint.yaml`, which is where a data-quality expectation lives:

```yaml
# .dbprint.yaml
connections:
  primary:
    assertions:
      tables:
        seedbank.collector:
          columns:
            email:
              null_rate: 0
```

Offline, an assertion is evaluated against the committed print — so it tells you what was true when the print was taken. Under `--online` it is evaluated against a fresh extraction. A predicate over a redacted column's values is refused rather than evaluated against placeholders. The grammar, the severities and the full predicate vocabulary are in [Assertions](../ASSERTIONS.md).

## Failing on staleness alone

A pipeline that wants to fail only when a print has aged past a deadline of its own can override every recorded threshold for one run:

```console
$ dbprint check --max-age 7d
```

The explicit flag governs every table directly and reads no rule to find a threshold, so it is independent of whatever the config has grown since.
