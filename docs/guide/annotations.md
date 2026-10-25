# Annotating a print

Half of what a newcomer needs is measurable, and dbprint measures it. The other half is not in the database at all: which of two revenue columns every report actually uses, that a status value has been dead since a migration, that a join everyone writes one way is one-to-many more often than they expect.

Annotations are where that half lives. They sit beside the generated artifacts, in files the producer never writes to.

## The files dbprint will not overwrite

| File | Annotates | Keyed by |
|---|---|---|
| `description.md` | the table | nothing — free-form prose |
| `statistics.annotations.yaml` | `statistics.yaml` | column name |
| `relationships.annotations.yaml` | `relationships.yaml` | edge |
| `manifest.annotations.yaml` | `manifest.yaml` | nothing — the connection has nothing narrower to key against |

Create any of them by hand and every later `dbprint generate` preserves it. The naming is deliberate: a print root holds both producer-written and human-written files, and `<artifact>.annotations.yaml` makes authorship legible from the name alone.

Annotations outlive their table. When a table leaves the database, `generate` deletes its producer-written files and leaves the annotations where they are; `check` reports each as a `manifest.orphaned-artifact` warning, and a table that returns under the same name picks them up again.

The three YAML files carry a `format_version` header and a closed root — a key the format does not define is rejected rather than silently accepted, so a human always gets a signal when what they wrote reaches no consumer. `description.md` is unstructured prose and carries neither.

## What goes in one

```yaml
# prints/primary/seedbank/accession/statistics.annotations.yaml
format_version: 1
columns:
  mass_g:
    note: |
      Recorded at intake, not at storage. The storage figure is on
      `storage_reading` and the two diverge for anything dried on site.
    claims:
      null_rate: 0
  status:
    values:
      - value: pending
        note: Set by the intake form only; the batch importer writes `queued`.
```

Four things are available on an entry, and they answer different questions:

- **`note`** is free-form Markdown; the docs site shows raw HTML in it as text. The format imposes no structure on it.
- **`claims`** are predicates in the [assertion grammar](../ASSERTIONS.md), evaluated against the column's own statistics. A claim that the artifact contradicts is reported by `dbprint check`, so a note that has gone stale announces itself.
- **`values`** attach a note to one value in the column's own value list.
- **`derived_from`** names the columns this one is copied or computed from, as `{table, column}` pairs. A query-purpose context for the column carries each source's note and value notes beside its own, labelled with the source, so a pitfall written once on a source table reaches every table built from it. It is lineage only — no join is implied — and a source the print does not hold is reported by `dbprint check` as `annotations.unknown-source`. A connection configured with a `dbt_manifest` fills this in for every model column it can trace; an entry written here replaces the traced list for its column, and `derived_from: []` suppresses it.

`relationships.annotations.yaml` additionally lets a human reject an inferred or measured edge with a verdict — the one place an annotation overrules the producer, and only because such an edge is a guess rather than a constraint the catalog declares.

## The rule that decides who wins

An annotation may correct an inference, and may add knowledge a measurement cannot express. It does not overrule a measurement.

That is a real boundary, not a stylistic one. The artifact's arithmetic and catalog reads were taken at a known moment against the live database; prose was written at another moment by someone working from memory. Where the two disagree about something a statistic can answer, the statistic is the one with provenance. Where they disagree about something no statistic can answer, there is nothing to disagree with — that is exactly the space annotations exist to fill.

The file naming carries the same point: these are `.annotations.`, never `.override.`. [SPEC 2.7](../format/v1/SPEC.md#27-human-authored-annotation-files) states the rule normatively and [SPEC 2.4](../format/v1/SPEC.md#24-descriptionmd) states the precedence it restates.

## A new per-table annotation file is invisible until the next generate

This is the one that catches people, and it applies to the three per-table files.

Every consumer surface — `dbprint context`, the MCP server, the browsable print `dbprint docs` builds — resolves a **table's** artifacts through `manifest.yaml`. The manifest records which annotation files existed at the moment the print was generated. A `description.md`, `statistics.annotations.yaml` or `relationships.annotations.yaml` created afterwards is not in it, so nothing reads it.

`dbprint check` reports it as a `manifest.orphaned-artifact` warning. But warnings are reported by count rather than by code, so the message is easy to miss — see [gating CI](ci.md).

Run `dbprint generate` after creating or deleting one of those three. Each run rebuilds the manifest's artifacts map from the table directories, so a plain run declares a new file and forgets a deleted one without re-profiling a table that is still fresh. Editing a file the manifest already records takes effect immediately; only creating or deleting one needs the run.

`manifest.annotations.yaml` is the exception, and behaves the way you would expect: it is a connection-level file read by name from the print root rather than resolved through the manifest, so creating it takes effect at once, and the conformance validator schema-checks it whenever it is present.

## Writing them with an agent

Most of what belongs in an annotation is already written down somewhere — in the code that writes the table and the reports that read it — and an agent with that code and read-only database access can find it. `dbprint annotate <table>` prints a brief for one table: the steps of the [annotation skill](../examples/skill/dbprint-annotate.md), the table's `--purpose query` context, and the open questions the print measured but cannot answer, each naming the file that answers it. Pipe it into an agent CLI or paste it into a chat; dbprint launches no program and calls no model.

`dbprint annotate <table> --check` then lists every finding on the table's annotation files — a contradicted claim, an unknown source, a key the format rejects — and exits 1 while any remain. A file it refuses for not matching its schema is read by no consumer until it is fixed. The skill has the agent run it until it passes.

## Where annotations surface

Not every annotation reaches every reader, and it is worth knowing which before choosing where to write something.

| Written in | Reaches |
|---|---|
| `description.md` | `dbprint context`, the MCP server, and the browsable print |
| `statistics.annotations.yaml` | the same three, per column |
| `relationships.annotations.yaml` | a verdict rejecting an inferred or measured edge withholds the edge from `dbprint context` and the MCP server, and the browsable print draws it marked as rejected; an edge a human adds that resolves against no producer edge is listed on both tables it joins, everywhere, tagged `annotated`; a verdict addressing a declared edge is void on every surface, and `dbprint check` warns `annotations.verdict-on-declared-edge` |
| `manifest.annotations.yaml` | `dbprint context` and `get_table_context` on any table, every format (once per document over several tables), the browsable print's index page, linked from every table page, and the MCP resource for the file itself |

Write a fact once, at the narrowest level it is true: a convention many tables share — what a status code means, which unit every timestamp uses — belongs in `manifest.annotations.yaml`, where every reader of every table meets it, and a fact about one table belongs in that table's own file.
