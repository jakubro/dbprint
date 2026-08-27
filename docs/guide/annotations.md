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

Three things are available on an entry, and they answer different questions:

- **`note`** is free-form Markdown. The format imposes no structure on it.
- **`claims`** are predicates in the [assertion grammar](../ASSERTIONS.md), evaluated against the column's own statistics. A claim that the artifact contradicts is reported by `dbprint check`, so a note that has gone stale announces itself.
- **`values`** attach a note to one value in the column's own value list.

`relationships.annotations.yaml` additionally lets a human reject an inferred edge with a verdict — the one place an annotation overrules the producer, and only because inference is a guess rather than a measurement.

## The rule that decides who wins

An annotation may correct an inference, and may add knowledge a measurement cannot express. It does not overrule a measurement.

That is a real boundary, not a stylistic one. The artifact's arithmetic and catalog reads were taken at a known moment against the live database; prose was written at another moment by someone working from memory. Where the two disagree about something a statistic can answer, the statistic is the one with provenance. Where they disagree about something no statistic can answer, there is nothing to disagree with — that is exactly the space annotations exist to fill.

The file naming carries the same point: these are `.annotations.`, never `.override.`. [SPEC 2.7](../format/v1/SPEC.md#27-human-authored-annotation-files) states the rule normatively and [SPEC 2.4](../format/v1/SPEC.md#24-descriptionmd) states the precedence it restates.

## A new per-table annotation file is invisible until the next generate

This is the one that catches people, and it applies to the three per-table files.

Every consumer surface — `dbprint context`, the MCP server, the browsable print `dbprint docs` builds — resolves a **table's** artifacts through `manifest.yaml`. The manifest records which annotation files existed at the moment the print was generated. A `description.md`, `statistics.annotations.yaml` or `relationships.annotations.yaml` created afterwards is not in it, so nothing reads it.

`dbprint check` reports it as a `manifest.orphaned-artifact` warning. But warnings are reported by count rather than by code, so the message is easy to miss — see [gating CI](ci.md).

Run `dbprint generate --force` after creating one of those three. `--force` is the part that matters: a plain `generate` re-reads only tables outside their `max_age_days` window, which is seven days by default, and a table it skips keeps its previous manifest entry whole — artifacts map included — so the new file goes unrecorded exactly as before. Editing a file the manifest already records takes effect immediately; only creation needs the forced run.

`manifest.annotations.yaml` is the exception, and behaves the way you would expect: it is a connection-level file read by name from the print root rather than resolved through the manifest, so creating it takes effect at once, and the conformance validator schema-checks it whenever it is present.

## Where annotations surface

Not every annotation reaches every reader, and it is worth knowing which before choosing where to write something.

| Written in | Reaches |
|---|---|
| `description.md` | `dbprint context`, the MCP server, and the browsable print |
| `statistics.annotations.yaml` | the same three, per column |
| `relationships.annotations.yaml` | a verdict rejecting an inferred edge reaches all three; an edge a human adds that resolves against no producer edge reaches the structured renders only |
| `manifest.annotations.yaml` | the markdown render of `dbprint context` over two or more tables, and the MCP resource for the file itself |

Two things to plan around. A human-authored edge that matches no producer edge is dropped from the markdown render and from the browsable print, which both iterate the producer's own edges; it survives in `--format json` and `--format yaml`, which carry the annotation list whole.

And connection-level notes are connection-level, twice over: they ride a document header that only a multi-table render builds, and only the markdown render builds one at all. A fact a reader of one table needs belongs in that table's own annotation file, not hoisted up to the connection.
