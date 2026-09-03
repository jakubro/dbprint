-- BigQuery live e2e schema. Exercises what the emulator substrate cannot prove: real
-- INFORMATION_SCHEMA.TABLES.ddl output, a genuine CLUSTER BY key (is_partitioning_column
-- and clustering_ordinal_position both read correctly here, unlike the emulator), a declared
-- (not-enforced) primary key and foreign key, and a real per-table description.

DROP TABLE IF EXISTS loan_request;
DROP TABLE IF EXISTS herbarium_sheet;
DROP TABLE IF EXISTS herbarium;

CREATE TABLE herbarium (
  id INT64 NOT NULL,
  code STRING NOT NULL,
  name STRING NOT NULL,
  PRIMARY KEY (id) NOT ENFORCED
);

CREATE TABLE herbarium_sheet (
  id INT64 NOT NULL,
  herbarium_id INT64,
  status STRING NOT NULL,
  label STRING NOT NULL,
  logged_at TIMESTAMP NOT NULL,
  PRIMARY KEY (id) NOT ENFORCED,
  FOREIGN KEY (herbarium_id) REFERENCES herbarium(id) NOT ENFORCED
)
CLUSTER BY herbarium_id, logged_at
OPTIONS (description = 'Herbarium sheet catalog');

-- Never populated: proves a genuinely empty table is not lost from list_tables even though
-- it carries no write-collected statistics at all.
CREATE TABLE loan_request (
  id INT64 NOT NULL,
  herbarium_sheet_id INT64,
  requested_at TIMESTAMP NOT NULL,
  PRIMARY KEY (id) NOT ENFORCED,
  FOREIGN KEY (herbarium_sheet_id) REFERENCES herbarium_sheet(id) NOT ENFORCED
);
