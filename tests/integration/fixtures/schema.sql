-- e2e schema exercising every dimension the format cares about: composite and self-referential
-- FKs, an unsupported column, temporal data, a view, a matview, comments and a secondary index.

CREATE TABLE public.herbarium (
    id              uuid        PRIMARY KEY,
    name            varchar(64) NOT NULL,
    biome           varchar(16) NOT NULL,
    created_at      timestamp(0) WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE public.curator (
    id              uuid                       PRIMARY KEY,
    email           varchar(255)               NOT NULL UNIQUE,
    herbarium_id    uuid                       NULL REFERENCES public.herbarium(id) ON DELETE CASCADE,
    traits          jsonb                      NULL,
    is_active       boolean                    NOT NULL DEFAULT true,
    field_photo     bytea                      NULL,
    viability_pct   numeric(10, 2)             NOT NULL DEFAULT 0,
    created_at      timestamp WITH TIME ZONE   NOT NULL DEFAULT now(),
    UNIQUE (id, herbarium_id)
);

COMMENT ON TABLE public.curator IS 'Primary curator table';
COMMENT ON COLUMN public.curator.email IS 'contact email address';

CREATE INDEX curator_email_idx ON public.curator (email);

CREATE TABLE public.fieldwork (
    id              uuid PRIMARY KEY,
    curator_id      uuid NOT NULL,
    herbarium_id    uuid NOT NULL,
    rank            varchar(32) NOT NULL,
    started_at      timestamp WITH TIME ZONE NOT NULL DEFAULT now(),
    FOREIGN KEY (curator_id, herbarium_id) REFERENCES public.curator (id, herbarium_id) ON DELETE CASCADE
);

CREATE TABLE public.botanist (
    id              integer PRIMARY KEY,
    name            varchar(64) NOT NULL,
    mentor_id       integer NULL REFERENCES public.botanist(id) ON DELETE SET NULL
);

CREATE TABLE public.curation_event (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recorded_by     uuid NULL,
    action          varchar(32) NOT NULL,
    created_at      timestamp WITH TIME ZONE NOT NULL,
    -- Future-dated on purpose: a queue of future-dated work is ordinary data, and its
    -- age would otherwise go negative and fail the print's own schema.
    scheduled_at    timestamp WITH TIME ZONE NOT NULL
);

CREATE VIEW public.active_curators_v AS
    SELECT id, email FROM public.curator WHERE is_active = true;

CREATE MATERIALIZED VIEW public.daily_viability_mv AS
    SELECT date_trunc('day', s.started_at) AS day,
           count(*)                         AS fieldwork_count,
           sum(u.viability_pct)             AS viability_total
    FROM public.fieldwork s
    JOIN public.curator u ON u.id = s.curator_id
    GROUP BY 1;
