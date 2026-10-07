# dbprint development tasks

set shell := ["bash", "-euo", "pipefail", "-c"]

# Detect if running inside a container or not
CONTAINER := `if [ -f /run/.containerenv ] || [ -f /.dockerenv ]; then echo 'true'; else echo 'false'; fi`
# Container-local venv path (avoids corrupting host .venv)
CONTAINER_VENV := "/tmp" / justfile_directory() / ".venv"
# UV env prefix: routes uv to container venv when in container; empty otherwise (uses .venv/ in cwd)
UV_ENV := if CONTAINER == "true" { f"UV_PROJECT_ENVIRONMENT='{{ CONTAINER_VENV }}' VIRTUAL_ENV=" } else { "VIRTUAL_ENV=" }
# UV runner: auto-selects container venv
UV_RUN := UV_ENV + " uv run --extra dev --extra mcp --extra docs"
# Python runner: auto-selects container venv
PYTHON := UV_RUN + " python -m"
# Test runner wrapper: bounds wall-clock time and virtual memory
RUN_BOUNDED := justfile_directory() / "scripts/run-bounded.sh"
# Test runner wrapper: kills the whole process tree past a total resident memory cap
RUN_CAPPED := justfile_directory() / "scripts/run-capped.sh"
# Clone detector, pinned exactly: its fingerprints are the baseline's format
JSCPD := "npx --yes jscpd@5.3.2 --config .jscpd.json --baseline .jscpd-baseline.json"
# Mutation testing works on a copy: mutmut writes `mutants/` into its working directory
MUTATE_DIR := "/tmp/dbprint--mutate"
MUTMUT := UV_ENV + " uv run --project " + justfile_directory() + " --extra dev --extra mcp --extra docs mutmut"
# xdist distribution mode; `load` suits a machine with fewer cores than there are vendor groups
DIST := env_var_or_default("DBPRINT_TEST_DIST", "loadgroup")

# List available recipes
default:
    @just --list

# Full pre-commit check
check: lint test-cov

# Install all dependencies and warm Delta's Maven/Ivy jar cache (see tests/_provisioning.py)
install:
    {{ UV_ENV }} uv sync --extra dev --extra mcp --extra docs 2>&1 | tee /tmp/dbprint--install.log
    {{ UV_RUN }} python -m tests._provisioning 2>&1 | tee -a /tmp/dbprint--install.log

# Run tests; ARGS narrows (pytest falls back to testpaths when given no path)
# `-rfEs`, not `-rs`: `-r` replaces pytest's default `fE`, so failures and errors must be restated.
test *ARGS:
    {{ UV_ENV }} {{ RUN_CAPPED }} 81920 -- {{ RUN_BOUNDED }} 30m 32768 -- uv run --extra dev --extra mcp --extra docs python -m pytest -rfEs {{ ARGS }} 2>&1 | tee /tmp/dbprint--test.log

# Run all tests with coverage, parallelized (kept out of `test` - not worth it on a narrowed run)
# `loadgroup` honours conftest's Spark and BigQuery groups: one instance per group, not per worker.
test-cov *ARGS:
    just test -n auto --dist {{ DIST }} --durations=40 --cov=src --cov-report=term-missing {{ ARGS }}

# Run every test that needs no database server, container or JVM, in parallel and without coverage
test-fast *ARGS:
    just test -n auto -m "'not live_server'" {{ ARGS }}

# Lint all code
lint:
    rm -f /tmp/dbprint--lint.log
    {{ UV_RUN }} ruff format --check 2>&1 | tee -a /tmp/dbprint--lint.log
    {{ UV_RUN }} ruff check 2>&1 | tee -a /tmp/dbprint--lint.log
    PYTHONPATH= {{ UV_RUN }} ty check 2>&1 | tee -a /tmp/dbprint--lint.log
    {{ UV_RUN }} deptry src 2>&1 | tee -a /tmp/dbprint--lint.log
    PYTHONPATH=src {{ UV_RUN }} lint-imports --no-logo --cache-dir /tmp/.import-linter-dbprint 2>&1 | tee -a /tmp/dbprint--lint.log
    {{ JSCPD }} --fail-on-new-clones --reporters json,sarif,silent 2>&1 | tee -a /tmp/dbprint--lint.log \
        || { {{ UV_RUN }} python scripts/new_clones.py /tmp/dbprint--jscpd/jscpd-report.json \
        | tee -a /tmp/dbprint--lint.log; exit 1; }
    just clone-reasons 2>&1 | tee -a /tmp/dbprint--lint.log

