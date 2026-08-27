# MySQL

```console
$ pip install 'dbprint[mysql]'
```

The extra carries the MySQL Connector/Python driver. Nothing else is needed — DDL comes from `SHOW CREATE TABLE` rather than an external binary.

Fully-qualified names are `database.table`.

## Privileges

```sql
CREATE USER 'dbprint_ro'@'%' IDENTIFIED BY '...';

GRANT SELECT ON my_db.* TO 'dbprint_ro'@'%';

-- Only where views are profiled.
GRANT SHOW VIEW ON my_db.* TO 'dbprint_ro'@'%';

-- Only when a table is narrowed to a fraction; see "When dbprint writes".
GRANT CREATE TEMPORARY TABLES ON my_db.* TO 'dbprint_ro'@'%';
```

| Privilege | On | Needed for |
|---|---|---|
| `SELECT` | the database | connecting, enumerating, DDL and statistics — a plain view takes it for DDL only, since no statement is issued against one |
| `SHOW VIEW` | the database | a view's DDL only |
| `CREATE TEMPORARY TABLES` | the database | the sampled-table copy only |

### What an under-privileged account does

MySQL refuses the connection outright rather than degrading. An account with no grant on the database fails at the first step:

```
primary: could not connect to MySQL at 127.0.0.1:3306/my_db as 'dbprint_ro':
1044 (42000): Access denied for user 'dbprint_ro'@'%' to database 'my_db'
```

and the run exits `4` having profiled nothing. There is no partial print to inspect, which makes a missing grant here easier to diagnose than on the other two engines.

`SHOW VIEW` is the one grant that is easy to miss, because it is needed for exactly one thing. A view's DDL comes from `SHOW CREATE TABLE`, which MySQL gates behind `SHOW VIEW` for views specifically. Without it the tables profile normally and every view fails:

```
1 table failed: ProgrammingError: 1142 (42000): SHOW VIEW command denied to user
'dbprint_ro'@'localhost' for table `my_db`.`taxon_names`
  operation: extract_ddl
  first: my_db.taxon_names
```

A project whose selectors exclude views does not need the grant at all.

## Sampling

| | |
|---|---|
| Construct | a `RAND(seed)` predicate in a wrapping subquery |
| Seeded | yes, from the table's own name |
| `looks_like` sub-draw | `ORDER BY RAND() LIMIT`, **not** seeded |

MySQL has no `TABLESAMPLE`, so a fraction is expressed as a predicate over a seeded `RAND()`. The seed makes one statement reproducible; what makes a whole profile coherent is the materialized copy below, since MySQL does not document what multiple references to one seeded `RAND()` return. Across runs the draw is stable only while scan order is, so treat run-to-run agreement on a sampled MySQL table as likely rather than guaranteed.

The extra distinct-value draw that `inferred.looks_like` takes on top of the sampled rows takes no seed, because MySQL's behaviour for multiple seeded `RAND()` references in one statement is undocumented. So on MySQL a shape claim agrees with the rest of a sampled profile at the **population** level rather than row for row. In practice that means `sampled` and `matched` on a column's `inferred` block describe a draw the other statistics did not see.

## When dbprint writes

Only one thing makes dbprint write, and only under one condition: `materialize_sample` is on by default, and it fires **only for a table narrowed to a fraction of its rows**. A fraction has two sources — a rule's `sample`, and a `max_rows_scanned` ceiling resolving against the table's size, which is legal at connection and `defaults` level as well as inside a rule. A project that narrows nothing, which is what `dbprint init` scaffolds, never writes. A `filter` is a predicate rather than a fraction and never materializes.

Where it does fire, the drawn rows are copied once into a session-lifetime `TEMPORARY` table, and every statistics statement for that table reads the copy. That is what makes the numbers in one file describe one set of rows.

Where `CREATE TEMPORARY TABLES` is absent the run does not fail. It warns on stderr and falls back to re-evaluating the predicate per statement:

```
table 'my_db.germination_trial': could not materialize its sample of 0.25
(ProgrammingError: 1044 (42000): Access denied for user 'dbprint_ro'@'%' to database 'my_db');
each statistic for it is measured over its own draw of the rows
```

Note that MySQL reports this as a plain access-denied error rather than one naming temporary tables, so the message is worth reading in full before concluding the account lost its `SELECT`.

The fallback is not an equivalent path. Each statement then draws its own rows, so a column's listed value counts and the non-null figure they are a share of come from different reads, and the file can disagree with itself. Setting `materialize_sample: false` chooses that trade deliberately, which is the right call where policy forbids the tool writing anything at all — and the wrong one where it was chosen to avoid a grant.

## Reference

- Every configuration key: [Configuration](../CONFIG.md)
- What a `scope` block does to the numbers: [choosing what to profile](../guide/scoping.md) and [SPEC 2.2.8](../format/v1/SPEC.md#228-scope--statistics-over-part-of-a-table)
- What DDL normalization strips and preserves: [SPEC 2.1.3](../format/v1/SPEC.md#213-what-must-be-stripped-per-adapter)
