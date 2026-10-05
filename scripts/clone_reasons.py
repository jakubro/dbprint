"""Fail on an accepted jscpd clone without a reason in .jscpd-reasons.json, or a reason without a clone.

An optional jscpd SARIF report names both sides of each accepted clone that lacks a reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


accepted = json.loads(Path(".jscpd-baseline.json").read_text(encoding="utf-8"))["fingerprints"]
reasons = json.loads(Path(".jscpd-reasons.json").read_text(encoding="utf-8"))
locations: dict[str, str] = {}

if len(sys.argv) > 1:
    run = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["runs"][0]
    roots = {
        key: Path(base["uri"].removeprefix("file://"))
        for key, base in run["originalUriBaseIds"].items()
    }

    def _side(location: dict) -> str:
        physical = location["physicalLocation"]
        path = (
            roots[physical["artifactLocation"]["uriBaseId"]] / physical["artifactLocation"]["uri"]
        )
        region = physical["region"]

        return f"{path.relative_to(Path.cwd(), walk_up=True)}:{region['startLine']}-{region['endLine']}"

    for result in run["results"]:
        locations[result["partialFingerprints"]["jscpdCloneHash/v1"]] = (
            f" ({_side(result['locations'][0])} <-> {_side(result['relatedLocations'][0])})"
        )

problems = [
    *(
        f"{fingerprint}: accepted without a reason{locations.get(fingerprint, '')}"
        for fingerprint in accepted
        if not reasons.get(fingerprint, "").strip()
    ),
    *(
        f"{fingerprint}: reason for no accepted clone"
        for fingerprint in reasons
        if fingerprint not in accepted
    ),
]

if problems:
    print("\n".join(problems), file=sys.stderr)
    sys.exit(1)
