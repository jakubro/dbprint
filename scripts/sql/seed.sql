-- Seed data: deterministic, derived from each row's ordinal so a
-- regeneration on any machine reproduces the same statistics byte-for-byte.

INSERT INTO seedbank.taxon (
    taxon_id, parent_taxon_id, scientific_name, rank, vernacular_name,
    description, is_endangered, created_at
)
SELECT
    i,
    CASE WHEN i <= 4 THEN NULL
         WHEN i <= 12 THEN (i - 5) % 4 + 1
         ELSE (i - 13) % 8 + 5 END,
    (ARRAY['Astro', 'Boreo', 'Calyxo', 'Dorypha', 'Elateri', 'Fenno', 'Glabro', 'Helio',
           'Ignisia', 'Juniperia', 'Kestrelia', 'Lunaris', 'Meridia', 'Noctura', 'Ombrella',
           'Pallasia', 'Quercinia', 'Riverwoodia', 'Silvana', 'Terracea']
    )[1 + ((i - 1) / 15)] || ' ' ||
    (ARRAY['montana', 'arvensis', 'sylvatica', 'littoralis', 'arenaria', 'palustris',
           'borealis', 'australis', 'maritima', 'glabrata', 'pallida', 'rupestris',
           'fragrans', 'umbrosa', 'viridis']
    )[1 + ((i - 1) % 15)],
    CASE WHEN i <= 4 THEN 'family' WHEN i <= 12 THEN 'genus' ELSE 'species' END,
    (ARRAY['Amber', 'Copper', 'Dusk', 'Ember', 'Frost', 'Golden', 'Ivory', 'Marsh', 'Rust',
           'Slate']
    )[1 + (((i - 1) % 200) / 20)] || ' ' ||
    (ARRAY['Bellflower', 'Clover', 'Coneflower', 'Fernweed', 'Foxglove', 'Gentian',
           'Harebell', 'Ironwort', 'Lupine', 'Marigold', 'Mullein', 'Nettle', 'Orchid',
           'Primrose', 'Ragwort', 'Sundrop', 'Thistle', 'Trillium', 'Violet', 'Yarrow']
    )[1 + ((i - 1) % 200) % 20],
    'This taxon is typically found in ' ||
    (ARRAY['lowland grassland', 'coastal dune scrub', 'montane forest understory',
           'riparian floodplain', 'semi-arid scrubland']
    )[1 + (((i - 1) % 250) / 50)] || '. Its ' ||
    (ARRAY['foliage', 'flowering habit', 'root structure', 'seed capsule', 'growth form']
    )[1 + ((((i - 1) % 250) % 50) / 10)] || ' tends to be ' ||
    (ARRAY['variable', 'consistent', 'moderately dense', 'sparse', 'robust', 'delicate',
           'uniform', 'irregular', 'compact', 'sprawling']
    )[1 + ((i - 1) % 250) % 10] ||
    ', and field observers note that it is stable across most collection sites.',
    (i % 23) = 0,
    TIMESTAMP WITH TIME ZONE '2012-01-01 00:00:00+00' + ((i * 37) % 4000) * INTERVAL '1 day'
FROM generate_series(1, 300) AS i;

