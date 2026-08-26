CREATE TABLE seedbank.accession (
    accession_id bigint NOT NULL,
    accession_code character varying(24) NOT NULL,
    taxon_id integer NOT NULL,
    collector_id uuid NOT NULL,
    vault_id integer NOT NULL,
    shelf_code character varying(8) NOT NULL,
    sheet_number character varying(12) NOT NULL,
    provenance_country character(2) NOT NULL,
    catalogue_url character varying(200) NOT NULL,
    traits jsonb,
    field_notes text NOT NULL,
    viability_pct numeric(5,2) NOT NULL,
    seed_count integer NOT NULL,
    collected_on date NOT NULL,
    received_at timestamp(0) with time zone NOT NULL,
    storage_temperature_c numeric(4,1)
);

COMMENT ON COLUMN seedbank.accession.taxon_id IS 'FK to taxon.taxon_id (verified on intake)';

ALTER TABLE ONLY seedbank.accession
    ADD CONSTRAINT accession_accession_code_key UNIQUE (accession_code);

ALTER TABLE ONLY seedbank.accession
    ADD CONSTRAINT accession_pkey PRIMARY KEY (accession_id);

CREATE INDEX accession_collector_id_idx ON seedbank.accession USING btree (collector_id);

CREATE INDEX accession_taxon_id_idx ON seedbank.accession USING btree (taxon_id);

CREATE INDEX accession_vault_shelf_idx ON seedbank.accession USING btree (vault_id, shelf_code);

ALTER TABLE ONLY seedbank.accession
    ADD CONSTRAINT accession_collector_id_fkey FOREIGN KEY (collector_id) REFERENCES seedbank.collector(collector_id) ON DELETE RESTRICT;

ALTER TABLE ONLY seedbank.accession
    ADD CONSTRAINT accession_taxon_id_fkey FOREIGN KEY (taxon_id) REFERENCES seedbank.taxon(taxon_id) ON DELETE RESTRICT;

ALTER TABLE ONLY seedbank.accession
    ADD CONSTRAINT accession_vault_shelf_fkey FOREIGN KEY (vault_id, shelf_code) REFERENCES seedbank.vault(vault_id, shelf_code) ON DELETE RESTRICT;
