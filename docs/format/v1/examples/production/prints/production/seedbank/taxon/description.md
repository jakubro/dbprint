# taxon

One row per taxonomic rank the collection references - family, genus and species
share this table, distinguished by `rank`.

## Hierarchy

- `parent_taxon_id` points up the rank chain: a species points at its genus, a genus
  at its family, a family at nothing. `foreign_key_candidate` on this column reflects
  that self-reference.
- `scientific_name` is the stable identifier across a `vernacular_name` renaming -
  join on it, not on the common name, when tracking a taxon over time.

## Reading the statistics

- `is_endangered` reflects the classification on file at intake, not a continuously
  updated status.
