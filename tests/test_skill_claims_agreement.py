"""Every fact the shipped example skill states is still true of what it describes.

The skill is hand-written, so no generator run notices when a tool is renamed, a `detection`
value is added, or a field leaves the schema - and a skill that sends an agent at a tool
nobody serves is worse than none. Each check reads the skill and the artifact it borrowed
from, never a second copy of either.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

from dbprint.mcp.resources import ResourceKind
from dbprint.mcp.tools import TOOL_DEFINITIONS


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "docs/examples/skill/dbprint.md"
CLI_DOCS_PATH = REPO_ROOT / "docs/CLI.md"
RELATIONSHIPS_SCHEMA_PATH = REPO_ROOT / "src/dbprint/spec/v1/relationships.schema.json"
_SCHEMA_PATHS = (
    REPO_ROOT / "src/dbprint/spec/v1/statistics.schema.json",
    REPO_ROOT / "src/dbprint/spec/v1/manifest.schema.json",
)

# The fields the skill's own rules turn on. A rename in the schema that left the skill behind
# would have it reasoning about an artifact no producer emits.
_FIELD_CLAIMS = (
    "scope",
    "rows_scanned",
    "sample",
    "filter",
    "row_count",
    "row_count_method",
    "values_coverage",
    "unmeasured",
    "null_patterns",
    "redacted",
    "profiled_at",
    "sum",
)

# The command surface it points a shell at, checked against the generated reference rather
# than the CLI itself - that file is rendered from `--help` and golden-tested against it.
_CLI_CLAIMS = ("dbprint context", "dbprint generate", "dbprint check", "--max-age")

_TOOL_SHAPED = re.compile(r"^(?:get|list|search)_[a-z_]+$")
_BACKTICKED = re.compile(r"`([^`]+)`")

# Both spellings the skill uses for a resource: the full URI, and the elided form beside it.
_RESOURCE_REF = re.compile(r"`(?:dbprint://[^`]*?|\.\.\.)/([a-z_]+)`")


def test_every_served_tool_is_named_by_the_skill() -> None:
    skill = SKILL_PATH.read_text()
    missing = sorted(tool.name for tool in TOOL_DEFINITIONS if f"`{tool.name}`" not in skill)

    assert not missing, f"served tools the skill never points at: {missing}"


def test_every_tool_shaped_name_in_the_skill_is_served() -> None:
    named = {token for token in _backticked() if _TOOL_SHAPED.fullmatch(token)}
    unserved = sorted(named - {tool.name for tool in TOOL_DEFINITIONS})

    assert named, "no tool name matched - the skill was restructured past this check"
    assert not unserved, f"the skill names tools nobody serves: {unserved}"


def test_every_detection_value_is_named_by_the_skill() -> None:
    """A fourth value in the enum leaves the skill's three-way split silently incomplete."""

    values = json.loads(RELATIONSHIPS_SCHEMA_PATH.read_text())["$defs"]["Detection"]["enum"]
    quoted = _quoted_tokens()
    missing = [value for value in values if value not in quoted]

    assert not missing, f"detection values the skill does not account for: {missing}"


def test_every_resource_the_skill_names_is_served() -> None:
    named = set(_RESOURCE_REF.findall(SKILL_PATH.read_text()))
    unserved = sorted(named - set(get_args(ResourceKind)))

    assert named, "no resource reference matched - the skill was restructured past this check"
    assert not unserved, f"the skill names resources nobody serves: {unserved}"


def test_every_field_the_skill_leans_on_is_a_schema_field() -> None:
    properties = _schema_properties()
    quoted = _quoted_tokens()

    assert not [name for name in _FIELD_CLAIMS if name not in properties], (
        f"claimed fields the schemas no longer define: {sorted(set(_FIELD_CLAIMS) - properties)}"
    )
    assert not [name for name in _FIELD_CLAIMS if name not in quoted], (
        f"fields listed here the skill no longer names as fields - drop them from "
        f"_FIELD_CLAIMS or restore the sentence: {sorted(set(_FIELD_CLAIMS) - quoted)}"
    )


def test_the_command_surface_the_skill_names_exists() -> None:
    skill = SKILL_PATH.read_text()
    reference = CLI_DOCS_PATH.read_text()

    for fragment in _CLI_CLAIMS:
        assert fragment in skill, f"{fragment!r} listed here but absent from the skill"
        assert fragment in reference, f"the skill names {fragment!r}, the CLI no longer has it"


def _backticked() -> set[str]:
    """Every backticked span in the skill, as written."""

    return set(_BACKTICKED.findall(SKILL_PATH.read_text()))


def _quoted_tokens() -> set[str]:
    """Identifier-shaped words inside backticks - `detection: declared` yields both of its own.

    Prose is not evidence a term is still being used as a field or an enum value: `measured`
    the detection and "measured over rows_scanned" are the same letters, and a check reading
    the whole text passes on a skill that dropped the first and kept the second.
    """

    return {token for span in _backticked() for token in re.split(r"[^A-Za-z0-9_]+", span)}


def _schema_properties() -> set[str]:
    """Every property name the statistics and manifest schemas define, at any depth."""

    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")

            if isinstance(properties, dict):
                names.update(properties)

            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for path in _SCHEMA_PATHS:
        walk(json.loads(path.read_text()))

    return names
