"""The Claude Code plugin agrees with the release it pins, its marketplace and its own skills."""

from __future__ import annotations

import json
import re

import yaml

import dbprint
from tests._scripts import REPO_ROOT


MARKETPLACE = json.loads((REPO_ROOT / ".claude-plugin/marketplace.json").read_text())
PLUGIN_DIR = REPO_ROOT / "plugin"
PLUGIN = json.loads((PLUGIN_DIR / ".claude-plugin/plugin.json").read_text())
SERVER = json.loads((REPO_ROOT / "server.json").read_text())
_USER_CONFIG_RE = re.compile(r"\$\{user_config\.(\w+)\}")


def test_the_plugin_pins_the_release_server_json_pins() -> None:
    server_pin = next(
        argument["value"]
        for argument in SERVER["packages"][0]["runtimeArguments"]
        if argument["name"] == "--with"
    )
    arguments = PLUGIN["mcpServers"]["dbprint"]["args"]

    assert (
        arguments[arguments.index("--with") + 1]
        == server_pin
        == f"dbprint[mcp]=={dbprint.__version__}"
    )
    assert PLUGIN["version"] == SERVER["version"] == dbprint.__version__


def test_the_marketplace_entry_resolves_to_the_plugin() -> None:
    (entry,) = (plugin for plugin in MARKETPLACE["plugins"] if plugin["name"] == "dbprint")

    assert (REPO_ROOT / entry["source"]).resolve() == PLUGIN_DIR
    assert PLUGIN["name"] == "dbprint"


def test_every_setting_the_server_reads_is_declared_and_optional() -> None:
    """Unset, an option is "", which `serve --project` reads as Claude Code's launch directory.

    A `default` reaches the server literally and `required` forces a value; both replace the "".
    """

    referenced = set(_USER_CONFIG_RE.findall(json.dumps(PLUGIN["mcpServers"])))

    assert referenced == {"project"}
    assert all(
        not PLUGIN["userConfig"][name].get("required")
        and "default" not in PLUGIN["userConfig"][name]
        for name in referenced
    )


def test_each_skill_is_named_for_its_directory() -> None:
    names = {}

    for skill in sorted((PLUGIN_DIR / "skills").glob("*/SKILL.md")):
        frontmatter = skill.read_text().split("---\n")[1]
        names[skill.parent.name] = yaml.safe_load(frontmatter)["name"]

    assert names == {"dbprint": "dbprint", "dbprint-annotate": "dbprint-annotate"}
