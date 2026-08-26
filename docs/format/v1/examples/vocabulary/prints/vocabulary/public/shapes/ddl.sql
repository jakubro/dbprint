CREATE TABLE public.shapes (
    row_id integer NOT NULL,
    pan character varying(24) NOT NULL,
    iban_value character varying(34) NOT NULL,
    bic_value character varying(11) NOT NULL,
    digest character varying(10) NOT NULL,
    mac_address character varying(20) NOT NULL,
    coordinates character varying(32) NOT NULL,
    resource_urn character varying(64) NOT NULL,
    duration character varying(24) NOT NULL,
    tz_name character varying(40) NOT NULL,
    currency character varying(4) NOT NULL,
    bearer_token character varying(256) NOT NULL,
    package_version character varying(24) NOT NULL,
    book_code character varying(17) NOT NULL,
    barcode character varying(14) NOT NULL,
    vehicle_id character varying(17) NOT NULL,
    device_id character varying(15) NOT NULL,
    event_timestamp character varying(10) NOT NULL,
    logged_at character varying(20) NOT NULL,
    tax_id character varying(20) NOT NULL,
    date_of_birth character varying(10) NOT NULL,
    blood_type character varying(3) NOT NULL,
    ethnicity character varying(24) NOT NULL,
    annual_salary character varying(10) NOT NULL
);

ALTER TABLE ONLY public.shapes
    ADD CONSTRAINT shapes_pkey PRIMARY KEY (row_id);
