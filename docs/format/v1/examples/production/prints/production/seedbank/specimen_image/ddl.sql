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

CREATE INDEX specimen_image_accession_id_idx ON seedbank.specimen_image USING btree (accession_id);

ALTER TABLE ONLY seedbank.specimen_image
    ADD CONSTRAINT specimen_image_accession_id_fkey FOREIGN KEY (accession_id) REFERENCES seedbank.accession(accession_id) ON DELETE CASCADE;