INSERT INTO seedbank.collector (
    collector_id, full_name, email, phone, institution, institution_email,
    street_address, postal_code, country_code, hired_on
)
SELECT
    (lpad(to_hex(i), 8, '0') || '-' ||
     substr(md5('collector-' || i::text), 1, 4) || '-4' ||
     substr(md5('collector-' || i::text), 5, 3) || '-a' ||
     substr(md5('collector-' || i::text), 8, 3) || '-' ||
     substr(md5('collector-' || i::text || 'x'), 1, 12))::uuid,
    (ARRAY['Ada', 'Bram', 'Chidi', 'Dana', 'Elio', 'Fern', 'Gita', 'Hugo', 'Ines', 'Jamal',
           'Kira', 'Leif', 'Mira', 'Noor', 'Omar', 'Pia', 'Quinn', 'Rosa', 'Sami', 'Tara',
           'Uma', 'Vik', 'Wren', 'Xiomara', 'Yusuf', 'Zara', 'Anya', 'Bodhi', 'Cleo', 'Dov',
           'Esme', 'Finn', 'Greta', 'Hana', 'Ivo', 'Jael', 'Kato', 'Lena', 'Milo', 'Nadia']
    )[1 + ((i - 1) / 10)] || ' ' ||
    (ARRAY['Alvarez', 'Berg', 'Chen', 'Dubois', 'Eriksen', 'Farah', 'Gupta', 'Haugen',
           'Ibori', 'Jarvi']
    )[1 + ((i - 1) % 10)],
    'collector' || i::text || '@fieldwork.example',
    '+1-555-' || lpad((1000 + i)::text, 4, '0'),
    (ARRAY['Kestrel Botanical Trust', 'Meridian Seed Alliance',
           'Northbridge Conservation Institute', 'Harrowgate Flora Foundation',
           'Silverpine Research Station', 'Longmere Biodiversity Centre',
           'Ashcombe Seed Archive', 'Fenwick Field Station', 'Copperleaf Conservancy',
           'Riverstone Botanical Society', 'Dovetail Ecology Institute',
           'Thornfield Seed Trust', 'Bramblewood Research Centre',
           'Ironbark Conservation Alliance', 'Millrace Botanical Institute']
    )[1 + (i % 15)],
    (ARRAY['archive@kestrelbotanical.example', 'seeds@meridianalliance.example',
           'records@northbridgeci.example', 'contact@harrowgateflora.example',
           'info@silverpineresearch.example', 'archive@longmerebio.example',
           'records@ashcombearchive.example', 'info@fenwickfield.example',
           'contact@copperleafconservancy.example', 'records@riverstonebotanical.example',
           'info@dovetailecology.example', 'archive@thornfieldseed.example',
           'contact@bramblewoodresearch.example', 'records@ironbarkalliance.example',
           'info@millracebotanical.example']
    )[1 + (i % 15)],
    (ARRAY['14 Arbor Lane', '221 Harbor Road', '8 Prairie Court', '56 Cinder Bluff Way',
           '3 Willowmere Drive', '120 Northgate Avenue', '47 Sable Creek Road',
           '19 Marrow Hill Lane', '62 Kestrel Way', '8 Meridian Court',
           '154 Northbridge Road', '29 Harrowgate Lane', '71 Silverpine Drive',
           '5 Longmere Court', '98 Ashcombe Road']
    )[1 + (i % 15)],
    (ARRAY['SW1A 1AA', 'SW1A 2BB', 'SW1A 3CC', 'SW1A 4DD', 'SW1A 5EE', 'SW1A 6FF', 'SW1A 7GG',
           'SW1A 8HH', 'SW1A 9JJ', 'SW1A 0KK']
    )[1 + (i % 10)],
    (ARRAY['GB', 'US', 'CA', 'DE', 'FR', 'AU', 'NZ', 'IE', 'NL', 'ZA'])[1 + (i % 10)],
    DATE '2008-01-01' + (i % 6200)
FROM generate_series(1, 400) AS i;

INSERT INTO seedbank.vault (
    vault_id, shelf_code, site_name, target_temperature_c, opens_at, closes_at
)
SELECT
    ((k - 1) / 6) + 1,
    chr(65 + ((k - 1) % 6)),
    (ARRAY['Ridgeline Cold Store', 'Harbor Point Vault', 'Prairie Hollow Facility',
           'Cinder Bluff Store', 'Willowmere Vault', 'Northgate Cold Store',
           'Sable Creek Facility', 'Marrow Hill Vault']
    )[((k - 1) / 6) + 1],
    -20.0 + (((k - 1) / 6) % 3) * 0.5,
    CASE WHEN ((k - 1) / 6) % 2 = 0 THEN TIME '07:30:00' ELSE TIME '08:00:00' END,
    CASE WHEN ((k - 1) / 6) % 2 = 0 THEN TIME '16:30:00' ELSE TIME '17:00:00' END
FROM generate_series(1, 48) AS k;

