---
name: dbprint-annotate
description: Write the notes that explain a database's dbprint snapshot - what its codes, units, sentinel values and exclusions mean - from the code that writes each table and read-only queries against the database. Use when asked to annotate or document a database's tables, or when dbprint annotate --check reports findings.
---

# Annotating a dbprint print

A print measures what the data holds. It cannot say what a code means, which unit a number is in, what a far-future date stands for, or which rows a report leaves out — and an agent reading the print stops looking for those answers elsewhere. The annotation layer is where they go. A wrong annotation is worse than none, because the reader follows it, so every statement written here is sourced, scoped and, wherever a statistic can check it, checked.

Work one table at a time, in the order below. You need the print (`prints/<connection>/`), the code that writes and reads the tables, and read-only access to the database. Never write to the database, and never edit a producer-written file (`statistics.yaml`, `relationships.yaml`, `ddl.sql`, `manifest.yaml`, `reading.md`).

## 1. Read what the print already says

`dbprint context <table>` (or `get_table_context`) shows the DDL, every column's statistics, the relationships and any annotations already written. A header line `Meanings: not documented here` marks a table nobody has annotated yet. Note every coded column (a short complete value list), every column whose unit is not in its name, every value far from the rest (`2099-12-31`, `-1`, `0` on a large share of rows), and every measured or inferred join.

## 2. Read the code before the data

Find what writes the table — migrations, models, the job or service that inserts and updates it — and what reads it: the reports, exports and queries that consume it. The code states what each code means, which unit a number is in, which default a null stands for, which time zone a timestamp was written in, and which rows a consumer excludes. Record the file path beside every fact you take from it.

## 3. Confirm with read-only queries

Check each fact from the code against the data before writing it down, and record the query beside it:

- **Grain.** Which columns identify a row — `SELECT acc.accession_id, COUNT(1) FROM seedbank.accession acc GROUP BY 1 HAVING COUNT(1) > 1` returning nothing.
- **Codes.** Every listed value of a coded column, and which code path writes each one. A value the code never writes is a finding, not a typo.
- **Units and scale.** Grams or kilograms, cents or whole units, a share or a percentage.
- **Sentinels and defaults.** What a far-future date, a `-1` or a null stands for, and on how many rows.
- **Time zones.** Whether a timestamp is UTC or local time, and whose local time.
- **Duplicates.** Values equal up to case or whitespace that the code treats as one.

A fact the code states and the data contradicts is written as the data shows it, with the contradiction noted. A question neither settles is written as an open question (step 5), never guessed.

## 4. Write the files

**`description.md`**, beside the table's `statistics.yaml`. Open with one sentence stating the grain ("One row per accession, keyed by `accession_id`."). Then where the rows come from: what writes them, when, and what a row's lifecycle is. Then the query notes — each one a rule a correct query needs, with its scope stated:

> Accessions on shelf `QX` are excluded from stock totals of `seed_count`; they still count when counting accessions received in a period. Source: `reports/stock.sql`.

A rule that applies to some calculations only says which. "Exclude shelf QX" written without its scope will be applied to every count a reader writes.

**`statistics.annotations.yaml`**, beside it. A column `note` for what a column means; a `values` entry for each code; `claims` for every statement a statistic can check:

```yaml
# statistics.annotations.yaml
format_version: 1
columns:
  shelf_code:
    note: Vault shelf the accession is stored on; the first letter names the cold room (`vaults/layout.py`).
    values:
      - value: QX
        note: Quarantine shelf, not yet cleared for germination trials. Excluded from stock totals of `seed_count`; still a row when counting accessions received.
  viability_pct:
    note: Share of seeds that germinated in the latest trial, in percent from 0 to 100, not a 0-1 fraction.
    claims:
      range.min: {min: 0}
      range.max: {max: 100}
  seed_count:
    note: Seeds counted at intake, before any trial draws from the accession.
    claims:
      null_rate: 0
  received_at:
    note: Written in UTC by the intake service (`intake/receive.py`); a report's day boundaries are UTC days.
grain:
  keys:
    - columns: [accession_id]
      note: Assigned at intake and never reused (`models/accession.py`).
```

A column copied or computed from another table's column — a `curator_profile.email` copied from `collector.email` — carries `derived_from` naming that column (`derived_from: [{table: arboretum.seedbank.collector, column: email}]`), and the pitfall is written once, on the source: a query-purpose context for the derived column carries the source's notes beside its own. A print generated with a dbt manifest already records this lineage for every model column it could trace; write `derived_from` only where it is missing or wrong.

**`relationships.annotations.yaml`**, beside it. Reject a measured or inferred edge that joins two columns sharing nothing but values, and add a join the producer could not find, each with a note naming its source:

```yaml
# relationships.annotations.yaml
format_version: 1
refers_to:
  - column: [seed_count]
    target_table: arboretum.seedbank.taxon
    target_column: [taxon_id]
    verdict: rejected
    note: A count of seeds, not a taxon id; the two ranges overlap by chance.
```

An added join takes the same shape without `verdict`, its `note` naming the code that writes the column.

**`manifest.annotations.yaml`**, at the print root, for a fact true of many tables — a code shared across them, a unit every timestamp uses. Write it once there rather than in every table's files.

## 5. Rules for every statement

- **Sourced.** Each statement names the file or the query it rests on.
- **Scoped.** A rule says which calculations it applies to and which it does not.
- **Checkable where possible.** Anything a statistic can check — a null rate, a range bound, a closed set of values, uniqueness — is also a `claims` entry, so `dbprint check` reports it the day it stops being true. Prose in `description.md` is checked by nothing.
- **Open questions stay open.** Write "Open question: whether `viability_pct` before the trial form was rebuilt was recorded as a percent; its history does not say" — never a hedged rule such as "probably a percent, treat with care", which reads as an instruction and gets followed either way.
- **Measured beats written.** Do not restate a count, a null rate or a value list the statistics already carry; the statistics are current as of the last run and prose is not. Annotate what a statistic cannot express.

## 6. Check before finishing

A newly created per-table annotation file is invisible until the next run records it in the manifest, so regenerate first, then check the table:

```console
$ dbprint generate
$ dbprint annotate <table> --check
```

It lists every finding on the table's annotation files — a contradicted claim, a stale column or value key, a verdict on an edge that no longer exists, an unassertable claim, a file the manifest does not record yet — and exits 1 while any remains. A contradicted claim means the claim or the note beside it is wrong: re-read the code and re-run the query, then fix the annotation, never the statistic. Repeat until it exits 0. Then read the table once more through `dbprint context <table> --purpose query`, as an agent writing SQL will, and confirm every rule a correct query needs is on that page.
