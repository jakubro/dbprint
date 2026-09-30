CREATE MATERIALIZED VIEW seedbank.germination_by_taxon_mv AS
 SELECT t.taxon_id,
    (date_trunc('year'::text, (gt.started_on)::timestamp with time zone))::date AS trial_year,
    sum(gt.sown_count) AS total_sown,
    sum(gt.germinated_count) AS total_germinated
   FROM ((seedbank.germination_trial gt
     JOIN seedbank.accession a ON ((a.accession_id = gt.accession_id)))
     JOIN seedbank.taxon t ON ((t.taxon_id = a.taxon_id)))
  GROUP BY t.taxon_id, (date_trunc('year'::text, (gt.started_on)::timestamp with time zone))
  WITH NO DATA;

CREATE UNIQUE INDEX germination_by_taxon_mv_taxon_year_idx ON seedbank.germination_by_taxon_mv USING btree (taxon_id, trial_year);
