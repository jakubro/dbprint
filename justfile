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

# List available recipes
default:
    @just --list

# Full pre-commit check
check: lint test-cov

# Install all dependencies
install:
    {{ UV_ENV }} uv sync --extra dev --extra mcp --extra docs 2>&1 | tee /tmp/dbprint--install.log

# Run tests; ARGS narrows (pytest falls back to testpaths when given no path)
test *ARGS:
    {{ UV_ENV }} {{ RUN_BOUNDED }} 20m 8192 -- uv run --extra dev --extra mcp --extra docs python -m pytest {{ ARGS }} 2>&1 | tee /tmp/dbprint--test.log

# Run all tests with coverage, parallelized (kept out of `test` - not worth it on a narrowed run)
test-cov *ARGS:
    just test -n auto --cov=src --cov-report=term-missing {{ ARGS }}

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

# Regenerate docs/CLI.md, docs/MCP.md's tool schemas, the consumer guide, and the two
# array-entry annotation schemas; all golden-tested
docs:
    {{ UV_RUN }} python scripts/gen_cli_docs.py
    {{ UV_RUN }} python scripts/gen_mcp_docs.py
    {{ UV_RUN }} python scripts/gen_reading_guide.py
    {{ UV_RUN }} python scripts/gen_annotation_schemas.py

# Regenerate the v1 reference example from a real Postgres run; golden-tested by check
example:
    {{ UV_RUN }} python scripts/gen_reference_example.py

# Regenerate the v1 vocabulary example (looks_like values the reference example has no home for)
example-vocabulary:
    {{ UV_RUN }} python scripts/gen_vocabulary_example.py
