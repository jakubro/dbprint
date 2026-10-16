"""Strict validation of `.dbprint.yaml` against the packaged schema, every problem in one pass."""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .read import BUILTIN_READ, MODE_PARAMETERS, fold_read


_VIEWS_WITH_MIN_ROWS = (
    "sets profile.read.views with match.min_rows - a view has no row-count estimate, so "
    "min_rows never selects one; move the views block into a rule of its own"
)
_SCOPES = ("Rule", "Connection", "Defaults")
_DURATION_PATTERN = "^0*[1-9][0-9]*[dhmsDHMS]$"
_VIEW_REFUSALS = {
    "max_rows": "not valid for views - a view has no row-count estimate to resolve a ceiling against",
    "fraction": "not valid for views - a sampling clause binds to a table; use mode: filter",
    "materialize": "not valid for views - a sampling clause binds to a table; use mode: filter",
}
_TAKES = {
    "full": "takes no parameters",
    "sample": "takes fraction or max_rows, and materialize",
    "filter": "takes filter",
    "catalog": "takes no parameters",
}


@dataclass(frozen=True)
class ConfigProblem:
    """One thing wrong with the file: where, as a dotted path, and what."""

    path: str
    message: str


def project_schema() -> dict[str, Any]:
    """The packaged JSON Schema for `.dbprint.yaml`, which editors and the loader share."""

    return _schema()


def validate_project(raw: Any) -> list[ConfigProblem]:
    """Every schema and cross-field problem in `raw`, in document order."""

    if not isinstance(raw, dict):
        return [ConfigProblem("", f"expected a mapping at the top level, got {_spell(raw)}")]

    located = _schema_problems(raw)
    dirty = [path for path, _ in located]
    located += [
        (path, message)
        for path, message in _semantic_problems(raw)
        if not any(_within(path, bad) or _within(bad, path) for bad in dirty)
    ]
    order = _document_order(raw)

    return [
        ConfigProblem(_render(path), message)
        for path, message in sorted(located, key=lambda item: _position(item[0], order))
    ]


def format_problems(config_path: Path, problems: Sequence[ConfigProblem]) -> str:
    """The loader's error text: one line for one problem, else a count and one line each."""

    if len(problems) == 1:
        return f"{config_path}: {_line(problems[0])}"

    lines = "\n".join(f"  {_line(problem)}" for problem in problems)

    return f"{config_path}: {len(problems)} problems:\n{lines}"


@cache
def _schema() -> dict[str, Any]:
    text = resources.files("dbprint.config").joinpath("project.schema.json").read_text("utf-8")

    return json.loads(text)


def _schema_problems(raw: dict[str, Any]) -> list[tuple[tuple[Any, ...], str]]:
    validator = Draft202012Validator(_schema())
    out: list[tuple[tuple[Any, ...], str]] = []
    seen: set[tuple[Any, ...]] = set()

    for error in sorted(validator.iter_errors(raw), key=lambda e: -len(e.absolute_path)):
        path = tuple(error.absolute_path)
        schema = _schema_of(error)

        if error.validator == "additionalProperties":
            out += [
                ((*path, key), _unknown_key_message(path, key, schema))
                for key in error.instance
                if key not in schema.get("properties", {})
            ]

            continue

        if "propertyNames" in error.relative_schema_path:
            mode = _get(raw, *path, "mode")
            out.append(((*path, error.instance), _off_mode_message(mode, None, error.instance)))

            continue

        target = (*path, _missing_key(error)) if error.validator == "required" else path

        if target in seen or any(_within(target, other) for other in seen if other != target):
            continue

        seen.add(target)
        out.append((target, _message(error)))

    return out


