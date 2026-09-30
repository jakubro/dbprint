-- e2e schema exercising every dimension the format cares about: composite and self-referential
-- FKs, an unsupported column, temporal data, a view, a matview, comments and a secondary index.

CREATE TABLE public.herbarium (
    id              UUID        PRIMARY KEY,
    name            VARCHAR(64) NOT NULL,
    biome           VARCHAR(16) NOT NULL,
    created_at      TIMESTAMP(0) WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE public.curator (
    id              UUID                       PRIMARY KEY,
    email           VARCHAR(255)               NOT NULL UNIQUE,
    herbarium_id    UUID                       NULL REFERENCES public.herbarium(id) ON DELETE CASCADE,
    traits          JSONB                      NULL,
    is_active       BOOLEAN                    NOT NULL DEFAULT TRUE,
    field_photo     BYTEA                      NULL,
    viability_pct   NUMERIC(10, 2)             NOT NULL DEFAULT 0,
    created_at      TIMESTAMP WITH TIME ZONE   NOT NULL DEFAULT NOW(),
    UNIQUE (id, herbarium_id)
);

COMMENT ON TABLE public.curator IS 'Primary curator table';
COMMENT ON COLUMN public.curator.email IS 'contact email address';

CREATE INDEX curator_email_idx ON public.curator (email);

CREATE TABLE public.fieldwork (
    id              UUID PRIMARY KEY,
    curator_id      UUID NOT NULL,
    herbarium_id    UUID NOT NULL,
    rank            VARCHAR(32) NOT NULL,
    started_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    FOREIGN KEY (curator_id, herbarium_id) REFERENCES public.curator (id, herbarium_id) ON DELETE CASCADE
);

CREATE TABLE public.botanist (
    id              INTEGER PRIMARY KEY,
    name            VARCHAR(64) NOT NULL,
    mentor_id       INTEGER NULL REFERENCES public.botanist(id) ON DELETE SET NULL
);

CREATE TABLE public.curation_event (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recorded_by     UUID NULL,
    action          VARCHAR(32) NOT NULL,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL,
    -- Future-dated on purpose: a queue of future-dated work is ordinary data, and its
    -- age would otherwise go negative and fail the print's own schema.
    scheduled_at    TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE VIEW public.active_curators_v AS
    SELECT id, email FROM public.curator WHERE is_active = TRUE;

CREATE MATERIALIZED VIEW public.daily_viability_mv AS
    SELECT DATE_TRUNC('day', s.started_at) AS day,
           COUNT(*)                         AS fieldwork_count,
           SUM(u.viability_pct)             AS viability_total
    FROM public.fieldwork s
    JOIN public.curator u ON u.id = s.curator_id
    GROUP BY 1;
