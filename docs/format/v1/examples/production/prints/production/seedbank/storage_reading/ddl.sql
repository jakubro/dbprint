CREATE TABLE seedbank.storage_reading (
    reading_id bigint NOT NULL,
    vault_id integer NOT NULL,
    shelf_code character varying(8) NOT NULL,
    reading_date date NOT NULL,
    temperature_c numeric(4,1) NOT NULL
)
PARTITION BY RANGE (reading_date);

ALTER TABLE ONLY seedbank.storage_reading
    ADD CONSTRAINT storage_reading_pkey PRIMARY KEY (reading_id, reading_date);

ALTER TABLE seedbank.storage_reading
    ADD CONSTRAINT storage_reading_vault_shelf_fkey FOREIGN KEY (vault_id, shelf_code) REFERENCES seedbank.vault(vault_id, shelf_code) ON DELETE RESTRICT;