def _message(error: ValidationError) -> str:
    keyword = error.validator
    path = tuple(error.absolute_path)

    if path[-2:] == ("views", "mode") and error.instance == "sample":
        return (
            "sample is not valid for views - a sampling clause binds to a table; use mode: filter"
        )

    if keyword == "not" and {"fraction", "max_rows"} <= set(error.instance or ()):
        return "sets both fraction and max_rows - a sample is bounded by one or the other"

    if keyword == "enum" and isinstance(error.instance, str):
        options = [str(option) for option in _schema_of(error)["enum"]]
        close = difflib.get_close_matches(error.instance, options, n=1, cutoff=0.6)
        hint = f"; did you mean '{close[0]}'?" if close else ""

        return f"expected {_describe(_schema_of(error))}, got {error.instance!r}{hint}"

    if keyword == "required":
        return "missing required key"

    if keyword == "minProperties" and tuple(error.absolute_path) == ("connections",):
        return "at least one connection must be defined"

    if keyword == "minProperties":
        return "sets nothing, so it would do nothing"

    if keyword == "anyOf" and "columns" in _schema_of(error).get("properties", {}):
        return "names no `columns`, `sensitivity` or `looks_like`, so it targets nothing"

    if keyword == "minItems" and error.validator_value == 1 and error.instance == []:
        return "is an empty list, so it matches nothing"

    return f"expected {_describe(_schema_of(error))}, got {_spell(error.instance)}"


def _schema_of(error: ValidationError) -> Mapping[str, Any]:
    return error.schema if isinstance(error.schema, Mapping) else {}


def _missing_key(error: ValidationError) -> str:
    present = error.instance if isinstance(error.instance, dict) else {}

    return next(key for key in error.validator_value if key not in present)


def _unknown_key_message(path: tuple[Any, ...], key: str, schema: Mapping[str, Any]) -> str:
    siblings = sorted(schema.get("properties", {}))

    if path[-1:] == ("views",) and key in _VIEW_REFUSALS:
        return _VIEW_REFUSALS[key]

    if (moved := _relocation(path, str(key))) is not None:
        return f"unknown key; {moved}"

    if close := difflib.get_close_matches(str(key), siblings, n=1, cutoff=0.6):
        return f"unknown key; did you mean '{close[0]}'?"

    return f"unknown key; valid keys here: {', '.join(siblings)}"


def _relocation(path: tuple[Any, ...], key: str) -> str | None:
    scope, depth = _scope_of(path)

    if scope is None:
        return None

    if homes := _key_homes(scope).get(key):
        best = min(homes, key=lambda home: (len(home), home))

        return None if best[:-1] == path[depth:] else f"did you mean '{'.'.join(best)}'?"

    if scope != "Rule" and (homes := _key_homes("Rule").get(key)):
        home = ".".join(min(homes, key=lambda home: (len(home), home)))

        return f"it belongs to a rule: did you mean 'rules[].{home}'?"

    if scope == "Defaults" and key in _key_homes("Connection"):
        return f"it is set per connection, never under defaults: 'connections.<name>.{key}'"

    if scope == "Rule" and (
        homes := [
            home
            for home in _key_homes("Connection").get(key, [])
            if not path[depth:] or home[:-1] == path[depth:]
        ]
    ):
        home = ".".join(min(homes, key=lambda home: (len(home), home)))

        return f"it is set per connection, never in a rule: 'connections.<name>.{home}'"

    return None


def _scope_of(path: tuple[Any, ...]) -> tuple[str | None, int]:
    for depth in range(len(path), -1, -1):
        head = path[:depth]

        if len(head) >= 2 and head[-2] == "rules" and isinstance(head[-1], int):
            return "Rule", depth

        if len(head) == 2 and head[0] == "connections":
            return "Connection", depth

        if head == ("defaults",):
            return "Defaults", depth

    return None, 0


@cache
def _key_homes(definition: str) -> dict[str, list[tuple[str, ...]]]:
    homes: dict[str, list[tuple[str, ...]]] = {}

    def walk(schema: Mapping[str, Any], prefix: tuple[str, ...], seen: frozenset[str]) -> None:
        schema = _resolved(schema)

        for name, child in schema.get("properties", {}).items():
            homes.setdefault(name, []).append((*prefix, name))
            ref = child.get("$ref", "") if isinstance(child, dict) else ""

            if ref and ref.rsplit("/", 1)[-1] in seen:
                continue

            walk(child, (*prefix, name), seen | {ref.rsplit("/", 1)[-1]})

    walk(_schema()["$defs"][definition], (), frozenset(_SCOPES))

    return homes


