-- Seed data: three values per column, cycled by row ordinal so every column
-- is a real, low-cardinality categorical the producer classifies and reads
-- the same way on every regeneration. Every value is a published test
-- vector, standard registry entry, or invented placeholder - never framed
-- as observed: Luhn-valid non-issuable card numbers, documentation IBANs
-- (Wikipedia's ISO 4217/13616 examples), RFC 4122's own example UUID inside
-- a URN, locally-administered MAC addresses (RFC 7042), IANA zone names,
-- ISO 4217 currency codes, hand-built HS256-shaped JWTs carrying no real
-- signing key, semantic version strings including the tag-prefixed and
-- prerelease/build forms, published ISBN/EAN/UPC test identifiers, VINs
-- satisfying the federal VIN regulation's own check digit (49 C.F.R.
-- 565.15, including its X form), IMEIs whose Reporting Body Identifier
-- is genuinely allocated (GSMA's own IMEI allocation guidelines,
-- TS.06 Annex A) - one of them inside JCB's card-issuer range, which is the
-- value a card_number IIN table would still misclaim - epoch-second
-- instants chosen to avoid isbn's own check digit by coincidence, ISO 8601
-- datetime strings distinct from a bare date's shape, invented
-- tax-reference numbers for a national_id column name, invented birth dates
-- for a date_of_birth column name, standard ABO/Rh blood-type codes for a
-- health column, abstract placeholder group labels (never a real ethnicity,
-- religion or other protected-category name) for a demographic column, and
-- invented salary figures for an employment column.

INSERT INTO public.shapes (
    row_id, pan, iban_value, bic_value, digest, mac_address,
    coordinates, resource_urn, duration, tz_name, currency, bearer_token,
    package_version, book_code, barcode, vehicle_id, device_id, event_timestamp,
    logged_at, tax_id, date_of_birth, blood_type, ethnicity, annual_salary
)
SELECT
    i,
    (ARRAY['4111111111111111', '5500005555555559', '378282246310005'])[1 + (i % 3)],
    (ARRAY['DE89370400440532013000', 'GB82WEST12345698765432',
           'FR1420041010050500013M02606']
    )[1 + (i % 3)],
    (ARRAY['DEUTDEFF500', 'DEUTDEFF', 'SEEDGB2L'])[1 + (i % 3)],
    (ARRAY['#FF5733', '#1A2B3C', 'a3f9c2e'])[1 + (i % 3)],
    (ARRAY['02:00:00:00:00:01', '02:00:00:00:00:02', '02-00-00-00-00-03'])[1 + (i % 3)],
    (ARRAY['12.3456,-65.4321', '-40.1234,120.9876', '0.0,0.0'])[1 + (i % 3)],
    (ARRAY['urn:isbn:0451450523', 'urn:uuid:f81d4fae-7dec-11d0-a765-00a0c91e6bf6',
           'urn:example:resource']
    )[1 + (i % 3)],
    (ARRAY['P1Y2M3DT4H5M6S', 'PT30M', 'P4W'])[1 + (i % 3)],
    (ARRAY['Europe/London', 'America/New_York', 'UTC'])[1 + (i % 3)],
    (ARRAY['EUR', 'USD', 'JPY'])[1 + (i % 3)],
    (ARRAY[
        'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsInJvbGUiOiJ1c2VyIn0.'
            || 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk',
        'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI3Iiwicm9sZSI6ImFkbWluIn0.'
            || 'QqZ1M2xW0hK9pR3sT7uV2yA5bC8dE1fG4hJ6kL9mN0o',
        'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMDAwIiwicm9sZSI6Imd1ZXN0In0.'
            || 'Zx9Yw8Vu7Ts6Rr5Qq4Pp3Oo2Nn1Mm0Ll9Kk8Jj7Ii6Hh'
    ])[1 + (i % 3)],
    (ARRAY['v2.4.1', '3.0.0-rc.2', '1.0.0-alpha.beta'])[1 + (i % 3)],
    (ARRAY['9780306406157', '0132350882', '080442957X'])[1 + (i % 3)],
    (ARRAY['4006381333931', '5901234123457', '036000291452'])[1 + (i % 3)],
    (ARRAY['1HGCM82633A004352', 'JH4TB2H26CC000000', '1M8GDM9AXKP042788'])[1 + (i % 3)],
    (ARRAY['352099001761481', '490154203237518', '356938035643809'])[1 + (i % 3)],
    (ARRAY['1704067201', '1704067202', '1704067203'])[1 + (i % 3)],
    (ARRAY['2026-01-01T08:15:30', '2026-03-14T23:59:59', '2026-07-04T00:00:00'])[1 + (i % 3)],
    (ARRAY['XX1234567890', 'XX9876543210', 'XX5555555555'])[1 + (i % 3)],
    (ARRAY['1975-03-14', '1990-11-02', '2001-07-23'])[1 + (i % 3)],
    (ARRAY['A+', 'O-', 'B+'])[1 + (i % 3)],
    (ARRAY['Group A', 'Group B', 'Group C'])[1 + (i % 3)],
    (ARRAY['52000', '68500', '81250'])[1 + (i % 3)]
FROM generate_series(1, 40) AS i;
