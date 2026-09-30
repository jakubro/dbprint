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
    ADD CONSTRAINT collector_email_key UNIQUE (email);

ALTER TABLE ONLY seedbank.collector
    ADD CONSTRAINT collector_pkey PRIMARY KEY (collector_id);
