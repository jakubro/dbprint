"""Plain-text rendering for `dbprint config explain`: a YAML `profile:` block, each leaf
commented with the layer that set it, under `#` lines stating what it was resolved against.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dbprint.config import RedactRule, RuleMatch
from dbprint.config.project import RuleVerdict
from dbprint.engine.explain import TableExplanation
from dbprint.engine.yaml_dumper import spell_inline


# A leaf longer than this (a whole `redact` list) takes its comment two spaces after it rather
# than pushing every other comment that far right.
_ALIGNED_WIDTH = 60


def render_explanation(explanation: TableExplanation) -> str:
    """The whole reply; `yaml.safe_load` of it is `{"profile": <the resolved values>}`."""

    header = [f"# connection: {explanation.connection}", *_table_lines(explanation)]
    header += _rule_lines(explanation)
    leaves = _leaf_lines(plain_profile(explanation.profile.values), explanation.profile.origins)
    width = max((len(text) for text, _ in leaves if len(text) <= _ALIGNED_WIDTH), default=0) + 2
    body = [
        f"{text.ljust(width) if len(text) < width else f'{text}  '}# {origin}" if origin else text
        for text, origin in leaves
    ]

    return "\n".join([*header, "profile:", *body]) + "\n"


def plain_profile(values: Mapping[str, Any]) -> dict[str, Any]:
    """The resolved tree as plain YAML data, a typed redact rule spelled as it is written."""

    return {
        key: plain_profile(value)
        if isinstance(value, Mapping)
        else [_redact_rule(item) if isinstance(item, RedactRule) else item for item in value]
        if isinstance(value, list | tuple)
        else value
        for key, value in values.items()
    }


def _table_lines(explanation: TableExplanation) -> list[str]:
    if explanation.fqn is None:
        return ["# table: none named - the connection's own profile, before any rule"]

    where = (
        f"in the print, profiled {explanation.profiled_at}"
        if explanation.printed
        else "not in the print"
    )
    lines = [f"# table: {explanation.fqn} ({where})"]

    if not explanation.in_scope:
        lines.append("# scope: outside tables.include/exclude - generate never profiles it")

    lines.append(f"# row count: {_count(explanation)}")

    if explanation.ceiling is not None:
        cap, fraction = explanation.ceiling
        reads = f"a sample fraction of {fraction}" if fraction is not None else "a whole read"
        lines.append(f"# max_rows: {cap}, which resolves to {reads} against that count")

    if explanation.recorded_changes:
        changed = ", ".join(explanation.recorded_changes)
        lines.append(f"# the print was profiled under a different profile: {changed}")

    return lines


def _count(explanation: TableExplanation) -> str:
    count = explanation.row_count

    if explanation.count_source == "print":
        return f"{count}, as the print last recorded it; size rules evaluated against it"

    if explanation.count_source == "database":
        return f"{count}, the database's current estimate; size rules evaluated against it"

    return f"none - {explanation.note}; size rules not applied"


def _rule_lines(explanation: TableExplanation) -> list[str]:
    if explanation.fqn is None:
        rows = [(label, _match(rule_match), "") for label, rule_match in explanation.rules]
    else:
        rows = [
            (v.label, _match(v.match), _verdict(v, explanation.row_count))
            for v in explanation.profile.rules
        ]

    if not rows:
        return ["# rules: none"]

    label_width = max(len(label) for label, _, _ in rows) + 2
    match_width = max(len(text) for _, text, _ in rows) + 2

    return [
        "# rules:",
        *(
            f"#   {label.ljust(label_width)}{text.ljust(match_width)}{verdict}".rstrip()
            for label, text, verdict in rows
        ),
    ]


def _match(rule_match: RuleMatch) -> str:
    written: dict[str, Any] = {"tables": list(rule_match.tables)}

    if rule_match.exclude:
        written["exclude"] = list(rule_match.exclude)

    if rule_match.min_rows is not None:
        written["min_rows"] = rule_match.min_rows

    return f"match {spell_inline(written)}"


def _verdict(verdict: RuleVerdict, count: int | None) -> str:
    threshold = verdict.match.min_rows

    if verdict.outcome == "applied":
        return f"applied ({count} >= {threshold})" if threshold is not None else "applied"

    if verdict.outcome == "below_min_rows":
        return f"not applied ({count} < {threshold})"

    if verdict.outcome == "size_unknown":
        return "not applied (no row count)"

    return "not matched (name)"


def _leaf_lines(
    tree: Mapping[str, Any],
    origins: Mapping[tuple[str, ...], str],
    path: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    indent = "  " * (len(path) + 1)
    out: list[tuple[str, str]] = []

    for key, value in tree.items():
        here = (*path, key)

        if isinstance(value, Mapping) and value:
            out.append((f"{indent}{key}:", ""))
            out += _leaf_lines(value, origins, here)
        else:
            out.append((f"{indent}{key}: {spell_inline(value)}", origins.get(here, "")))

    return out


def _redact_rule(rule: RedactRule) -> dict[str, Any]:
    written: dict[str, Any] = {
        key: list(getattr(rule, key))
        for key in ("columns", "sensitivity", "looks_like")
        if getattr(rule, key)
    }
    written["with"] = rule.with_

    return written
