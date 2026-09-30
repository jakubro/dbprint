-- Seed data: realistic distributions for the e2e test.

INSERT INTO public.herbarium (id, name, biome) VALUES
  ('00000000-0000-7000-8000-000000000001', 'Ferngrove', 'temperate'),
  ('00000000-0000-7000-8000-000000000002', 'Oakhaven', 'tropical'),
  ('00000000-0000-7000-8000-000000000003', 'Willowmere', 'temperate'),
  ('00000000-0000-7000-8000-000000000004', 'Cedarbrook', 'arid'),
  ('00000000-0000-7000-8000-000000000005', 'Birchfield', 'temperate');

-- 60 curators distributed across the 5 herbaria.
INSERT INTO public.curator (
  id,
  email,
  herbarium_id,
  traits,
  is_active,
  field_photo,
  viability_pct,
  created_at
)
SELECT
  ('00000000-0000-7000-8000-' || LPAD((1000 + gen.i)::TEXT, 12, '0'))::UUID AS id,
  'curator' || gen.i || '@example.com' AS email,
  ('00000000-0000-7000-8000-' || LPAD(((gen.i % 5) + 1)::TEXT, 12, '0'))::UUID AS herbarium_id,
  JSONB_BUILD_OBJECT('grade', gen.i % 3, 'flags', JSONB_BUILD_ARRAY('a', 'b')) AS traits,
  gen.i % 4 != 0 AS is_active,
  DECODE(LPAD(TO_HEX(gen.i), 4, '0'), 'hex') AS field_photo,
  ((gen.i % 7) * 25.50)::NUMERIC(10, 2) AS viability_pct,
  TIMESTAMP '2025-01-01' + gen.i * INTERVAL '1 day' AS created_at
FROM
  GENERATE_SERIES(1, 60) gen (i);

-- 30 fieldwork trips exercising the composite FK.
INSERT INTO public.fieldwork (id, curator_id, herbarium_id, rank, started_at)
SELECT
  ('00000000-0000-7000-8000-' || LPAD((2000 + rnk.rn)::TEXT, 12, '0'))::UUID AS id,
  rnk.id AS curator_id,
  rnk.herbarium_id,
  CASE rnk.rn % 3
    WHEN 0 THEN 'assistant'
    WHEN 1 THEN 'associate'
    ELSE 'lead'
  END AS rank,
  TIMESTAMP '2025-06-01' + rnk.rn * INTERVAL '1 day' AS started_at
FROM
  (
    SELECT
      cur.id,
      cur.herbarium_id,
      ROW_NUMBER() OVER (ORDER BY cur.id) AS rn
    FROM
      public.curator cur
    WHERE
      cur.herbarium_id IS NOT NULL
    LIMIT 30
  ) rnk;

-- 10 botanists with a self-referential mentor_id chain.
INSERT INTO public.botanist (id, name, mentor_id) VALUES
  (1, 'Director', NULL),
  (2, 'Taxonomist', 1),
  (3, 'Registrar', 1),
  (4, 'Senior Botanist', 2),
  (5, 'Botanist', 4),
  (6, 'Botanist', 4),
  (7, 'Botanist', 4),
  (8, 'Field Lead', 3),
  (9, 'Technician', 8),
  (10, 'Technician', 8);

-- 200 curation event rows; 80 distinct minute-rounded timestamps so created_at
-- is > enumeration_threshold, landing on temporal.
INSERT INTO public.curation_event (recorded_by, action, created_at, scheduled_at)
SELECT
  ('00000000-0000-7000-8000-' || LPAD(((gen.i % 60) + 1000)::TEXT, 12, '0'))::UUID,

  CASE gen.i % 4
    WHEN 0 THEN 'accession'
    WHEN 1 THEN 'mount'
    WHEN 2 THEN 'annotate'
    ELSE 'reshelve'
  END,

  TIMESTAMP '2026-06-01 12:00:00+00' - (gen.i % 80) * INTERVAL '1 minute',
  TIMESTAMP '3000-01-01 12:00:00+00' + (gen.i % 80) * INTERVAL '1 day'
FROM
  GENERATE_SERIES(1, 200) gen (i);

-- Refresh the materialized view so it has data when profiled.
REFRESH MATERIALIZED VIEW public.daily_viability_mv;

-- Force planner statistics so reltuples reflects the seeded row counts.
ANALYZE public.herbarium;
ANALYZE public.curator;
ANALYZE public.fieldwork;
ANALYZE public.botanist;
ANALYZE public.curation_event;
ANALYZE public.daily_viability_mv;
