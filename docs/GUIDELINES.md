# dbprint Guidelines

> **Audience**: engineers working on this codebase. Not end users — nothing here describes
> how to use dbprint, and nothing here is part of its public contract.
>
> **Purpose**: Coding conventions, style rules, and development commands. For how the
> pieces fit together at runtime, see [ARCHITECTURE.md](ARCHITECTURE.md). For the on-disk
> format contract, see [format/v1/SPEC.md](format/v1/SPEC.md) (normative).

Conventions here reflect patterns **actually in the codebase**. Focus is on what is easy to
get wrong — not on what a competent Python engineer already does by reflex.

**Mandatory**: run `just check` after every change, before committing. Fix every issue.

**Comments describe current state, not change history.** No "was", "previously", "renamed
from", "now does X instead of Y". Git tracks history; a comment explains what exists now and
why it is non-obvious.

**No ephemeral coordination identifiers in source.** Issue numbers, dates, and tracker tags
do not belong in code, comments, docstrings, or test names — a reader of this repository cannot
resolve them. Code describes what is true now.

**Python prose is ASCII; string literals are not policed.** Docstrings and comments stay
inside `\x00-\x7F` — `just check` fails otherwise, naming the file, line and code point
(`tests/test_charset.py`). Spec citations are written `SPEC 2.2.3` / `MCP.md 3.1`, never with a
section sign. Prose dashes are the ASCII hyphen.

String literals are exempt as a **category**, not per line, and the boundary is drawn from the
AST rather than from `noqa` markers: a string that is the first statement of a module, class or
function is a docstring and is policed; every other string constant is data and is not. The
exemption is deliberate — `spec_ref` values are written to the terminal by `check` and the
format specification illustrates that field with a section sign, so rewriting them would both
change what users see and diverge from the normative document. Error-message literals reach
stderr for the same reason. This file and the rest of `docs/` are Markdown and out of scope.

Ruff's `RUF001`-`RUF003` are enabled but are **not** this gate: they flag only characters
confusable with ASCII, so they miss the em dash, the section sign and the box-drawing dash
entirely. They stay on as defence in depth against homoglyphs.

---

## 0. Development commands

