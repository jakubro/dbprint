"""Print the clones a jscpd JSON report marks new against the baseline, one pair per line."""

from __future__ import annotations

import json
import sys
from pathlib import Path


report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

for clone in report["duplicates"]:
    if clone["isNew"]:
        first, second = clone["firstFile"], clone["secondFile"]
        print(
            f"new clone ({clone['lines']} lines): {first['name']}:{first['start']}-{first['end']}"
            f" <-> {second['name']}:{second['start']}-{second['end']}",
        )