INSERT INTO seedbank.accession (
    accession_id, accession_code, taxon_id, collector_id, vault_id, shelf_code,
    sheet_number, provenance_country, catalogue_url, traits, field_notes,
    viability_pct, seed_count, collected_on, received_at
)
SELECT
    i,
    'ACC-' || lpad(i::text, 6, '0'),
    1 + (i % 300),
    (lpad(to_hex(1 + ((i - 1) % 400)), 8, '0') || '-' ||
     substr(md5('collector-' || (1 + ((i - 1) % 400))::text), 1, 4) || '-4' ||
     substr(md5('collector-' || (1 + ((i - 1) % 400))::text), 5, 3) || '-a' ||
     substr(md5('collector-' || (1 + ((i - 1) % 400))::text), 8, 3) || '-' ||
     substr(md5('collector-' || (1 + ((i - 1) % 400))::text || 'x'), 1, 12))::uuid,
    (((1 + ((i - 1) % 48)) - 1) / 6) + 1,
    chr(65 + (((1 + ((i - 1) % 48)) - 1) % 6)),
    lpad((i % 900)::text, 3, '0'),
    (ARRAY['GB', 'US', 'CA', 'DE', 'FR', 'AU', 'NZ', 'IE', 'NL', 'ZA'])[1 + (i % 10)],
    'https://specimens.example.org/accession/' || i::text,
    CASE WHEN i % 17 = 0 THEN NULL
         WHEN i % 29 = 0 THEN
             ('{"habitat": "' ||
              (ARRAY['lowland grassland', 'coastal scrub', 'montane forest',
                     'riparian corridor', 'arid steppe']
              )[1 + (i % 5)] || '", "moisture_pct": ' || (10 + (i % 80))::text ||
              ', "reclassified_taxon_id": ' || (1 + ((i + 150) % 300))::text || '}')::jsonb
         ELSE ('{"habitat": "' ||
               (ARRAY['lowland grassland', 'coastal scrub', 'montane forest',
                      'riparian corridor', 'arid steppe']
               )[1 + (i % 5)] || '", "moisture_pct": ' || (10 + (i % 80))::text || '}')::jsonb
    END,
    'Collected from a ' ||
    (ARRAY['ridge slope', 'valley floor', 'streambank', 'open meadow', 'forest edge',
           'rocky outcrop', 'sand dune', 'wetland margin', 'hillside terrace',
           'coastal bluff', 'grazed pasture', 'abandoned orchard']
    )[1 + (((i - 1) % 1872) / 156)] || ' where ' ||
    (ARRAY['soil moisture was notably high', 'the canopy cover was sparse',
           'recent disturbance was visible', 'grazing pressure appeared light',
           'the substrate was rocky and shallow', 'standing water was present nearby',
           'the site had been recently burned', 'shade cover exceeded half the plot',
           'wind exposure was considerable', 'the slope aspect faced north',
           'leaf litter was thick underfoot', 'the area showed signs of erosion']
    )[1 + ((((i - 1) % 1872) % 156) / 13)] || '. The sample shows ' ||
    (ARRAY['good seed fill', 'some insect damage', 'uniform pod maturity',
           'a few immature capsules', 'strong stem vigor', 'minor fungal spotting',
           'dense flowering heads', 'pale coloration throughout',
           'notable size variation', 'healthy root development', 'light seed set',
           'above-average pod count', 'consistent capsule shape']
    )[1 + ((i - 1) % 1872) % 13] || '.',
    round((20 + (i % 350) * 0.23)::numeric, 2),
    50 + (i % 4000),
    DATE '2018-01-01' + (i % 2800),
    TIMESTAMP WITH TIME ZONE '2018-01-01 00:00:00+00' + (i % 2800) * INTERVAL '1 day'
        + (5 + (i % 20)) * INTERVAL '1 day'
FROM generate_series(1, 2500) AS i;

