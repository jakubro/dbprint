#!/bin/bash
# Run a command bounded by wall-clock time and per-process address space.
set -euo pipefail

USAGE="Usage: run-bounded.sh <time> <as_mb> [--] <command...>"

TIME_LIMIT=${1:?$USAGE}
AS_LIMIT_MB=${2:?$USAGE}
shift 2
[[ ${1:-} == "--" ]] && shift

AS_BYTES=$((AS_LIMIT_MB * 1024 * 1024))

# RLIMIT_AS is address space per process and never a total for the run - a JVM reserves tens of
# gigabytes it never touches. The run's memory ceiling is tests/conftest.py's worker sizing.
exec \
  timeout --kill-after=10s --signal=TERM "$TIME_LIMIT" \
  prlimit --as="$AS_BYTES" -- \
  "$@"
