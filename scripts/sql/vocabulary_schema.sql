-- Vocabulary-example schema: one table, one column per looks_like value the
-- production example's seed-bank domain has no honest home for
-- (docs/format/v1/examples/vocabulary), plus one column
-- demonstrating the epoch_unit inferred field's per-value evidence rule -
-- its numeric-column bounds rule needs a cardinality this 40-row table
-- cannot carry above the enumeration threshold, so that arm is proven by
-- the engine test suite instead (see tests/engine/test_orchestrator.py) -
-- and five columns demonstrating the sensitivity: national_id,
-- sensitivity: date_of_birth, sensitivity: health, sensitivity:
-- demographic and sensitivity: employment categories, which the seed-bank
-- domain has no honest column for either.

CREATE TABLE public.shapes (
    row_id INTEGER NOT NULL,
    pan CHARACTER VARYING(24) NOT NULL,
    iban_value CHARACTER VARYING(34) NOT NULL,
    bic_value CHARACTER VARYING(11) NOT NULL,
    digest CHARACTER VARYING(10) NOT NULL,
    mac_address CHARACTER VARYING(20) NOT NULL,
    coordinates CHARACTER VARYING(32) NOT NULL,
    resource_urn CHARACTER VARYING(64) NOT NULL,
    duration CHARACTER VARYING(24) NOT NULL,
    tz_name CHARACTER VARYING(40) NOT NULL,
    currency CHARACTER VARYING(4) NOT NULL,
    bearer_token CHARACTER VARYING(256) NOT NULL,
    package_version CHARACTER VARYING(24) NOT NULL,
    book_code CHARACTER VARYING(17) NOT NULL,
    barcode CHARACTER VARYING(14) NOT NULL,
    vehicle_id CHARACTER VARYING(17) NOT NULL,
    device_id CHARACTER VARYING(15) NOT NULL,
    event_timestamp CHARACTER VARYING(10) NOT NULL,
    logged_at CHARACTER VARYING(20) NOT NULL,
    tax_id CHARACTER VARYING(20) NOT NULL,
    date_of_birth CHARACTER VARYING(10) NOT NULL,
    blood_type CHARACTER VARYING(3) NOT NULL,
    ethnicity CHARACTER VARYING(24) NOT NULL,
    annual_salary CHARACTER VARYING(10) NOT NULL
);

ALTER TABLE ONLY public.shapes
    ADD CONSTRAINT shapes_pkey PRIMARY KEY (row_id);
