# germination_trial

One row per germination attempt run against a batch drawn from an accession.

## Relationships

- `accession_id` is a declared foreign key. `collector_id` is not - nothing here
  constrains it to `collector.collector_id`, so the connection between a trial and
  who ran it is inferred from the column name alone, not enforced by the database.

## Reading the statistics

- `germinated_count` is never greater than `sown_count`, but nothing in the schema
  guarantees that; a future batch of trials could violate it.
