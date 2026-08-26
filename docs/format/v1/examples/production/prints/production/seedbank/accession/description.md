# accession

One row per seed lot received into the collection.

## Provenance

- `provenance_country` records where the material was collected, which may differ from
  `collector.country_code` when a collector gathers material while travelling.
- `traits` is unstructured per-accession metadata (habitat, moisture at collection); the
  schema imposes no fixed shape on it, and it is absent on some rows.

## Reading the statistics

- `received_at` is always later than `collected_on` - the gap is transit and processing
  time, not measurement error.
- `viability_pct` is measured once at intake and is not re-tested on a schedule, so it
  reflects the sample's condition on arrival rather than its condition today.
