-- Redshift live e2e schema. Exercises what the Postgres-backed shim cannot prove:
-- real SVV_REDSHIFT_COLUMNS/SHOW TABLE/SHOW CONSTRAINTS output, a SORTKEY, an
-- informational (never-enforced) FK, and a genuinely empty table's SVV_TABLE_INFO absence.

DROP VIEW IF EXISTS herbarium_late_view;
DROP TABLE IF EXISTS herbarium_sheet;
DROP TABLE IF EXISTS herbarium;
DROP TABLE IF EXISTS loan_request;
DROP TABLE IF EXISTS germination_reading;

CREATE TABLE herbarium (
  id INTEGER NOT NULL PRIMARY KEY,
  code VARCHAR(16) NOT NULL,
  name VARCHAR(64) NOT NULL
);

-- Late-binding: no `pg_rewrite`-equivalent bound query plan, so `depends_on` MUST be
-- omitted rather than published as `[]` - the ordinary way to define a Redshift view over
-- external or Spectrum tables.
CREATE VIEW herbarium_late_view AS SELECT id, code, name FROM herbarium WITH NO SCHEMA BINDING;

CREATE TABLE herbarium_sheet (
  id INTEGER NOT NULL PRIMARY KEY,
  herbarium_id INTEGER REFERENCES herbarium(id),
  status VARCHAR(16) NOT NULL,
  label VARCHAR(64) NOT NULL,
  logged_at TIMESTAMP NOT NULL
)
SORTKEY (herbarium_id, logged_at);

COMMENT ON TABLE herbarium_sheet IS 'Herbarium sheet catalog';
COMMENT ON COLUMN herbarium_sheet.label IS 'human-facing specimen label';

-- Never populated: proves a genuinely empty table is not lost from list_tables even though
-- SVV_TABLE_INFO carries no row for it at all (documented, unmeasured).
CREATE TABLE loan_request (
  id INTEGER NOT NULL PRIMARY KEY,
  herbarium_sheet_id INTEGER REFERENCES herbarium_sheet(id),
  requested_at TIMESTAMP NOT NULL
);

-- No declared key at all, and no single column is near-unique - the shape `probe_grain`
-- searches over. `seed_count` is constant, so its cardinality product with either other column
-- falls under the pruning floor and is never even probed; `(trial_id, reading_no)` is the one
-- pair the search reaches, and the seed makes it fully unique.
CREATE TABLE germination_reading (
  trial_id INTEGER NOT NULL,
  reading_no INTEGER NOT NULL,
  seed_count INTEGER NOT NULL
);