INSERT INTO seedbank.germination_trial (
    trial_id, accession_id, collector_id, medium, sown_count, germinated_count,
    started_on, observed_at
)
SELECT
    k,
    1 + (k % 320),
    (lpad(to_hex(1 + ((k * 3) % 400)), 8, '0') || '-' ||
     substr(md5('collector-' || (1 + ((k * 3) % 400))::text), 1, 4) || '-4' ||
     substr(md5('collector-' || (1 + ((k * 3) % 400))::text), 5, 3) || '-a' ||
     substr(md5('collector-' || (1 + ((k * 3) % 400))::text), 8, 3) || '-' ||
     substr(md5('collector-' || (1 + ((k * 3) % 400))::text || 'x'), 1, 12))::uuid,
    CASE WHEN k % 41 = 0 THEN 'control'
         ELSE (ARRAY['moist filter paper', 'agar medium', 'sand tray', 'vermiculite mix',
                     'sterile grit']
              )[1 + (k % 5)] END,
    20 + (k % 60),
    ((20 + (k % 60)) * (30 + (k % 60))) / 100,
    DATE '2020-01-01' + (k % 1800),
    TIMESTAMP WITH TIME ZONE '2020-01-01 00:00:00+00' + (k % 1800) * INTERVAL '1 day'
        + (14 + (k % 20)) * INTERVAL '1 day'
FROM generate_series(1, 900) AS k;

INSERT INTO seedbank.specimen_image (
    image_id, accession_id, storage_path, file_name, content_type, thumbnail_b64,
    byte_size, captured_at
)
SELECT
    k,
    1 + ((k * 5) % 280),
    'specimens/' || (2020 + (k % 6))::text || '/img_' || lpad(k::text, 6, '0') || '.jpg',
    'img_' || lpad(k::text, 6, '0') || '.jpg',
    (ARRAY['image/jpeg', 'image/png', 'image/tiff'])[1 + (k % 3)],
    encode(('specimen-photo-' || k::text)::bytea, 'base64'),
    40000 + ((k * 137) % 900000),
    TIMESTAMP WITH TIME ZONE '2020-01-01 00:00:00+00' + (k % 1800) * INTERVAL '1 day'
        + (k % 24) * INTERVAL '1 hour'
FROM generate_series(1, 700) AS k;

INSERT INTO fixture.shape_probe (probe_id, logger_ipv4, json_text, payload_bytes, tag_list)
SELECT
    p,
    (ARRAY['10.0.4.11', '10.0.4.12', '10.0.7.20', '10.0.7.21', '10.0.9.5', '10.0.9.6',
           '10.0.12.30', '10.0.12.31', '10.0.15.2', '10.0.15.3']
    )[1 + (p % 10)],
    '{"reading": ' || (10 + (p % 90))::text || '.' || lpad((p % 100)::text, 2, '0')
        || ', "unit": "C", "ok": true}',
    decode('deadbeef' || lpad(to_hex(p), 4, '0'), 'hex'),
    ARRAY['probe', 'sensor-' || (p % 5)::text]
FROM generate_series(1, 50) AS p;

CREATE VIEW seedbank.accession_summary AS
 SELECT
    a.accession_id,
    a.accession_code,
    t.scientific_name,
    t.vernacular_name,
    c.full_name AS collector_name,
    a.collected_on,
    a.viability_pct,
    ((a.accession_id - 1) % 900) + 1 AS germination_trial_id
   FROM seedbank.accession a
   JOIN seedbank.taxon t ON t.taxon_id = a.taxon_id
   JOIN seedbank.collector c ON c.collector_id = a.collector_id;

CREATE MATERIALIZED VIEW seedbank.germination_by_taxon_mv AS
 SELECT
    t.taxon_id,
    (date_trunc('year', gt.started_on))::date AS trial_year,
    sum(gt.sown_count) AS total_sown,
    sum(gt.germinated_count) AS total_germinated
   FROM seedbank.germination_trial gt
   JOIN seedbank.accession a ON a.accession_id = gt.accession_id
   JOIN seedbank.taxon t ON t.taxon_id = a.taxon_id
  GROUP BY t.taxon_id, (date_trunc('year', gt.started_on))
  WITH DATA;

CREATE UNIQUE INDEX germination_by_taxon_mv_taxon_year_idx
    ON seedbank.germination_by_taxon_mv USING btree (taxon_id, trial_year);

ANALYZE;
