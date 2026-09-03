-- Databricks live e2e schema. Exercises what the local PySpark+Delta substrate cannot prove:
-- real Unity Catalog information_schema output, a genuine CLUSTER BY liquid-clustering key,
-- a declared (not-enforced) foreign key, real per-object comments, and SHOW CREATE TABLE.

DROP TABLE IF EXISTS loan_request;
DROP TABLE IF EXISTS herbarium_sheet;
DROP TABLE IF EXISTS herbarium;

CREATE TABLE herbarium (
  id INT NOT NULL,
  code STRING NOT NULL,
  name STRING NOT NULL,
  CONSTRAINT herbarium_pk PRIMARY KEY (id)
) USING DELTA;

CREATE TABLE herbarium_sheet (
  id INT NOT NULL,
  herbarium_id INT,
  status STRING NOT NULL,
  label STRING NOT NULL COMMENT 'human-facing specimen label',
  logged_at TIMESTAMP NOT NULL,
  CONSTRAINT herbarium_sheet_pk PRIMARY KEY (id),
  CONSTRAINT herbarium_sheet_herbarium_fk FOREIGN KEY (herbarium_id) REFERENCES herbarium(id)
) USING DELTA CLUSTER BY (herbarium_id, logged_at)
COMMENT 'Herbarium sheet catalog';

-- Never populated: proves a genuinely empty table is not lost from list_tables even though
-- it carries no write-collected statistics at all.
CREATE TABLE loan_request (
  id INT NOT NULL,
  herbarium_sheet_id INT,
  requested_at TIMESTAMP NOT NULL,
  CONSTRAINT loan_request_pk PRIMARY KEY (id),
  CONSTRAINT loan_request_sheet_fk FOREIGN KEY (herbarium_sheet_id) REFERENCES herbarium_sheet(id)
) USING DELTA;
