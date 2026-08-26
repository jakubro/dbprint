# collector

One row per field collector who has submitted material to the collection.

## Redaction

Five columns demonstrate the format's three primitives: `email`, `phone` and
`institution_email` are masked, `institution` is hashed, `street_address` is dropped.
Counts and distribution are unaffected on all five - only the literals are withheld.

`full_name` is the contrast: `inferred.sensitivity: personal_name` with no `redacted`
marker, because no rule names that category - the two category-keyed rules cover
`contact` and `online_identifier`, and the other two target columns by glob. Its values
ship in full, which is what `privacy.unredacted-sensitive` reports.

## Reading the statistics

- `institution` and `institution_email` are not guaranteed to share a domain - the
  data makes no claim they were ever validated against each other.
