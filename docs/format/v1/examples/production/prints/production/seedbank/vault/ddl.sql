CREATE TABLE seedbank.vault (
    vault_id integer NOT NULL,
    shelf_code character varying(8) NOT NULL,
    site_name character varying(80) NOT NULL,
    target_temperature_c numeric(4,1) NOT NULL,
    opens_at time without time zone NOT NULL,
    closes_at time without time zone NOT NULL
);

ALTER TABLE ONLY seedbank.vault
    ADD CONSTRAINT vault_pkey PRIMARY KEY (vault_id, shelf_code);