All commands run from the repository root via [just](https://github.com/casey/just). `just`
(no args) lists them.

```bash
just check       # full pre-commit gate: lint + coverage-gated test. Run before every commit.
just fix         # auto-fix: ruff check --fix + ruff format + ty check --fix
just lint        # ruff check + ruff format --check + ty check
just test [ARGS] # pytest, no coverage; ARGS narrows (just test tests/engine -k diff)
just test-cov    # pytest with coverage (term-missing); what `check` runs
just docs        # regenerate every generated document: CLI.md, MCP.md's tool schemas,
                 # reference/conformance.md, the reading guide, the annotation schemas
just install     # uv sync --extra dev --extra mcp --extra docs
```

- **Python 3.13+.** Linting and formatting: `ruff` (line length 100, isort enabled). Type
  checking: `ty`. Tests: `pytest`.
- **No upper bound on the lint/type-check toolchain.** `ruff` and `ty` track their latest
  release deliberately — `ruff.toml` enables `default + extend-select`, never an enumerated
  rule list, so a version bump adopts whatever the new default is rather than freezing the set
  this project started with. A rule that turns out not to fit gets a stated per-site suppression
  (see §1 Error handling), never a config-level opt-out that silently re-narrows the set.
- **Coverage is gated at `check`, not at `test`.** `check` runs `test-cov`, which enforces
  `fail_under = 80`. `just test` deliberately skips coverage so a scoped run
  (`just test tests/cli`) measures nothing and fails nothing — coverage of the whole package
  is only meaningful for the whole suite.
- **A per-file coverage percentage disagreeing between two runs of an unchanged tree is
  worth a second run before it is trusted as a regression.** Re-run `test-cov` once more on
  the same tree; only a repeat disagreement is real. A single run's number is not
  self-certifying — coverage.py's own run-to-run stability on this suite has not been proven.
- **Optional extras**: `postgres` (`psycopg[binary]`), `mysql` (`mysql-connector-python`),
  `snowflake` (`snowflake-connector-python`), `mcp` (the `serve` command + MCP SDK), `docs`
  (the `dbprint docs` site: Flask, Markdown, inflect). `dev` pulls the test + lint toolchain.
  Vendor connectors are imported lazily so a base install never pays for them (see §1, Imports).
- **Virtualenv**: the `justfile` auto-routes `uv` to a container-local venv under `/tmp` when
  it detects a container; otherwise it uses `.venv/` in the working directory. Do not run
  `uv sync` directly inside a container — go through `just`.

---

## 1. Python conventions

These apply to every package. They are uniform across the codebase; deviations are bugs.

### Module layout and docstrings

- **Every `.py` file opens with a module docstring**, then a blank line, then
  `from __future__ import annotations`, then imports. The docstring's first line is a one-line
  summary; further lines are rare — see the terseness rule below.
- **Blank line after any docstring** — module, class, function — before the body.
- **One line is the default for every docstring and comment, not the floor.** Multi-line is
  earned by a caller's need — a non-obvious contract, an edge case, a `Raises` clause — never by
  a maintainer's argument for the code. Cite (`SPEC N.N`, `ARCHITECTURE.md §N`) instead of
  re-deriving what the citation already says; alternatives-considered reasoning and worked
  numeric examples belong in a test or nowhere, not in the docstring.
- **Class docstring wherever the name does not carry the role.** Always on dataclasses and
  records, whose field semantics and sort/identity rules need stating. One line unless those
  rules need explaining. A `Test*` class carries one only when the grouping's rationale is not
  already in its name; a `test_*` function's name is its description.
- **Function docstring on public functions**, sized to complexity: a one-liner when the name +
  signature already tell the story, multi-line only when behavior, an edge case, or a `Raises`
  contract cannot be read off the signature. Private helpers may omit the docstring when
  self-evident.
- Do not restate the signature or type hints in prose. Document *why* and *what is not
  obvious* — in the fewest words that carry it.
- **Comment/docstring lines stay under ~100 chars** (ruff's `line-length`). Compress wording
  first; if content genuinely doesn't fit, split into multiple tight lines (each a complete
  thought), never one crammed mega-line or word-wrapped prose.
- **Public definitions precede private ones at module level**, so the surface reads before
  the machinery. Two shapes are exempt because moving them breaks or obscures resolution:
  a private consumed at import time (a decorator target such as `cli/main.py`'s root group,
  a `Protocol` named in a signature below it, a helper feeding a module-level constant or a
  registration call), and `assertions/predicate.py`, whose `parse` / `_parse_*` /
  `evaluate` / `_eval_*` grouping mirrors the two branches a new predicate form must add and
  is worth more than the ordering.

### Type hints

- **`from __future__ import annotations` in every module that contains code.** `ruff.toml`
  sets `required-imports`, so `just check` fails any module that omits it. Package markers
  holding nothing but a docstring are the one exception — there is nothing to annotate, and
  ruff does not flag them.
- **PEP 604 unions and built-in generics**: `str | None`, `list[str]`, `dict[str, Any]`,
  `tuple[str, ...]`. Never `Optional`, `Union`, `typing.List`.
- **Return type on every function**, including `-> None`.
- **`Protocol` for structural contracts** (cursor seams, callback shapes) — method bodies are
  `...`. Use a `Callable[...]` type alias for factory hooks.
- `cast(...)` is the sanctioned escape hatch for narrowing catalog strings into a `Literal` or
  bridging a known signature mismatch. Use it with a short comment, not as a habit.

### Imports

```python
from __future__ import annotations

import json
from pathlib import Path

import yaml

from dbprint.config import StatisticsConfig
from .base import Adapter
```

- Four groups, blank-line separated: `__future__` -> stdlib -> third-party -> first-party.
  Intra-package relative imports continue the first-party group with no blank line between
  them (`ruff.toml` sets `no-lines-before = ["local-folder"]`, so isort enforces exactly
  this). Two blank lines before the first definition.
- **Cross-package imports are absolute** (`from dbprint.engine import ...`); **intra-package
  imports are relative** (`from .base import ...`), including the parent package
  (`from ..errors import ...`).
- Alias a sibling helper module only when its bare name would collide with a local binding:
  `from . import introspect as introspect_module` because `introspect` is also a variable
  there. Where no collision exists, import it bare (`from . import errors`).
- **Lazy / function-scoped imports** for two reasons only: (1) optional vendor extras — import
  the connector inside the factory via `importlib.import_module`, catch `ImportError`, and
  re-raise an actionable "install dbprint[<extra>]" message; (2) breaking an import cycle or
  deferring a heavy import off the hot path.
- Declare `__all__` in packages that expose a curated public surface. `F401` is ignored in
  `__init__.py` for exactly that reason and enforced everywhere else, so an import left
  behind by a deletion fails `just check` rather than lingering.

### Data structures

- **`@dataclass(frozen=True)` is the default** for records, value types, requests, parsed
  nodes, and config. Mutable dataclasses are reserved for accumulating result objects.
- **Mutable defaults via `field(default_factory=...)`** — never a bare `[]` / `{}`. Copy a
  module-level default constant inside the factory so it cannot be mutated.
- **Prefer immutable fields**: `tuple[str, ...]` over `list[str]` for things that should not
  change after construction.
- **`Literal` for closed string sets — never `enum.Enum`.** Declare the alias at module top in
  `PascalCase` (`Classification`, `Severity`, `RenderMode`). Vocabulary membership sets are
  `frozenset`.

### Naming

- Private helpers, attributes, and constants take a leading `_`.
- Module-level constants are `UPPER_SNAKE`; private ones `_UPPER_SNAKE`. Config defaults take a
  `_DEFAULT` suffix; compiled regexes take an `_RE` suffix.
- Type aliases are `PascalCase`; everything else is `snake_case`.
- Use keyword-only arguments (`*`) for helper options that would otherwise be positional noise.

### Whitespace and layout

Separate logical phases with blank lines; keep cohesive runs tight. The codebase is uniformly
spaced this way.

- A blank line **precedes every control block** (`if` / `for` / `while` / `with` / `try`) and
  **every `return` / `raise`** that is not the first statement in its block.
- A blank line **follows a block** before the next statement at the outer level.
- **Tight (no blank lines):** dataclass field lists, dict / list literals, comprehensions,
  runs of related simple assignments, and `elif` / `else` / `except` / `finally` chains.
- Use **`if` / `elif` / `else`** for mutually-exclusive return dispatch — not a sequence of
  bare `if cond: return` followed by a fallthrough `return`.
- **A construct split across lines puts one element per line.** Lists, dicts, sets, tuples,
  call arguments and parameter lists are either on a single line or exploded one per line
  with a trailing comma — never split at the brackets with the contents sharing an indented
  line. `COM812` and the formatter's magic trailing comma enforce this together, and
  `just fix` iterates because adding a comma re-wraps a construct and can expose a nested
  one. Comprehensions sit outside the rule: they carry no element commas, so the formatter
  splits them at the bracket and their shape is a review question.

```python
def classify(name: str, cardinality: int | None) -> Classification:
    """Return the first matching classification per the priority order."""

    sql_type = _base_type(name)

    if sql_type in _UNSUPPORTED:
        return "unsupported"
    elif cardinality is not None and cardinality <= ENUMERATION_THRESHOLD:
        return "categorical"
    else:
        return "text"
```

### Error handling

- **Raise vs return is deliberate, and load-bearing.** Raise for caller errors and structural
  problems the caller cannot recover from in line; return a typed outcome (an `Issue`, an
  `Outcome(malformed=True)`, a sentinel node) when the failure is data the caller aggregates.
  When in doubt, match the package's existing split (see §2).
- **Custom exceptions subclass the most precise stdlib base** (`ConfigError(ValueError)`,
  `IdentifierRejected(ValueError)`). Define them next to the code that raises them. There is no
  single global exception base.
- **Messages are lowercase, actionable, and quote the offending value with `!r`.** User-facing
  errors embed remediation (the install hint, a copy-pasteable `exclude:` snippet, the command
  to run).
- **`raise NewError(...) from exc`** whenever wrapping a caught exception — preserve the cause.
- **Run-all-then-report**: a failure in one unit (table, connection, check) never aborts its
  siblings; outcomes aggregate. See [ARCHITECTURE.md §9](ARCHITECTURE.md) for the level matrix
  and the exit-code model.
- **A broad `except Exception` is common here** — run-all-then-report, close-time cleanup, and
  a stdlib/protocol contract that forbids raising (`logging.Handler.emit`, an MCP tool call) all
  need one. Ruff's `BLE001`/`S110` flag every occurrence; where the catch is deliberate, suppress
  it at the site rather than fixing what is not broken: `except Exception:  # noqa: BLE001 - why
  this site is intentional`. The reason is mandatory and must be true of that site — identical
  wording across sites that genuinely share one idiom (three adapters degrading the same way on
  a failed temporal fetch) is fine; the same wording pasted onto a site it does not actually
  describe is not. No rule-level or file-level ignore, for `BLE001`/`S110` or any other rule —
  see §0's "no ceiling" note; suppressing a rule everywhere is indistinguishable from never
  adopting it.

### Logging

dbprint is a library; it stays quiet.

- ✅ The **engine** uses stdlib logging for the rare operational warning:
  `_LOG = logging.getLogger(__name__)` at module level.
- ✅ **adapters may log one DEBUG statement trace per `exec_query`/`pg_dump` call** — text,
  bound parameters (never merged into the text), elapsed time, row count where the driver
  reports one — tagged with the connection/table/operation context the engine sets via
  `adapters/trace_context.py`. This is the only logging this carve-out adds; it does not
  license further logging elsewhere in an adapter.
- 🚫 **config, conformance, spec, assertions, and mcp do not log at all; new adapter code adds
  no logging beyond that one statement trace.** Every other adapter concern communicates through
  return values and raised exceptions.
- ✅ **User-facing output is the CLI's job**: Rich `Console` for TTY rendering, `click.echo(...,
  err=True)` for messages and warnings to stderr.
- 🚫 Never `print()` — except protocol I/O (the MCP stdio transport) and the CLI's piped-stream
  writes.

---

## 2. Package conventions

Each package's runtime role is documented in [ARCHITECTURE.md](ARCHITECTURE.md); below are the
style rules that ride on top. Dependencies flow downward only — see
[ARCHITECTURE.md §1](ARCHITECTURE.md) for the layering rule.

### `adapters` — talk to one database

- ✅ Implement the `Adapter` ABC ([ARCHITECTURE.md §2](ARCHITECTURE.md)); return typed
  intermediate records (`TableMeta`, `ColumnStats`, ...), **never** artifact-shaped dicts.
- ✅ Honor the full abstract signature even when ignoring arguments (`del` the unused params
  with a comment rather than dropping them).
- ✅ Parameterize values, using the placeholder style the driver's configured `paramstyle`
  expects (`%s` for psycopg and mysql-connector; `?` for the Snowflake connector, which is
  opened with `paramstyle="qmark"`). **Hand-quote identifiers** through the quoting helpers
  before interpolating them into SQL. Never interpolate raw values; row limits and sample
  sizes come from validated config integers, not from user input.
- ✅ **`scope.filter` is the one interpolated string, and the exception is deliberate.** It
  cannot be bound: a placeholder carries a value, and this is a boolean expression — and
  [SPEC 2.2.8](format/v1/SPEC.md) requires it recorded verbatim and forbids a producer
  parsing, rewriting or validating it, so there is nothing to hand a driver. It reaches SQL
  in exactly one place per adapter, the source expression every statistics query selects
  `FROM`. The trust boundary is `.dbprint.yaml`, which already names the connection whose
  credentials the run uses: anyone who can edit it can already reach the database. Nothing
  else in that block relaxes — the sample fraction is a float validated into `(0, 1]` at
  load, and a new scope key is parameterized or validated unless it is a predicate too.
- ✅ Gate vendor connectors behind a lazy import + extras message; expose a `cursor_factory`
  seam for databases that cannot run in-process (see [ARCHITECTURE.md §2](ARCHITECTURE.md)).
- ✅ **An adapter's SQL is validated against its own engine's dialect, and that is not
  optional.** Each adapter package declares a `DIALECT` (vendor + driver paramstyle);
  `tests/adapters/test_dialect_guard.py` sweeps every statement the adapter emits from a
  recording proxy on its live cursor and rejects syntax the declared vendor does not accept.
  A new adapter adds its declaration, and the sweep covers it. Two things the guard needs to
  keep working: the contract fixture must stay wide enough for the classification-specific
  statistics branches to run (they emit nothing on a three-row table, so their SQL would go
  unchecked), and a new vendor-exclusive construct belongs in `adapters/dialect.py`'s support
  table.
- 🚫 Do not stamp `classification` onto stats — the engine applies the spec priority. Do not
  log anything beyond the one statement trace `exec_query`/`pg_dump` already emit (see §0
  Logging). Do not catch user-SQL execution errors (let them propagate so the assertion layer
  surfaces them).

> **What the dialect guard does not prove.** It proves an adapter emits syntax its own engine
> accepts and that every branch producing SQL runs. It cannot prove the engine accepts the
> statement — only running it against that account does.
> The Snowflake adapter runs against an in-process duckdb substrate, so a green suite is
> evidence of dialect conformance, never of live validation.

### `cli` — parse, dispatch, render

- ✅ `import rich_click as click`. One module per subcommand under `commands/`, each exporting a
  `<verb>_command` callback wired into the root group in `main.py` via `add_command`.
- ✅ A command callback's docstring **is** its `--help` text, and it carries every section:
  a one-line summary, then purpose + side effects, then `**Arguments:**` documenting
  positionals (Click renders no `help=` on arguments), then `**Exit codes:**` and
  `**Examples:**`. Every one of these is a Markdown heading inside the docstring itself:
  `main.py` sets `rich_click.TEXT_MARKUP = "markdown"`, so the docstring is the entire help
  text and Markdown renders as written. Make every option `help=` concrete, with an inline
  example where the value is non-obvious. The reference at `docs/CLI.md` is generated from
  `--help` (`just docs`) and golden-tested, so help is the single source of truth - never
  hand-edit `docs/CLI.md`.
- ✅ Exit via `ctx.exit(max(codes))` — engine exit codes are data on the result objects; the
  command aggregates them ("worst code wins"). Never `sys.exit`.
- ✅ Detect TTY in exactly one place (`rendering.resolve_render_mode`, the only `isatty()`
  caller). The TTY/piped branch belongs only at renderer selection. Machine formats and
  `--output` writes always force piped.
- ✅ Catch per-connection setup failures and turn them into a synthetic result with the
  connection exit code; never let one connection abort the run.
- 🚫 No SQL, adapter, or engine logic in the CLI. It builds an adapter via the registry,
  constructs `Engine`, and renders the result.

### `config` — load and validate

- ✅ Pure parsing and validation of `.dbprint.yaml`, the connections file, and `.env`. Frozen
  dataclasses; `tuple` fields; `default_factory` for nested config.
- ✅ Validate by raising `ConfigError` with a contextual, actionable message. Accumulate every
  unresolved credential key and raise once, listing them all.
- 🚫 No database I/O. The only filesystem access is config discovery (walk up for
  `.dbprint.yaml`).

### `conformance` — validate a print

- ✅ `validate_print()` returns `list[Issue]`, ordered (it relies on `Issue` being
  `frozen=True, order=True` with field order == sort order). It **never raises for a
  format-level problem** — malformed YAML becomes a `schema.invalid-yaml` Issue.
- ✅ Raise only for filesystem/IO problems it genuinely cannot inspect.
- ✅ A validator recomputes through a shared `spec/` rule only when that rule is pure
  arithmetic. Where the rule clamps or bounds its output (`coverage_share` is the one today),
  the validator reads the operands the clamp was applied to instead — recomputing through the
  clamp compares a bounded value against itself and can never disagree.
- ✅ Issue codes are dotted namespaces (`schema.*`, `stats.*`, `version.*`); `spec_ref` cites
  the section (`§2.2.3`). Severity is exactly `error` or `warning` — there is no `info`.

### `spec` — normative helpers

- ✅ Pure, deterministic, no I/O, no state (`classification.py`, `looks_like.py`). The branch
  order **is** the spec priority — first match wins; do not reorder.
- ✅ `FORMAT_VERSION` lives in `spec/v1/__init__.py` as the single source. JSON Schemas load
  once via `importlib.resources` (shipped in the wheel).

### `engine` — orchestrate

- ✅ `generate()` and `compute_diff()` share the private `_run_extraction()` pipeline; the
  divergence is the write-or-not boundary ([ARCHITECTURE.md §3](ARCHITECTURE.md)). Frozen
  request dataclasses in, result dataclasses out, `Literal` status fields.
- ✅ Per-table writes are atomic ([ARCHITECTURE.md §4](ARCHITECTURE.md)); never overwrite
  user-authored `description.md` or `statistics.annotations.yaml`.
- ✅ **Artifact YAML targets YAML v1.2's core schema**, not PyYAML's own (1.1-based) default
  resolver: `yaml_dumper.py`'s `str` representer quotes a scalar whenever a 1.2 parser would
  retype it (null/bool/int/float), covering the exponent-form float production 1.1 misses.
  Adding a representer for a new scalar type never bypasses this - route it through
  `represent_scalar`, not a hand-built string.
- 🚫 The engine knows the on-disk layout but not Click — it returns typed results, it does not
  render.

### `assertions` — the check DSL

- ✅ Structural problems raise `ParseError` (in `parser.py` only). Bad predicates and failed
  checks **return** (`MalformedPredicate`, `Outcome(malformed=True)`, an `Issue`) — the
  evaluators never raise for assertion failure.
- ✅ The predicate AST is a closed set of frozen dataclasses unified by a `Predicate` union
  alias (not an enum, not a class hierarchy). Adding a form means: new dataclass, extend the
  union, add the `_parse_*` branch **and** the `_eval_*` branch.
- ✅ Every evaluator's `evaluate()` sorts its `Issue` list before returning — determinism is part
  of the contract. Issue codes come from the codes module, never hardcoded strings.

### `mcp` — serve prints

- ✅ `resources.py` and `tools.py` are **pure**: `(state, *args) -> dict | dataclass`, no SDK
  import, no `async`. `server.py` is the only file that imports `mcp` and wires the pure
  handlers to the SDK ([ARCHITECTURE.md §11](ARCHITECTURE.md)).
- ✅ The `[mcp]` extra is gated by the CLI caller, not re-checked in `server.py`. HTTP transport
  binds to loopback only (enforced upstream). Re-read from disk on every call — no caching.
- ✅ Errors are factory functions returning a frozen `McpError` (which subclasses `Exception`);
  codes are restricted to the `JsonRpcCode` `Literal`. Both entry points surface the typed error
  to the client: `read_resource` translates it into the SDK's own error object
  (`mcp.shared.exceptions.MCPError(code, message)`), and `call_tool` catches it explicitly and
  returns `CallToolResult(..., is_error=True)` itself - the SDK does not wrap a raised exception
  into one.

---

## 3. Testing

The pytest tree under `tests/` mirrors the source layout. `just check` enforces 80% coverage
over the whole suite; `just test` runs without coverage so scoped runs stay usable.

### Principles

- **Test behavior, not implementation.** Call the public surface and assert observable
  outcomes. Use real dependencies where practical (the contract suite runs against real
  Postgres/MariaDB; Snowflake uses an in-process duckdb substrate —
  [ARCHITECTURE.md §10](ARCHITECTURE.md)).
- ✅ **Always** assert an observable outcome — never "does not raise" without an explicit
  assertion. Parameterize tests that differ only in input values.
- 🚫 **Do not test language guarantees** — dataclass defaults, `default_factory` execution,
  enum/`Literal` membership, `isinstance` on typed constructs. The declaration is the spec.
- 🚫 **Do not mock the only collaborator and assert its return reaches the response** — that
  tests the framework, not the code.
- 🚫 **Do not re-implement production logic in the test.** Call the real code path.
- 🚫 **Do not derive a test's expected value from the same artifact or object the assertion checks** — parsing a config file and asserting a field read from that file, or reading a live object's own attribute and checking rendered output contains it. Use an independently authored literal instead.
- **A consumer surface ships with register entries, not a byte-golden alone.**
  `tests/consumer/register.py` is the shared claims register — one entry per consumer-visible
  state that has produced a wrong rendering, with the obligation every surface owes it. A new
  surface (`dbprint context`, an MCP channel, the docs site, the guide, or one added later)
  declares the register entries it asserts in a module-level `COVERS` frozenset **and adds
  itself to `_SURFACES` in `tests/consumer/test_register_coverage.py`** — that dict is
  hand-maintained, not a package scan, so a surface left out of it is never checked at all.
  Once listed, it fails short of the full register by name, and fails every surface a new
  register entry has not yet reached. A byte-golden still pins layout; the register is what
  pins truth.

The acid test: if you replaced the implementation with a no-op or a wrong implementation, would
the test fail? If not, it is not earning its keep.

### Structure

- **`conformance` is the format gate.** Tests validate generated prints through
  `validate_print()` and assert zero `error`-severity issues; the suite is also consumable by
  external CI ([ARCHITECTURE.md §1](ARCHITECTURE.md)).
- **`MockAdapter`** (`adapters/mock.py`) drives engine tests without a database. Every adapter
  runs the shared contract battery (`tests/adapters/test_base_contract.py`) against its
  substrate.
- **Fixture objects are named from the reference example's seed-bank domain.** The canonical
  objects come from `scripts/sql/schema.sql` — `seedbank.taxon`, `seedbank.collector`,
  `seedbank.vault`, `seedbank.accession`, `seedbank.germination_trial`,
  `seedbank.specimen_image`, `seedbank.storage_reading`, `fixture.shape_probe` — and the same
  domain already covers `curator`, `herbarium`, `herbarium_sheet`, `viability_check`,
  `botanist`, `curation_event`, `active_curators`, `garden`, `cultivar`, `fieldwork`,
  `sowing_trial`, `germination_reading`, `curator_profile`, `curator_note`, `batch`,
  `fixture.staging`, `fixture.contact_probe`, `fixture.probe_target` and
  `fixture.type_probe`. `arboretum` is the vocabulary's word for
  the database-level segment of a three-part FQN, a tier none of the above names. Two
  things stay: the schema names a database itself supplies (`public`,
  `information_schema`, `pg_catalog`, `mysql`, `sys`), and structural placeholders carrying
  no domain at all (`public.t`, `a`/`b`/`c`, `wide`, `narrow`). `loan` is excluded, not
  sanctioned — a specimen loan is real vocabulary here, but the word reads as financial on
  sight; qualify it (`specimen_loan`) or reach for a different object. Extend the vocabulary
  here as a fixture needs it — add the word in the same commit that uses it. `field_site`,
  `field_log`, `field_round` and `sample` are further structural table names, carrying no
  fixed shape of their own.
- **A view suffixes the object it reads: `_v`, or `_mv` when materialized.** `active_curators_v`,
  `specimen_loan_v`, `germination_by_taxon_mv`. The stem stays a sanctioned name, so the suffix
  adds a shape rather than inventing vocabulary.
- **A pluralized object name is a divergence everywhere except where the plural is the subject.**
  Fixtures are singular (`seedbank.taxon`, `seedbank.botanist`), because a plural reads as a
  different object from the one the vocabulary sanctions. The one exception is
  `tests/engine/test_inference.py`'s `public.curators`, where the foreign-key stem rule under
  test is what strips the `s` — renaming it would delete the case.
- **A column is named by the role a fixture needs it to play**, so a rename cannot silently
  move a test's subject: a foreign-key-shaped id is `herbarium_id`; a low-cardinality
  categorical is `rank`; a nullable integer is `seed_count`; a nullable timestamp is
  `withdrawn_at`; a non-nullable future-dated timestamp is `matures_at`; a self-referential FK
  is `mentor_id`; an id column carrying no constraint is `recorded_by`; an unsupported-type
  binary blob is `field_photo`; a free-text column is `field_notes`; a numeric measure is
  `viability_pct`; `herbarium`'s own climate categorical is `biome`; a digit-shaped numeric
  identifier carrying no sensitivity token of its own is `accession_number`; a column existing
  only as an annotation, with no statistics of its own, is `shelf_location`; a free-typed
  column with no role beyond filling out a shape (a composite-key member, a column outside a
  declared key) is `condition`, `cohort` or `plot`; a personal-data column is one of
  `seedbank.collector`'s own — `full_name`, `email`, `phone`, `institution`,
  `street_address`, `postal_code`, `country_code`.
  `tests/spec/test_sensitivity.py`'s corpus is not fixture vocabulary: its names are the
  classifier's own input and stay real-world PII, finance, health, HR and telecom words; a
  shipped example needing a materialized sensitivity case already has one in
  `vocabulary_schema.sql`.
- **A fixture name implies a shape.** A test may use a real seed-bank object's name only with
  that object's real columns; a fixture needing a different shape invents a different object
  rather than borrowing a name it does not honour.
- **Writes go to an ephemeral scratch tree** (pytest tmp paths) — never the working tree. The
  live clusters build under the first system temp directory their own account can write to,
  and a session-scoped guard fails the run if anything the suite named appears outside it.
- **The suite sandboxes itself outside a container.** `pytest_configure` re-execs under
  `bwrap` with the host bound read-only and only the scratch tree writable, so a fixture
  writing to an unexpected path fails instead of reaching the machine. Inside a container it
  is skipped deliberately — the blast radius is already the container, and namespace creation
  is usually unavailable there. A missing `bwrap` outside one raises rather than falling back:
  a sandbox that quietly does not run is worse than none, because it is trusted. CI gains
  nothing from this, since the workflow itself runs in a container.

---

## Cross-references

- [ARCHITECTURE.md](ARCHITECTURE.md) — module map, layering, adapter protocol, engine flow,
  write contract, error model, exit codes, test substrates.
- [format/v1/SPEC.md](format/v1/SPEC.md) — normative on-disk format specification.
- [CONFIG.md](CONFIG.md) — every `.dbprint.yaml` and credentials key, with defaults.
- [MCP.md](MCP.md) — MCP server specification.
- [ASSERTIONS.md](ASSERTIONS.md) — assertion DSL specification.
