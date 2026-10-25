#!/bin/bash
# Run a command under a time cap and a memory cap, each enforced by a monitor that names it and a backstop.
set -euo pipefail

USAGE="Usage: run-capped.sh <minutes> <mem_mb> <as_mb> [--] <command...>"

CAP_MINUTES=${1:?$USAGE}
CAP_MB=${2:?$USAGE}
AS_MB=${3:?$USAGE}
shift 3
[[ ${1:-} == "--" ]] && shift

POLL_SECONDS=0.5

if ! ps -o pid= -p $$ > /dev/null 2>&1; then
  echo "run-capped: ps cannot read this process table, so no cap can be enforced" >&2
  exit 1
fi

# The backstops hold if this monitor dies: timeout keeps its own clock a minute past the time cap, and
# the kernel enforces the address-space limit on every process the command starts. That limit is per
# process, never a total - a JVM reserves address space it never touches - so as_mb sits far above mem_mb.
timeout --kill-after=10s "$(((CAP_MINUTES + 1) * 60))" prlimit --as="$((AS_MB * 1024 * 1024))" -- "$@" &
ROOT=$!
trap 'kill -9 $(tree "$ROOT") 2>/dev/null || true' INT TERM

main() {
  local peak_kb=0 status=0 deadline=$((SECONDS + CAP_MINUTES * 60))

  while kill -0 "$ROOT" 2>/dev/null; do
    local pids rss_kb
    pids=$(tree "$ROOT")
    rss_kb=$({ ps -o rss= -p "${pids// /,}" 2>/dev/null || true; } | awk '{s += $1} END {print s + 0}')

    ((rss_kb > peak_kb)) && peak_kb=$rss_kb

    if ((rss_kb > CAP_MB * 1024)); then
      stop 137 "$((rss_kb / 1024)) MB resident exceeds the ${CAP_MB} MB cap" "$pids"
    fi

    if ((SECONDS >= deadline)); then
      stop 124 "still running at the ${CAP_MINUTES} minute cap" "$pids"
    fi

    sleep "$POLL_SECONDS"
  done

  wait "$ROOT" || status=$?
  echo "run-capped: peak $((peak_kb / 1024)) MB resident of ${CAP_MB} MB cap" >&2
  exit "$status"
}

stop() {
  local code=$1 reason=$2 pids
  read -ra pids <<< "$3"

  echo "run-capped: $reason, killing:" >&2
  ps -o pid=,rss=,args= -p "${3// /,}" --sort=-rss | head -5 | cut -c1-160 >&2 || true
  kill -9 "${pids[@]}" 2>/dev/null || true
  wait "$ROOT" 2>/dev/null || true
  exit "$code"
}

# Walks parent links rather than process groups, so a child that starts its own session is still found.
tree() {
  ps -eo pid=,ppid= | awk -v root="$1" '
    { parent[$1] = $2 }
    END {
      found[root] = 1; out = root; grew = 1
      while (grew) {
        grew = 0
        for (p in parent) if (!(p in found) && (parent[p] in found)) { found[p] = 1; out = out " " p; grew = 1 }
      }
      print out
    }'
}

main