# Mutation-test one package (spec, assertions, conformance), or one module of it, against its tests in a /tmp copy
mutate PACKAGE MODULE="*":
    rm -rf {{ MUTATE_DIR }} && mkdir -p {{ MUTATE_DIR }}
    cp -r src tests docs scripts pyproject.toml {{ MUTATE_DIR }}/
    printf 'only_mutate = ["src/dbprint/{{ PACKAGE }}/{{ MODULE }}"]\npytest_add_cli_args_test_selection = ["tests/{{ PACKAGE }}"]\n' \
        >> {{ MUTATE_DIR }}/pyproject.toml
    cd {{ MUTATE_DIR }} && DBPRINT_HYPOTHESIS_PROFILE=mutate {{ MUTMUT }} run 2>&1 | tr '\r' '\n' \
        | grep -v '^[^0-9]*Generating mutants' | tee /tmp/dbprint--mutate.log
    cd {{ MUTATE_DIR }} && {{ MUTMUT }} results | tee -a /tmp/dbprint--mutate.log \
        | sed -n 's/^ *\([^:]*\): survived$/\1/p' | while read -r name; do {{ MUTMUT }} show "$name"; done \
        | tee -a /tmp/dbprint--mutate.log

# Refresh the clone baseline, print every clone, and list each accepted one still lacking a reason
dupes:
    {{ JSCPD }} --update-baseline --reporters console,sarif 2>&1 | tee /tmp/dbprint--dupes.log
    just clone-reasons 2>&1 | tee -a /tmp/dbprint--dupes.log

# Fail unless each clone in the baseline has a reason in .jscpd-reasons.json, and each reason a clone
clone-reasons:
    {{ UV_RUN }} python scripts/clone_reasons.py /tmp/dbprint--jscpd/jscpd-report.sarif

# Auto-fix all code
fix:
    rm -f /tmp/dbprint--fix.log
    for _ in 1 2 3 4 5; do \
        {{ UV_RUN }} ruff check --fix 2>&1 | tee -a /tmp/dbprint--fix.log; \
        {{ UV_RUN }} ruff format 2>&1 | tee -a /tmp/dbprint--fix.log; \
        {{ UV_RUN }} ruff check --quiet 2>/dev/null && break; \
    done
    PYTHONPATH= {{ UV_RUN }} ty check --fix 2>&1 | tee -a /tmp/dbprint--fix.log

# Regenerate every generated document (CLI, MCP schemas, conformance index, guide); golden-tested
docs:
    rm -f /tmp/dbprint--docs.log
    {{ UV_RUN }} python scripts/gen_cli_docs.py 2>&1 | tee -a /tmp/dbprint--docs.log
    {{ UV_RUN }} python scripts/gen_mcp_docs.py 2>&1 | tee -a /tmp/dbprint--docs.log
    {{ UV_RUN }} python scripts/gen_conformance_index.py 2>&1 | tee -a /tmp/dbprint--docs.log
    {{ UV_RUN }} python scripts/gen_statistics_matrix.py 2>&1 | tee -a /tmp/dbprint--docs.log
    {{ UV_RUN }} python scripts/gen_reading_guide.py 2>&1 | tee -a /tmp/dbprint--docs.log
    {{ UV_RUN }} python scripts/gen_annotation_schemas.py 2>&1 | tee -a /tmp/dbprint--docs.log

# Regenerate the v1 reference example against a throwaway Postgres; golden-tested by check
example:
    {{ UV_RUN }} python scripts/gen_reference_example.py 2>&1 | tee /tmp/dbprint--example.log

# Regenerate the v1 vocabulary example (looks_like values the reference example has no home for)
example-vocabulary:
    {{ UV_RUN }} python scripts/gen_vocabulary_example.py 2>&1 | tee /tmp/dbprint--example-vocabulary.log

# Regenerate the landing page's recording against a throwaway Postgres; golden-tested by check
demo:
    {{ UV_RUN }} python scripts/gen_demo_cast.py 2>&1 | tee /tmp/dbprint--demo.log

# Build the documentation site (Astro + Starlight over docs/); its own job, not part of check
site:
    cd site && npm ci 2>&1 | tee /tmp/dbprint--site.log
    cd site && npm run build 2>&1 | tee -a /tmp/dbprint--site.log

# Serve docs/ with live reload at the configured base; HOST=0.0.0.0 to reach it from outside
preview HOST="127.0.0.1" PORT="4321":
    cd site && npm ci && npm run dev -- --host {{ HOST }} --port {{ PORT }}
