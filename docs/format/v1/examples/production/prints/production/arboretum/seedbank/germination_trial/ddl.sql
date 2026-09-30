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

CREATE INDEX germination_trial_accession_id_idx ON seedbank.germination_trial USING btree (accession_id);

ALTER TABLE ONLY seedbank.germination_trial
    ADD CONSTRAINT germination_trial_accession_id_fkey FOREIGN KEY (accession_id) REFERENCES seedbank.accession(accession_id) ON DELETE CASCADE;
