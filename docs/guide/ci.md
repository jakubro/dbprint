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
| `1` | generic — a malformed print, a `.dbprint.yaml` that fails to load, or a column the current `redact` rules would publish otherwise than the print does (`privacy.redaction-not-applied`) |
| `2` | staleness — a print is older than its own threshold |
| `3` | drift (`--online`) — the committed print no longer matches the database |
| `4` | connection (`--online`) — the database could not be reached, or the target lists none of the committed tables |
| `5` | partial extraction — the committed manifest names a table the last `generate` could not profile, or (`--online`) the connection was reached but some tables could not be re-extracted; the ones that were are still compared and reported |
| `6` | assertion — a data-quality assertion failed |

Drift covers what a committed print can be compared on. Column defaults, indexes and comments are recorded only in `ddl.sql`, which the comparison does not read, so a change to one of them alone is not drift: neither `check --online` nor `generate` reports it ([tracking drift](drift.md)).

When several outcomes apply — within one connection or across connections — the exit code is the first of `1`, `4`, `5`, `6`, `3`, `2` that does: a result that cannot be trusted or a table that was not read outranks the state of a trustworthy print. So while the manifest names a table the last `generate` could not profile, offline `check` returns `5` over a stale print or drift until a `generate` profiles it, and a malformed print reads as `1` whatever else holds. A pipeline that needs every cause reads `--format json`, which lists every issue.

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
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.13"
      - run: pip install dbprint
      - run: dbprint check
```

No extra is needed: the offline path opens no connection, so it loads no driver.

For the online job, install the extra for your engine and supply credentials through the environment. Every credential key reads from `DBPRINT_<CONNECTION>_<KEY>`, upper-cased, which keeps them out of the repository:

```yaml
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.13"
      - run: pip install 'dbprint[postgres]'
      - run: dbprint check --online
        env:
          DBPRINT_PRIMARY_HOST: ${{ secrets.DB_HOST }}
          DBPRINT_PRIMARY_USER: ${{ secrets.DB_USER }}
          DBPRINT_PRIMARY_PASSWORD: ${{ secrets.DB_PASSWORD }}
```

The online job needs a database reachable from the runner and the read privileges your engine's adapter page lists — [duckdb](../adapters/duckdb.md), [PostgreSQL](../adapters/postgres.md), [MySQL](../adapters/mysql.md), [ClickHouse](../adapters/clickhouse.md), [Redshift](../adapters/redshift.md), [Snowflake](../adapters/snowflake.md), [Databricks](../adapters/databricks.md), [BigQuery](../adapters/bigquery.md). PostgreSQL additionally needs `pg_dump` on `PATH`, at least the server's major version.

It also needs the temporary-table privilege from that page if anything in scope resolves to a fraction. `--online` re-extracts, so any table read in `sample` mode — by `fraction`, or by a `max_rows` ceiling at any level that resolves below its size — makes the job write a temporary table. Without the privilege PostgreSQL, duckdb and Databricks read the seeded sample directly, with a warning; MySQL, Snowflake, ClickHouse, Redshift and BigQuery fail those tables instead, and the job exits `5`.

## Exit codes from `generate`

A scheduled regenerate job switches on a different set, since `generate` connects and writes:

| Code | Meaning |
|---|---|
| `0` | ok |
| `1` | generic — the configuration does not load, or the committed `manifest.yaml` cannot be read or followed, or is missing beside existing prints; nothing is written |
| `3` | schema drift was recorded against the previous print — or there was none: a first run records every table as added |
| `4` | connection — the database could not be reached, `pg_dump` is missing, or the target lists none of the committed tables |
| `5` | partial — some tables failed; the rest were written |
| `7` | total failure — every table it touched failed |

When several apply — within one connection or across connections — the exit code is the first of `1`, `4`, `7`, `5`, `3` that does.

`3` reports *schema* movement only. A statistic that moved lands in `diff.yaml` without changing the exit code, so a job that gates on data movement reads the artifact rather than the status.

## Output and progress

Both commands render differently to a terminal than to a pipe, and both take flags to force the choice. `--no-tui` gives the plain tab-separated form a log should capture; `--tui` forces the Rich form.

`generate`'s progress is on stderr, the same as `check` and `diff` — merge it into the pipe to capture it. One tab-separated record per event that has something to report, with a `start`/`done` pair per preparatory phase, a pair per table, and a `sketched` line for each table that got a join-key sketch:

```console
$ dbprint generate 2>&1 | tee generate.log
primary	connecting	start
primary	connecting	done
primary	listing	start
primary	listing	done
primary	inventory	start
primary	inventory	done
primary	inventory	schema	arboretum.public	4 objects	0.0s
primary	arboretum.public.accession	start
primary	arboretum.public.accession	ok	2,500 rows	0.2s
primary	arboretum.public.collector	start
primary	arboretum.public.collector	ok	120 rows	0.1s
primary	arboretum.public.taxon	start
primary	arboretum.public.taxon	ok	300 rows	0.1s
primary	arboretum.public.taxon_names	start
primary	arboretum.public.taxon_names	ok	- rows	0.1s
primary	arboretum.public.accession	sketched	0.0s
primary	arboretum.public.collector	sketched	0.0s
primary	arboretum.public.taxon	sketched	0.0s
primary	summary	4 ok / 0 failed / 0 skipped	0.9s
```

`-q` silences stderr progress on every command — `generate`, `diff` and `check` all put it there, so a pipeline reaching for `-q` to quieten a log never affects a command's own stdout payload.

## The machine envelope

`--format json` returns an array with one object per connection:

| Key | Holds |
|---|---|
| `connection` | the connection name |
| `manifest_present` | whether a committed manifest was found at all |
| `exit_code` | this connection's own code, before the connections are combined |
| `default_max_age_days` | the connection's configured threshold |
| `issues` | conformance findings |
| `stale_entries` | tables past their recorded threshold |
| `drift_issues` | findings from `--online` re-extraction |
| `assertion_issues` | failed assertions |
| `not_run` | tables that could not be judged, each with a `severity` |
| `summary` | per-category counts |

Every entry in `issues`, `drift_issues` and `assertion_issues` carries `path`, `code`, `severity`, `detail` and `spec_ref`, so one `jq` filter works across all three.

## Assertions

`check` also evaluates the `assertions` block in `.dbprint.yaml`, which is where a data-quality expectation lives:

```yaml
# .dbprint.yaml
connections:
  primary:
    assertions:
      tables:
        arboretum.seedbank.collector:
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
