#!/bin/bash
# Run a command bounded by wall-clock time and virtual memory.
set -euo pipefail

USAGE="Usage: run-bounded.sh <time> <mem_mb> [--] <command...>"

TIME_LIMIT=${1:?$USAGE}
MEM_LIMIT_MB=${2:?$USAGE}
shift 2
[[ ${1:-} == "--" ]] && shift

MEM_BYTES=$((MEM_LIMIT_MB * 1024 * 1024))

# prlimit --as caps virtual memory (RLIMIT_AS), inherited by every child.
exec \
  timeout --kill-after=10s --signal=TERM "$TIME_LIMIT" \
  prlimit --as="$MEM_BYTES" -- \
  "$@"
