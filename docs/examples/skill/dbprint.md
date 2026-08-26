# dbprint: reading a print from disk

A dbprint print lives under `prints/<connection_name>/` inside a project. Start at that
directory's `manifest.yaml`: its `tables` map is keyed by fully-qualified table name, and
each entry's `path` is where that table's own directory lives, relative to the connection
root.

Each table directory holds up to six files:

- `ddl.sql` - the table's DDL. Always present.
- `statistics.yaml` - per-column measurements. Catalog-only for plain views: columns and
  their SQL type, nothing measured.
- `relationships.yaml` - foreign keys in and out. May be absent for plain views.
- `description.md` - optional human-authored narrative.
- `statistics.annotations.yaml`, `relationships.annotations.yaml` - optional
  human-authored corrections and claims.

`statistics.yaml` wins over `description.md` on any question both answer - the prose may
describe a schema a later run already changed underneath it.

To find where a column is used, search every table's `relationships.yaml` for it as a
`column` entry, or scan the manifest's own table names - there is no cross-table index on
disk.

Read `prints/<connection_name>/reading.md` next. It teaches how to interpret what these
files say, not just where they are.
