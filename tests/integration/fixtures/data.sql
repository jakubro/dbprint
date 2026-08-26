-- Seed data: realistic distributions for the e2e test.

INSERT INTO public.herbarium (id, name, biome) VALUES
  ('00000000-0000-7000-8000-000000000001', 'Ferngrove',  'temperate'),
  ('00000000-0000-7000-8000-000000000002', 'Oakhaven',   'tropical'),
  ('00000000-0000-7000-8000-000000000003', 'Willowmere', 'temperate'),
  ('00000000-0000-7000-8000-000000000004', 'Cedarbrook', 'arid'),
  ('00000000-0000-7000-8000-000000000005', 'Birchfield', 'temperate');

-- 60 curators distributed across the 5 herbaria.
INSERT INTO public.curator (id, email, herbarium_id, traits, is_active, field_photo, viability_pct, created_at)
SELECT
    ('00000000-0000-7000-8000-' || lpad((1000 + i)::text, 12, '0'))::uuid AS id,
    'curator' || i || '@example.com' AS email,
    ('00000000-0000-7000-8000-' || lpad((((i % 5) + 1))::text, 12, '0'))::uuid AS herbarium_id,
    jsonb_build_object('grade', (i % 3), 'flags', jsonb_build_array('a', 'b')) AS traits,
    (i % 4 != 0) AS is_active,
    decode(lpad(to_hex(i), 4, '0'), 'hex') AS field_photo,
    ((i % 7) * 25.50)::numeric(10, 2) AS viability_pct,
    timestamp '2025-01-01' + (i * interval '1 day') AS created_at
FROM generate_series(1, 60) AS i;

-- 30 fieldwork trips exercising the composite FK.
INSERT INTO public.fieldwork (id, curator_id, herbarium_id, rank, started_at)
SELECT
    ('00000000-0000-7000-8000-' || lpad((2000 + i)::text, 12, '0'))::uuid AS id,
    u.id      AS curator_id,
    u.herbarium_id AS herbarium_id,
    CASE (i % 3) WHEN 0 THEN 'assistant' WHEN 1 THEN 'associate' ELSE 'lead' END AS rank,
    timestamp '2025-06-01' + (i * interval '1 day') AS started_at
FROM (
    SELECT id, herbarium_id, row_number() OVER (ORDER BY id) AS rn
    FROM public.curator
    WHERE herbarium_id IS NOT NULL
    LIMIT 30
) u
CROSS JOIN LATERAL (SELECT u.rn AS i) ix;

-- 10 botanists with a self-referential mentor_id chain.
INSERT INTO public.botanist (id, name, mentor_id) VALUES
  (1,  'Director',        NULL),
  (2,  'Taxonomist',      1),
  (3,  'Registrar',       1),
  (4,  'Senior Botanist', 2),
  (5,  'Botanist',        4),
  (6,  'Botanist',        4),
  (7,  'Botanist',        4),
  (8,  'Field Lead',      3),
  (9,  'Technician',      8),
  (10, 'Technician',      8);

-- 200 curation event rows; 80 distinct minute-rounded timestamps so created_at
-- is > enumeration_threshold, landing on temporal.
INSERT INTO public.curation_event (recorded_by, action, created_at, scheduled_at)
SELECT
    ('00000000-0000-7000-8000-' || lpad(((i % 60) + 1000)::text, 12, '0'))::uuid,
    CASE (i % 4) WHEN 0 THEN 'accession' WHEN 1 THEN 'mount' WHEN 2 THEN 'annotate' ELSE 'reshelve' END,
    timestamp '2026-06-01 12:00:00+00' - ((i % 80) * interval '1 minute'),
    timestamp '3000-01-01 12:00:00+00' + ((i % 80) * interval '1 day')
FROM generate_series(1, 200) AS i;

-- Refresh the materialized view so it has data when profiled.
REFRESH MATERIALIZED VIEW public.daily_viability_mv;

-- Force planner statistics so reltuples reflects the seeded row counts.
ANALYZE public.herbarium;
ANALYZE public.curator;
ANALYZE public.fieldwork;
ANALYZE public.botanist;
ANALYZE public.curation_event;
ANALYZE public.daily_viability_mv;
