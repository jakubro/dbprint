"""Pass a gated step's output through, failing the step when it prints a warning.

`KNOWN` drops the lines a step prints that dbprint has no switch for, each with its reason.
"""

from __future__ import annotations

import io
import re
import sys


WARNING_RE = re.compile(r"(?i:\bwarn(?:ing|ings)?\b)|[a-z]Warning\b")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
KNOWN = (
    (
        re.compile(r"^WARNING: Using incubator modules: jdk\.incubator\.vector$"),
        "Spark's launcher adds the JDK's vector incubator module to every JVM it starts",
    ),
    (
        re.compile(r"^WARNING: package sun\.security\.action not in java\.base$"),
        "Spark's launcher opens sun.security.action, a package newer JDKs no longer ship",
    ),
    (
        re.compile(
            r"^\S+ \S+ WARN NativeCodeLoader: Unable to load native-hadoop library for your",
        ),
        "pyspark bundles Hadoop without its native library, so Hadoop runs its Java fallback",
    ),
    (
        re.compile(r'^Setting default log level to "WARN"\.$'),
        "Spark naming its default log level, not a warning",
    ),
)

found = []

for raw in io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"):
    line = ANSI_RE.sub("", raw.rstrip("\n"))

    if any(pattern.search(line) for pattern, _ in KNOWN):
        continue

    print(raw, end="", flush=True)

    if WARNING_RE.search(line):
        found.append(line.strip())

if found:
    print(f"\n{len(found)} warning line(s); a gated step prints none:", file=sys.stderr)

    for line in found:
        print(f"  {line}", file=sys.stderr)

    sys.exit(1)
