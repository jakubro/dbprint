#!/bin/bash
# Run a command and kill its whole process tree once the tree's resident memory passes a cap.
set -euo pipefail

USAGE="Usage: run-capped.sh <mem_mb> [--] <command...>"

CAP_MB=${1:?$USAGE}
shift
[[ ${1:-} == "--" ]] && shift

POLL_SECONDS=0.5

"$@" &
ROOT=$!
trap 'kill -9 $(tree "$ROOT") 2>/dev/null || true' INT TERM

main() {
  local peak_kb=0 status=0

  while kill -0 "$ROOT" 2>/dev/null; do
    local pids rss_kb
    pids=$(tree "$ROOT")
    rss_kb=$({ ps -o rss= -p "${pids// /,}" 2>/dev/null || true; } | awk '{s += $1} END {print s + 0}')

    ((rss_kb > peak_kb)) && peak_kb=$rss_kb

    if ((rss_kb > CAP_MB * 1024)); then
      echo "run-capped: $((rss_kb / 1024)) MB resident exceeds ${CAP_MB} MB, killing:" >&2
      ps -o pid=,rss=,args= -p "${pids// /,}" --sort=-rss | head -5 | cut -c1-160 >&2
      kill -9 $pids 2>/dev/null || true
      wait "$ROOT" 2>/dev/null || true
      exit 137
    fi

    sleep "$POLL_SECONDS"
  done

  wait "$ROOT" || status=$?
  echo "run-capped: peak $((peak_kb / 1024)) MB resident of ${CAP_MB} MB cap" >&2
  exit "$status"
}

# Walks parent links rather than process groups: a test server starts in a session of its own.
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
