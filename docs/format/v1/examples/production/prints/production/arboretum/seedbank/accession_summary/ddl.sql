CREATE VIEW seedbank.accession_summary AS
 SELECT a.accession_id,
    a.accession_code,
    t.scientific_name,
    t.vernacular_name,
    c.full_name AS collector_name,
    a.collected_on,
    a.viability_pct,
    (((a.accession_id - 1) % (900)::bigint) + 1) AS germination_trial_id
   FROM ((seedbank.accession a
     JOIN seedbank.taxon t ON ((t.taxon_id = a.taxon_id)))
     JOIN seedbank.collector c ON ((c.collector_id = a.collector_id)));
