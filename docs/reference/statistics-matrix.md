# Statistics required-field matrix

Generated from `spec/statistics_matrix.py` - do not edit by hand. Run `just docs` to
regenerate. [SPEC 2.2.3](../format/v1/SPEC.md#223-required--optional--forbidden-field-matrix-per-classification) is the
normative table this page mirrors; this page exists so a third-party producer can diff its
own emission logic against something checked cell for cell, rather than reconstructing the
matrix from prose that can silently move underneath it.

Every classification but `unsupported` requires eight base fields, listed once here rather
than in every row: `sql_type`, `nullable`, `null_count`, `null_rate`, `cardinality`,
`cardinality_ratio`, `cardinality_method`, `classification`. `unsupported` requires only the
first four - `cardinality`, `cardinality_ratio` and `cardinality_method` are FORBIDDEN on it
instead of required, so its row lists its actual required set in full rather than a diff
against the base. `rows_scanned` appears in neither column below - it is conditioned on the
file's own `scope` block, not on a classification.

A field absent from both columns for a classification is a footnoted case: `SPEC 2.2.3`'s own
footnotes (marked with a symbol) subtract a required field or add an exception under a stated
condition (an all-null column, redaction, a `sql_type` without day granularity). This page
carries only the unconditional rows; read the footnote text in SPEC for the conditional ones.

| Classification | Required beyond the base 8 | Forbidden |
|---|---|---|
| `boolean` | `values`, `values_coverage` | `distribution`, `empty_count`, `frequencies`, `freshness`, `length`, `mean`, `negative_count`, `normalized_cardinality`, `percentiles`, `quantized_count`, `range`, `sum`, `unrepresentable`, `zero_count` |
| `json` | - | `distribution`, `empty_count`, `frequencies`, `freshness`, `length`, `mean`, `negative_count`, `normalized_cardinality`, `percentiles`, `quantized_count`, `range`, `redacted`, `sketch`, `sum`, `unrepresentable`, `values`, `values_coverage`, `values_coverage_method`, `zero_count` |
| `foreign_key_candidate` | `distribution`, `length`, `values`, `values_coverage` | `empty_count`, `frequencies`, `freshness`, `mean`, `negative_count`, `percentiles`, `quantized_count`, `range`, `sum`, `unrepresentable`, `zero_count` |
| `categorical` | `distribution`, `length`, `values`, `values_coverage` | `empty_count`, `frequencies`, `freshness`, `mean`, `negative_count`, `percentiles`, `quantized_count`, `range`, `sum`, `unrepresentable`, `zero_count` |
| `temporal` | `distribution`, `frequencies`, `freshness`, `percentiles`, `quantized_count`, `range`, `values` | `empty_count`, `length`, `mean`, `negative_count`, `normalized_cardinality`, `sum`, `values_coverage`, `values_coverage_method`, `zero_count` |
| `numeric` | `distribution`, `frequencies`, `mean`, `negative_count`, `percentiles`, `quantized_count`, `range`, `sum`, `values`, `zero_count` | `empty_count`, `freshness`, `length`, `normalized_cardinality`, `unrepresentable`, `values_coverage`, `values_coverage_method` |
| `text` | `distribution`, `empty_count`, `length`, `values`, `values_coverage` | `frequencies`, `freshness`, `mean`, `negative_count`, `percentiles`, `quantized_count`, `range`, `sum`, `unrepresentable`, `zero_count` |
| `unsupported` | `classification`, `null_count`, `null_rate`, `nullable`, `sql_type` (fewer than the base 8) | `cardinality`, `cardinality_method`, `cardinality_ratio`, `distribution`, `empty_count`, `frequencies`, `freshness`, `inferred`, `length`, `mean`, `negative_count`, `normalized_cardinality`, `percentiles`, `quantized_count`, `range`, `redacted`, `sketch`, `sum`, `unrepresentable`, `values`, `values_coverage`, `values_coverage_method`, `zero_count` |
