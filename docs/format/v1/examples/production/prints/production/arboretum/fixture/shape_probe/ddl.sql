CREATE TABLE fixture.shape_probe (
    probe_id integer NOT NULL,
    logger_ipv4 character varying(45) NOT NULL,
    json_text text NOT NULL,
    payload_bytes bytea,
    tag_list text[] NOT NULL
);

ALTER TABLE ONLY fixture.shape_probe
    ADD CONSTRAINT shape_probe_pkey PRIMARY KEY (probe_id);
