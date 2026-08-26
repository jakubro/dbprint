-- Schema drift applied between the generator's two runs; produces the
-- diff.yaml comparison in the committed print.

ALTER TABLE seedbank.accession ADD COLUMN storage_temperature_c numeric(4,1);
COMMENT ON COLUMN seedbank.accession.taxon_id IS 'FK to taxon.taxon_id (verified on intake)';
ANALYZE seedbank.accession;
