CREATE TABLE seedbank.taxon (
    taxon_id integer NOT NULL,
    parent_taxon_id integer,
    scientific_name character varying(120) NOT NULL,
    rank character varying(16) NOT NULL,
    vernacular_name character varying(120) NOT NULL,
    description text NOT NULL,
    is_endangered boolean DEFAULT false NOT NULL,
    created_at timestamp(0) with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY seedbank.taxon
    ADD CONSTRAINT taxon_pkey PRIMARY KEY (taxon_id);

ALTER TABLE ONLY seedbank.taxon
    ADD CONSTRAINT taxon_scientific_name_key UNIQUE (scientific_name);

CREATE INDEX taxon_parent_taxon_id_idx ON seedbank.taxon USING btree (parent_taxon_id);

ALTER TABLE ONLY seedbank.taxon
    ADD CONSTRAINT taxon_parent_taxon_id_fkey FOREIGN KEY (parent_taxon_id) REFERENCES seedbank.taxon(taxon_id) ON DELETE SET NULL;