def _resolved(schema: Mapping[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")

    if not isinstance(ref, str):
        return dict(schema)

    target = _schema()["$defs"][ref.rsplit("/", 1)[-1]]

    return {**_resolved(target), **{k: v for k, v in schema.items() if k != "$ref"}}


def _describe(schema: Mapping[str, Any]) -> str:
    schema = _resolved(schema)

    if "enum" in schema:
        return "one of " + ", ".join(f"'{value}'" for value in schema["enum"])

    kind = schema.get("type")

    if kind == "boolean":
        return "true or false"

    if kind == "integer" and "maximum" in schema:
        return f"an integer from {schema['minimum']} to {schema['maximum']}"

    if kind == "integer":
        return (
            f"an integer of at least {schema['minimum']}" if "minimum" in schema else "an integer"
        )

    if kind == "number":
        low = (
            f"({schema['exclusiveMinimum']}"
            if "exclusiveMinimum" in schema
            else f"[{schema.get('minimum')}"
        )

        return f"a number in {low}, {schema.get('maximum')}]"

    if kind == "string":
        if schema.get("pattern") == _DURATION_PATTERN:
            return "a duration like 30s, 10m or 2h"

        return "a non-empty string" if "pattern" in schema or "minLength" in schema else "a string"

    if kind == "array":
        items = _resolved(schema.get("items", {}))

        if items.get("type") == "string":
            return "a list of strings"

        if items.get("type") == "integer":
            return f"a non-empty list of integers from {items['minimum']} to {items['maximum']}"

        if "enum" in items:
            return "a list of " + _describe(items).removeprefix("one of ")

        return "a list"

    return "a mapping"


def _semantic_problems(raw: dict[str, Any]) -> list[tuple[tuple[Any, ...], str]]:
    out: list[tuple[tuple[Any, ...], str]] = []
    defaults = raw.get("defaults")
    defaults = defaults if isinstance(defaults, dict) else {}
    connections = raw.get("connections")
    connections = connections if isinstance(connections, dict) else {}

    # A `defaults` rule is checked once per connection it reaches; report each problem once.
    for name, body in connections.items():
        out += [p for p in _read_problems(defaults, name, body) if p not in out]

    default_parallelism = _get(defaults, "run", "parallelism")

    for name, body in connections.items():
        if not isinstance(body, dict) or body.get("adapter") != "duckdb":
            continue

        own = _get(body, "run", "parallelism")
        value, where = (
            (own, ("connections", name, "run", "parallelism"))
            if own is not None
            else (default_parallelism, ("defaults", "run", "parallelism"))
        )

        if isinstance(value, int) and value > 1:
            out.append(
                (
                    where,
                    (
                        f"{value} is not supported on the duckdb connection {name!r}, whose "
                        f"sessions cannot share one database; set it to 1"
                    ),
                ),
            )

    return out


def _read_problems(defaults: Any, name: str, body: Any) -> list[tuple[tuple[Any, ...], str]]:
    """Each written read block's parameters against the mode it has or inherits, and each
    level's effective block against the parameters its mode needs.
    """

    out: list[tuple[tuple[Any, ...], str]] = []
    connection = ("connections", name)

    for kind, builtin in BUILTIN_READ.items():
        effective, origin = dict(builtin), {"mode": "built-in default"}
        levels = [(("defaults",), defaults, "defaults"), (connection, body, f"connections.{name}")]

        for where, block, label in levels:
            written = _get(block, "profile", "read", kind)

            if isinstance(written, dict):
                out += _block_problems(
                    (*where, "profile", "read", kind),
                    written,
                    effective,
                    origin,
                )
                effective = fold_read(effective, written, label, origin)
                out += _missing(where + ("profile", "read", kind), effective)

        for owner, block in ((("defaults",), defaults), (connection, body)):
            rules = _get(block, "rules")

            for index, rule in enumerate(rules if isinstance(rules, list) else []):
                written = _get(rule, "profile", "read", kind)

                if not isinstance(written, dict):
                    continue

                where = (*owner, "rules", index, "profile", "read", kind)
                out += _block_problems(where, written, effective, dict(origin))

                # A rule setting mode needs its own parameters: earlier rules may have dropped them.
                if "mode" in written:
                    out += _missing(where, written, " in a rule's own block")
                else:
                    out += _missing(where, fold_read(effective, written, "", {}))

                if kind == "views" and _get(rule, "match", "min_rows") is not None:
                    out.append(((*owner, "rules", index), _VIEWS_WITH_MIN_ROWS))

    return out


def _block_problems(
    where: tuple[Any, ...],
    written: Mapping[str, Any],
    inherited: Mapping[str, Any],
    origin: Mapping[str, str],
) -> list[tuple[tuple[Any, ...], str]]:
    if "mode" in written:
        return []

    mode = inherited["mode"]

    return [
        ((*where, key), _off_mode_message(mode, origin.get("mode", "built-in default"), key))
        for key in written
        if key not in MODE_PARAMETERS.get(mode, frozenset())
    ]


def _missing(
    where: tuple[Any, ...],
    effective: Mapping[str, Any],
    scope: str = "",
) -> list[tuple[tuple[Any, ...], str]]:
    if effective.get("mode") == "sample" and not {"fraction", "max_rows"} & set(effective):
        return [(where, f"mode: sample needs fraction or max_rows{scope}")]

    if effective.get("mode") == "filter" and "filter" not in effective:
        return [(where, f"mode: filter needs filter{scope}")]

    return []


def _off_mode_message(mode: Any, inherited_from: str | None, key: Any) -> str:
    owner = next((m for m, keys in MODE_PARAMETERS.items() if key in keys), None)

    if inherited_from is None:
        return f"not valid under mode: {mode}; mode: {mode} {_TAKES.get(str(mode), '')}".rstrip()

    remedy = f"set mode: {owner} in this block" if owner else "remove it"

    return f"not valid under mode: {mode} (inherited from {inherited_from}); {remedy}"


def _get(tree: Any, *keys: Any) -> Any:
    for key in keys:
        if isinstance(tree, dict):
            tree = tree.get(key)
        elif isinstance(tree, list) and isinstance(key, int) and 0 <= key < len(tree):
            tree = tree[key]
        else:
            return None

    return tree


def _within(path: tuple[Any, ...], prefix: tuple[Any, ...]) -> bool:
    return path[: len(prefix)] == prefix


def _document_order(raw: Any) -> dict[tuple[Any, ...], int]:
    order: dict[tuple[Any, ...], int] = {}

    def walk(node: Any, path: tuple[Any, ...]) -> None:
        order[path] = len(order)
        children: Iterable[tuple[Any, Any]] = (
            node.items()
            if isinstance(node, dict)
            else enumerate(node)
            if isinstance(node, list)
            else ()
        )

        for key, child in children:
            walk(child, (*path, key))

    walk(raw, ())

    return order


def _position(path: tuple[Any, ...], order: Mapping[tuple[Any, ...], int]) -> tuple[int, int]:
    for depth in range(len(path), -1, -1):
        if path[:depth] in order:
            return order[path[:depth]], len(path) - depth

    return 0, len(path)


def _render(path: tuple[Any, ...]) -> str:
    out = ""

    for part in path:
        out += f"[{part}]" if isinstance(part, int) else f".{part}" if out else str(part)

    return out


def _line(problem: ConfigProblem) -> str:
    return f"{problem.path}: {problem.message}" if problem.path else problem.message


def _spell(value: Any) -> str:
    if isinstance(value, dict):
        return "a mapping"

    if isinstance(value, list):
        return "a list"

    return repr(value)
