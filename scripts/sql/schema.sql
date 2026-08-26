-- Reference-example schema: a seed-bank domain exercising every dbprint
-- format dimension (docs/format/v1/examples/production).
--
-- seedbank.taxon             -- self-referential FK (parent_taxon_id)
-- seedbank.collector         -- personal-data columns; redaction targets
-- seedbank.vault             -- composite PK (vault_id, shelf_code)
-- seedbank.accession         -- composite FK to vault; jsonb traits; FK to taxon/collector
-- seedbank.germination_trial -- FK to accession
-- seedbank.specimen_image    -- FK to accession
-- seedbank.storage_reading   -- declared range partition key; teaches physical_layout (2.2.11)
-- fixture.shape_probe        -- second schema; ipv4/json/array/bytea shape coverage

CREATE SCHEMA seedbank;
CREATE SCHEMA fixture;

CREATE TABLE seedbank.taxon (
    taxon_id integer NOT NULL,
    parent_taxon_id integer,
    scientific_name character varying(120) NOT NULL,
    rank character varying(16) NOT NULL,
    vernacular_name character varying(120) NOT NULL,
    description text NOT NULL,
    is_endangered boolean NOT NULL DEFAULT false,
    created_at timestamp(0) with time zone NOT NULL DEFAULT now()
);

ALTER TABLE ONLY seedbank.taxon
    ADD CONSTRAINT taxon_pkey PRIMARY KEY (taxon_id);

ALTER TABLE ONLY seedbank.taxon
    ADD CONSTRAINT taxon_scientific_name_key UNIQUE (scientific_name);

ALTER TABLE ONLY seedbank.taxon
    ADD CONSTRAINT taxon_parent_taxon_id_fkey FOREIGN KEY (parent_taxon_id)
    REFERENCES seedbank.taxon(taxon_id) ON DELETE SET NULL;

CREATE INDEX taxon_parent_taxon_id_idx ON seedbank.taxon USING btree (parent_taxon_id);

CREATE TABLE seedbank.collector (
    collector_id uuid NOT NULL,
    full_name character varying(120) NOT NULL,
    email character varying(320) NOT NULL,
    phone character varying(24) NOT NULL,
    institution character varying(120) NOT NULL,
    institution_email character varying(320) NOT NULL,
    street_address character varying(200) NOT NULL,
    postal_code character varying(12) NOT NULL,
    country_code character(2) NOT NULL,
    hired_on date NOT NULL
);

ALTER TABLE ONLY seedbank.collector
    ADD CONSTRAINT collector_pkey PRIMARY KEY (collector_id);

ALTER TABLE ONLY seedbank.collector
    ADD CONSTRAINT collector_email_key UNIQUE (email);

CREATE TABLE seedbank.vault (
    vault_id integer NOT NULL,
    shelf_code character varying(8) NOT NULL,
    site_name character varying(80) NOT NULL,
    target_temperature_c numeric(4,1) NOT NULL,
    opens_at time NOT NULL,
    closes_at time NOT NULL
);

ALTER TABLE ONLY seedbank.vault
    ADD CONSTRAINT vault_pkey PRIMARY KEY (vault_id, shelf_code);

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
    received_at timestamp(0) with time zone NOT NULL
);

ALTER TABLE ONLY seedbank.accession
    ADD CONSTRAINT accession_pkey PRIMARY KEY (accession_id);

ALTER TABLE ONLY seedbank.accession
    ADD CONSTRAINT accession_accession_code_key UNIQUE (accession_code);

ALTER TABLE ONLY seedbank.accession
    ADD CONSTRAINT accession_taxon_id_fkey FOREIGN KEY (taxon_id)
    REFERENCES seedbank.taxon(taxon_id) ON DELETE RESTRICT;

ALTER TABLE ONLY seedbank.accession
    ADD CONSTRAINT accession_collector_id_fkey FOREIGN KEY (collector_id)
    REFERENCES seedbank.collector(collector_id) ON DELETE RESTRICT;

