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
test *ARGS:
    {{ UV_ENV }} {{ RUN_BOUNDED }} 20m 32768 -- uv run --extra dev --extra mcp --extra docs python -m pytest {{ ARGS }} 2>&1 | tee /tmp/dbprint--test.log

# Run all tests with coverage, parallelized (kept out of `test` - not worth it on a narrowed run)
# `loadgroup` honours conftest's per-vendor groups: one live substrate per run, not per worker.
test-cov *ARGS:
    just test -n auto --dist {{ DIST }} --cov=src --cov-report=term-missing {{ ARGS }}

# Lint all code
lint:
    rm -f /tmp/dbprint--lint.log
    {{ UV_RUN }} ruff format --check 2>&1 | tee -a /tmp/dbprint--lint.log
    {{ UV_RUN }} ruff check 2>&1 | tee -a /tmp/dbprint--lint.log
    PYTHONPATH= {{ UV_RUN }} ty check 2>&1 | tee -a /tmp/dbprint--lint.log

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

# Build the documentation site (Astro + Starlight over docs/); its own job, not part of check
site:
    cd site && npm ci 2>&1 | tee /tmp/dbprint--site.log
    cd site && npm run build 2>&1 | tee -a /tmp/dbprint--site.log

# Serve docs/ with live reload at the configured base; HOST=0.0.0.0 to reach it from outside
preview HOST="127.0.0.1" PORT="4321":
    cd site && npm run dev -- --host {{ HOST }} --port {{ PORT }}