ALTER TABLE ONLY seedbank.accession
    ADD CONSTRAINT accession_vault_shelf_fkey FOREIGN KEY (vault_id, shelf_code)
    REFERENCES seedbank.vault(vault_id, shelf_code) ON DELETE RESTRICT;

CREATE INDEX accession_taxon_id_idx ON seedbank.accession USING btree (taxon_id);
CREATE INDEX accession_collector_id_idx ON seedbank.accession USING btree (collector_id);
CREATE INDEX accession_vault_shelf_idx ON seedbank.accession USING btree (vault_id, shelf_code);

COMMENT ON COLUMN seedbank.accession.taxon_id IS 'FK to taxon.taxon_id';

CREATE TABLE seedbank.germination_trial (
    trial_id integer NOT NULL,
    accession_id bigint NOT NULL,
    collector_id uuid NOT NULL,
    medium character varying(40) NOT NULL,
    sown_count integer NOT NULL,
    germinated_count integer NOT NULL,
    started_on date NOT NULL,
    observed_at timestamp(0) with time zone NOT NULL
);

ALTER TABLE ONLY seedbank.germination_trial
    ADD CONSTRAINT germination_trial_pkey PRIMARY KEY (trial_id);

ALTER TABLE ONLY seedbank.germination_trial
    ADD CONSTRAINT germination_trial_accession_id_fkey FOREIGN KEY (accession_id)
    REFERENCES seedbank.accession(accession_id) ON DELETE CASCADE;

CREATE INDEX germination_trial_accession_id_idx
    ON seedbank.germination_trial USING btree (accession_id);

CREATE TABLE seedbank.specimen_image (
    image_id integer NOT NULL,
    accession_id bigint NOT NULL,
    storage_path character varying(200) NOT NULL,
    file_name character varying(80) NOT NULL,
    content_type character varying(60) NOT NULL,
    thumbnail_b64 text NOT NULL,
    byte_size bigint NOT NULL,
    captured_at timestamp(0) with time zone NOT NULL
);

ALTER TABLE ONLY seedbank.specimen_image
    ADD CONSTRAINT specimen_image_pkey PRIMARY KEY (image_id);

ALTER TABLE ONLY seedbank.specimen_image
    ADD CONSTRAINT specimen_image_accession_id_fkey FOREIGN KEY (accession_id)
    REFERENCES seedbank.accession(accession_id) ON DELETE CASCADE;

CREATE INDEX specimen_image_accession_id_idx
    ON seedbank.specimen_image USING btree (accession_id);

-- Declared but unattached: no partition yet holds a row, which is itself a
-- legitimate declared-but-empty state - the key is a schema fact independent
-- of data (SPEC 2.2.11), so this table stays at zero rows by design.
CREATE TABLE seedbank.storage_reading (
    reading_id bigint NOT NULL,
    vault_id integer NOT NULL,
    shelf_code character varying(8) NOT NULL,
    reading_date date NOT NULL,
    temperature_c numeric(4,1) NOT NULL
) PARTITION BY RANGE (reading_date);

-- No `ONLY`: a partitioned table holds no rows of its own, so a constraint on
-- it must cascade to every partition - `ONLY` is rejected here, unlike the
-- plain tables above.
ALTER TABLE seedbank.storage_reading
    ADD CONSTRAINT storage_reading_pkey PRIMARY KEY (reading_id, reading_date);

ALTER TABLE seedbank.storage_reading
    ADD CONSTRAINT storage_reading_vault_shelf_fkey FOREIGN KEY (vault_id, shelf_code)
    REFERENCES seedbank.vault(vault_id, shelf_code) ON DELETE RESTRICT;

CREATE TABLE fixture.shape_probe (
    probe_id integer NOT NULL,
    logger_ipv4 character varying(45) NOT NULL,
    json_text text NOT NULL,
    payload_bytes bytea,
    tag_list text[] NOT NULL
);

ALTER TABLE ONLY fixture.shape_probe
    ADD CONSTRAINT shape_probe_pkey PRIMARY KEY (probe_id);
