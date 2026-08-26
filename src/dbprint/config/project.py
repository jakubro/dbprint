"""Project config (`.dbprint.yaml`) - schema, dataclasses, upward-discovery loader.

Defaults cascade into per-connection blocks where the connection doesn't override.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

from dbprint.spec.looks_like import LooksLike
from dbprint.spec.sensitivity import Sensitivity
from .selectors import match


ENUMERATION_THRESHOLD_DEFAULT = 50
TOP_N_VALUES_DEFAULT = 20
TOP_N_NULL_PATTERNS_DEFAULT = 20
LOOKS_LIKE_SAMPLE_SIZE_DEFAULT = 1000
PERCENTILES_DEFAULT = (1, 25, 50, 75, 99)
MAX_AGE_DAYS_DEFAULT = 7

# `max_rows_scanned` snaps its resolved fraction down this grid, so estimate drift stays
# within a step (ratio in CONFIG.md).
CEILING_GRID_RATIO = 1.1
STAT_CHANGE_THRESHOLD_DEFAULT = {
    "cardinality_ratio": 0.02,
    "percentile_pct": 0.05,
    "values_coverage": 0.05,
    "default": 0.01,
}
OUTPUT_DEFAULT = "prints"
INFER_RELATIONSHIPS_DEFAULT = True
MATERIALIZE_SAMPLE_DEFAULT = True
SKETCH_ALL_COLUMNS_DEFAULT = False

# Credential key for the redaction salt - it lives with the passwords, not in `.dbprint.yaml`.
REDACTION_SALT_KEY = "redaction_salt"

ADAPTERS: tuple[str, ...] = ("postgres", "snowflake", "mysql")

# Keys read only inside a `rules:` entry, mapped to the tail of the remediation snippet an
# error suggests. `min_rows` only selects tables, so its snippet also needs a narrowing
# setting (`sample`/`filter`).
_RULE_ONLY_KEYS: dict[str, str] = {
    "sample": "",
    "filter": "",
    "min_rows": ", sample: 0.01",
}

_STATISTICS_INT_KEYS: tuple[str, ...] = (
    "enumeration_threshold",
    "top_n_values",
    "top_n_null_patterns",
    "looks_like_sample_size",
)


class ConfigError(ValueError):
    """Raised when the project config file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class StatisticsConfig:
    """Tuning for statistics computation per SPEC 2.2."""

    enumeration_threshold: int = ENUMERATION_THRESHOLD_DEFAULT
    top_n_values: int = TOP_N_VALUES_DEFAULT
    top_n_null_patterns: int = TOP_N_NULL_PATTERNS_DEFAULT
    looks_like_sample_size: int = LOOKS_LIKE_SAMPLE_SIZE_DEFAULT
    percentiles: tuple[int, ...] = PERCENTILES_DEFAULT


@dataclass(frozen=True)
class RuleConfig:
    """One profiling rule: a matcher plus the settings it overrides.

    `include`/`exclude`/`min_rows` select the tables the rule governs; the rest override the
    connection's settings for them. `sample` and `filter` each narrow rows per SPEC 2.2.8, at
    most one per rule; `max_rows_scanned` narrows too but cascades like `sample` without
    joining that exclusion, and `statistics` merges key by key. `label` names the rule in an
    error (`defaults rules[0]`, `connection 'w' rules[1]`), empty for a rule built in code.
    """

    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    min_rows: int | None = None
    sample: float | None = None
    filter: str | None = None
    max_rows_scanned: int | None = None
    statistics: dict[str, Any] = field(default_factory=dict)
    max_age_days: int | None = None
    label: str = ""

    def matches(self, fqn: str, row_count: int | None = None) -> bool:
        """True when this rule governs a table of that name at that size.

        `row_count` is the catalog estimate, None when unavailable or when the caller holds
        no database. A `min_rows` rule left without one goes unapplied, not assumed to match.
        """

        if not self.matches_name(fqn):
            return False

        if self.min_rows is None:
            return True

        return row_count is not None and row_count >= self.min_rows

    def matches_name(self, fqn: str) -> bool:
        """True when the name matchers admit `fqn`, disregarding any size condition."""

        return match(fqn, list(self.include), list(self.exclude))


@dataclass(frozen=True)
class RedactRule:
    """One redaction rule: which columns it covers, and what it does to their values.

    `columns` globs over the qualified `<fqn>.<column>`; matching any of `columns`,
    `sensitivity` or `looks_like` covers the column. Declaration order, last match wins.
    """

    columns: tuple[str, ...] = ()
    sensitivity: tuple[str, ...] = ()
    looks_like: tuple[str, ...] = ()
    with_: str = "mask"

    def covers(self, qualified: str, sensitivity: str | None, looks_like: str | None) -> bool:
        """True when this rule governs a column with those inferred properties."""

        if self.columns and match(qualified, list(self.columns), []):
            return True

        if sensitivity is not None and sensitivity in self.sensitivity:
            return True

        return looks_like is not None and looks_like in self.looks_like


@dataclass(frozen=True)
class TableSettings:
    """Effective settings for one table once the rule cascade has been applied.

    At most one of `sample` and `filter` is set (SPEC 2.2.8). A `max_rows_scanned` ceiling folds
    into `sample` as a fraction, and `ceiling_yielded` is True when a ceiling matched but
    `filter` won instead. `matched_rules` is every rule that matched, in declaration order.
    """

    statistics: StatisticsConfig
    max_age_days: int
    sample: float | None = None
    filter: str | None = None
    ceiling_yielded: bool = False
    matched_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiffConfig:
    """Tuning for diff rendering per SPEC 2.6.9."""

    stat_change_threshold: dict[str, float] = field(
        default_factory=lambda: dict(STAT_CHANGE_THRESHOLD_DEFAULT),
    )


@dataclass(frozen=True)
class ConnectionConfig:
    """Per-connection settings; `include`/`exclude` gate what is profiled, `rules` say how."""

    name: str
    adapter: Literal["postgres", "snowflake", "mysql"]
    auto: bool = False
    output: Path = field(default_factory=lambda: Path(OUTPUT_DEFAULT))
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    max_age_days: int = MAX_AGE_DAYS_DEFAULT
    max_rows_scanned: int | None = None
    statistics: StatisticsConfig = field(default_factory=StatisticsConfig)
    rules: tuple[RuleConfig, ...] = ()
    diff: DiffConfig = field(default_factory=DiffConfig)
    infer_relationships: bool = INFER_RELATIONSHIPS_DEFAULT
    materialize_sample: bool = MATERIALIZE_SAMPLE_DEFAULT
    sketch_all_columns: bool = SKETCH_ALL_COLUMNS_DEFAULT
    redact: tuple[RedactRule, ...] = ()
    redaction_salt: str | None = None
    assertions_raw: dict[str, Any] = field(default_factory=dict)

    @property
    def rules_read_row_counts(self) -> bool:
        """True when any rule carries a size condition, or a ceiling is in force anywhere.

        Constant for a whole run, since the rule list is fixed at load. The engine reads it
        to decide whether to fetch a catalog estimate at all.
        """

        return self.max_rows_scanned is not None or any(
            rule.min_rows is not None or rule.max_rows_scanned is not None for rule in self.rules
        )

    def size_conditions_name(self, fqn: str) -> bool:
        """True when some rule or the connection's own ceiling would read a count for this table.

        The size test itself is disregarded: this separates "size mattered and could not be
        read" from "no size condition governs this table". A connection-level ceiling governs
        every table, unlike a rule's own condition.
        """

        if self.max_rows_scanned is not None:
            return True

        return any(
            (rule.min_rows is not None or rule.max_rows_scanned is not None)
            and rule.matches_name(fqn)
            for rule in self.rules
        )

    def redaction_for(
        self,
        qualified: str,
        sensitivity: str | None,
        looks_like: str | None,
    ) -> str | None:
        """The primitive covering one column, or None when nothing redacts it.

        Declaration order, last match wins, with `defaults` rules walking before the
        connection's own. No primitive means the column is not redacted.
        """

        primitive: str | None = None

        for rule in self.redact:
            if rule.covers(qualified, sensitivity, looks_like):
                primitive = rule.with_

        return primitive

    def settings_for(self, fqn: str, row_count: int | None = None) -> TableSettings:
        """Effective settings for one table: connection values, then every matching rule.

        Rules apply in declaration order, later ones winning. `row_count` is the catalog
        estimate gating every size-conditioned rule; callers holding no database omit it,
        leaving those rules unapplied. `max_rows_scanned` cascades on the same timeline as
        `sample` and whichever was set last wins, so a ceiling a later `sample` overrides is
        never converted to a fraction. A `filter` on the table beats a ceiling outright, and
        `ceiling_yielded` says so for the caller to log (`config` does not log - GUIDELINES).

        Raises `ConfigError` when matching rules settle both a filter and an explicit sample:
        each rule carries at most one, so the pair arises only across rules, once a table
        name is in hand.
        """

        statistics = self.statistics
        max_age_days = self.max_age_days
        sample: float | None = None
        predicate: str | None = None
        sample_rule = ""
        filter_rule = ""
        cap = self.max_rows_scanned
        cap_position = 0 if self.max_rows_scanned is not None else -1
        sample_position = -1
        matched_rules: list[str] = []

        for index, rule in enumerate(self.rules):
            if not rule.matches(fqn, row_count):
                continue

            matched_rules.append(rule.label or f"rules[{index}]")

            if rule.statistics:
                statistics = replace(statistics, **rule.statistics)

            if rule.max_age_days is not None:
                max_age_days = rule.max_age_days

            if rule.sample is not None:
                sample = rule.sample
                sample_rule = rule.label or f"rules[{index}]"
                sample_position = index + 1

            if rule.filter is not None:
                predicate = rule.filter
                filter_rule = rule.label or f"rules[{index}]"

            if rule.max_rows_scanned is not None:
                cap = rule.max_rows_scanned
                cap_position = index + 1

        if sample is not None and predicate is not None:
            raise ConfigError(
                f"connection {self.name!r}: table {fqn!r} resolves to both a filter "
                f"(from {filter_rule}) and a sample (from {sample_rule}). A table is narrowed "
                f"by a predicate or by a fraction, never both - drop one of the two keys, or "
                f"narrow the rule that sets it so it stops matching this table.",
            )

        ceiling_yielded = False

        if predicate is not None:
            ceiling_yielded = cap is not None
        elif cap is not None and cap_position > sample_position:
            sample = _resolve_ceiling(cap, row_count)

        return TableSettings(
            statistics=statistics,
            max_age_days=max_age_days,
            sample=sample,
            filter=predicate,
            ceiling_yielded=ceiling_yielded,
            matched_rules=tuple(matched_rules),
        )


@dataclass(frozen=True)
class ProjectConfig:
    """Loaded project config plus the resolved project root directory."""

    project_root: Path
    connections: dict[str, ConnectionConfig]


def bind_redaction_salt(conn: ConnectionConfig, salt: str | None) -> ConnectionConfig:
    """Attach the resolved salt; a `hash` rule without one is refused, never defaulted."""

    if salt is None and any(rule.with_ == "hash" for rule in conn.redact):
        raise ConfigError(
            f"connection {conn.name!r}: a `redact` rule uses `with: hash` but no "
            f"`{REDACTION_SALT_KEY}` is configured. Add it to the connection's entry in "
            f"~/.dbprint/connections.yaml, or set "
            f"DBPRINT_{conn.name.upper()}_{REDACTION_SALT_KEY.upper()}. An unsalted digest is "
            f"reversible by dictionary attack, so it is refused rather than defaulted.",
        )

    return replace(conn, redaction_salt=salt)


def load_project(start: Path | None = None) -> ProjectConfig:
    """Discover and load .dbprint.yaml by walking up from `start` (cwd if None).

    Raises `ConfigError` when the file cannot be found, parsed, or validated.
    """

    cwd = (start or Path.cwd()).resolve()
    config_path = _find_project_config(cwd)

    if config_path is None:
        raise ConfigError(
            f"no .dbprint.yaml found in {cwd} or any parent directory. "
            f"Run `dbprint init` in the project root to create one.",
        )

    raw = _load_yaml(config_path)
    project_root = config_path.parent

    defaults = _parse_defaults(raw.get("defaults") or {})
    connections_raw = raw.get("connections") or {}

    if not isinstance(connections_raw, dict):
        raise ConfigError(
            f"{config_path}: `connections` must be a mapping, got {type(connections_raw).__name__}.",
        )

    if not connections_raw:
        raise ConfigError(f"{config_path}: at least one connection must be defined.")

    connections = {
        name: _parse_connection(name, body or {}, defaults, project_root, config_path)
        for name, body in connections_raw.items()
    }

    return ProjectConfig(project_root=project_root, connections=connections)


def _find_project_config(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        candidate = d / ".dbprint.yaml"

        if candidate.is_file():
            return candidate

    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML — {exc}") from exc

    if data is None:
        return {}

    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping, got {type(data).__name__}.")

    return data


def _parse_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse project-level defaults block; missing fields stay as None."""

    return {
        "statistics": raw.get("statistics"),
        "rules": raw.get("rules"),
        "redact": raw.get("redact"),
        "diff": raw.get("diff"),
        "max_age_days": raw.get("max_age_days"),
        "max_rows_scanned": raw.get("max_rows_scanned"),
        "output": raw.get("output"),
        "include": raw.get("include"),
        "exclude": raw.get("exclude"),
        "infer_relationships": raw.get("infer_relationships"),
        "materialize_sample": raw.get("materialize_sample"),
        "sketch_all_columns": raw.get("sketch_all_columns"),
        "raw": raw,
    }


def _parse_connection(
    name: str,
    body: dict[str, Any],
    defaults: dict[str, Any],
    project_root: Path,
    config_path: Path,
) -> ConnectionConfig:
    adapter = body.get("adapter")

    if adapter not in ADAPTERS:
        raise ConfigError(
            f"{config_path}: connection {name!r}: adapter must be one of {ADAPTERS}, got {adapter!r}.",
        )

    output_raw = body.get("output") or defaults.get("output") or OUTPUT_DEFAULT
    output = _resolve_path(output_raw, project_root)

    include = _coerce_pattern_list(
        body.get("include"),
        defaults.get("include"),
        ("*",),
        config_path,
        name,
        "include",
    )
    exclude = _coerce_pattern_list(
        body.get("exclude"),
        defaults.get("exclude"),
        (),
        config_path,
        name,
        "exclude",
    )

    max_age_days = _resolve_max_age_days(
        body.get("max_age_days"),
        defaults.get("max_age_days"),
        config_path,
        name,
    )
    max_rows_scanned = _resolve_max_rows_scanned(
        body.get("max_rows_scanned"),
        defaults.get("max_rows_scanned"),
        config_path,
        name,
    )

    statistics = _parse_statistics(
        body.get("statistics"),
        defaults["statistics"],
        config_path=config_path,
        conn_name=name,
    )
    _reject_misplaced_keys(defaults["raw"], body, config_path, name)
    rules = _parse_rules(defaults["rules"], config_path, name, source="defaults") + _parse_rules(
        body.get("rules"),
        config_path,
        name,
        source=f"connection {name!r}",
    )
    diff = _parse_diff(body.get("diff"), defaults["diff"], config_path, name)
    redact = _parse_redact(
        defaults["redact"],
        config_path,
        name,
        source="defaults",
    ) + _parse_redact(
        body.get("redact"),
        config_path,
        name,
        source=f"connection {name!r}",
    )

    assertions_raw = body.get("assertions")

    if assertions_raw is None:
        assertions_raw = {}
    elif not isinstance(assertions_raw, dict):
        raise ConfigError(
            f"{config_path}: connection {name!r}: `assertions` must be a mapping, "
            f"got {type(assertions_raw).__name__}.",
        )

    return ConnectionConfig(
        name=name,
        adapter=adapter,
        auto=bool(body.get("auto", False)),
        output=output,
        include=include,
        exclude=exclude,
        max_age_days=max_age_days,
        max_rows_scanned=max_rows_scanned,
        statistics=statistics,
        rules=rules,
        diff=diff,
        redact=redact,
        infer_relationships=_coerce_bool(
            body.get("infer_relationships"),
            defaults.get("infer_relationships"),
            default=INFER_RELATIONSHIPS_DEFAULT,
            field_label=f"connection {name!r}: infer_relationships",
            config_path=config_path,
        ),
        materialize_sample=_coerce_bool(
            body.get("materialize_sample"),
            defaults.get("materialize_sample"),
            default=MATERIALIZE_SAMPLE_DEFAULT,
            field_label=f"connection {name!r}: materialize_sample",
            config_path=config_path,
        ),
        sketch_all_columns=_coerce_bool(
            body.get("sketch_all_columns"),
            defaults.get("sketch_all_columns"),
            default=SKETCH_ALL_COLUMNS_DEFAULT,
            field_label=f"connection {name!r}: sketch_all_columns",
            config_path=config_path,
        ),
        assertions_raw=dict(assertions_raw),
    )


def _parse_statistics(
    conn_block: Any,
    defaults_block: Any,
    config_path: Path,
    conn_name: str,
) -> StatisticsConfig:
    """Merge defaults <- per-connection for statistics, validating each block where written."""

    integers = {
        **_coerce_statistics_integers(defaults_block, config_path, conn_name, "defaults"),
        **_coerce_statistics_integers(
            conn_block,
            config_path,
            conn_name,
            f"connection {conn_name!r}",
        ),
    }

    merged = {**(defaults_block or {}), **(conn_block or {})}
    percentiles_raw = merged.get("percentiles", list(PERCENTILES_DEFAULT))

    return StatisticsConfig(
        percentiles=_coerce_percentiles(percentiles_raw, config_path, conn_name),
        **integers,
    )


def _parse_rules(
    raw: Any,
    config_path: Path,
    conn_name: str,
    *,
    source: str,
) -> tuple[RuleConfig, ...]:
    """Parse one `rules:` list into ordered RuleConfigs; validate every entry."""

    if raw is None:
        return ()

    if not isinstance(raw, list):
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {source} rules must be a list of rule "
            f"mappings, got {type(raw).__name__}.",
        )

    return tuple(
        _parse_rule(entry, index, config_path, conn_name, source=source)
        for index, entry in enumerate(raw)
    )


def _parse_rule(
    raw: Any,
    index: int,
    config_path: Path,
    conn_name: str,
    *,
    source: str,
) -> RuleConfig:
    label = f"{source} rules[{index}]"

    if not isinstance(raw, dict):
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label} must be a mapping, "
            f"got {type(raw).__name__}.",
        )

    include = _coerce_pattern_list(
        raw.get("include"),
        None,
        ("*",),
        config_path,
        conn_name,
        "include",
        label=label,
    )
    exclude = _coerce_pattern_list(
        raw.get("exclude"),
        None,
        (),
        config_path,
        conn_name,
        "exclude",
        label=label,
    )
    min_rows = _coerce_min_rows(raw.get("min_rows"), config_path, conn_name, label)
    sample = _coerce_sample(raw.get("sample"), config_path, conn_name, label)
    predicate = _coerce_predicate(raw.get("filter"), config_path, conn_name, label)
    statistics = _coerce_statistics_overrides(
        raw.get("statistics"),
        config_path,
        conn_name,
        label,
    )
    max_age_days = _coerce_max_age_days(
        raw.get("max_age_days"),
        config_path,
        f"connection {conn_name!r}: {label}.max_age_days",
    )
    max_rows_scanned = _coerce_max_rows_scanned(
        raw.get("max_rows_scanned"),
        config_path,
        f"connection {conn_name!r}: {label}.max_rows_scanned",
    )

    # A rule that changes nothing is a mis-nested or misspelled key ignored keys would hide.
    if (
        sample is None
        and predicate is None
        and not statistics
        and max_age_days is None
        and max_rows_scanned is None
    ):
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label} sets no sample, filter, statistics, "
            f"max_age_days or max_rows_scanned, so it would do nothing. Remove it or give it a "
            f"setting.",
        )

    # A rule that sets something but matches nothing. An absent `include` defaults to
    # `["*"]`, so only an explicit empty list reaches here.
    if not include:
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label}.include is an empty list, so the "
            f"rule matches no table and would do nothing. Remove it, or list the patterns the "
            f"rule should match.",
        )

    if sample is not None and predicate is not None:
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label} sets both sample and filter. "
            f"A table is narrowed by a predicate or by a fraction, never both - keep one, or "
            f"widen the predicate to describe the rows you want.",
        )

    return RuleConfig(
        include=include,
        exclude=exclude,
        min_rows=min_rows,
        sample=sample,
        filter=predicate,
        max_rows_scanned=max_rows_scanned,
        statistics=statistics,
        max_age_days=max_age_days,
        label=label,
    )


_REDACTION_PRIMITIVES: tuple[str, ...] = ("mask", "drop", "hash")

# The closed vocabularies a redact rule can target, read from the modules that define them
# so a new value needs no second edit here. `columns` globs over names, so it has no set.
_SENSITIVITIES: frozenset[str] = frozenset(get_args(Sensitivity))
_LOOKS_LIKE_PATTERNS: frozenset[str] = frozenset(get_args(LooksLike))


def _parse_redact(
    raw: Any,
    config_path: Path,
    conn_name: str,
    *,
    source: str,
) -> tuple[RedactRule, ...]:
    """Parse one `redact:` list into ordered RedactRules; validate every entry."""

    if raw is None:
        return ()

    if not isinstance(raw, list):
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {source} `redact` must be a list of rule "
            f"mappings, got {type(raw).__name__}.",
        )

    return tuple(
        _parse_redact_rule(entry, index, config_path, conn_name, source=source)
        for index, entry in enumerate(raw)
    )


def _parse_redact_rule(
    raw: Any,
    index: int,
    config_path: Path,
    conn_name: str,
    *,
    source: str,
) -> RedactRule:
    label = f"{source} redact[{index}]"

    if not isinstance(raw, dict):
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label} must be a mapping, "
            f"got {type(raw).__name__}.",
        )

    columns = _coerce_pattern_list(
        raw.get("columns"),
        None,
        (),
        config_path,
        conn_name,
        "columns",
        label=label,
    )
    sensitivity = _coerce_vocabulary(
        _coerce_pattern_list(
            raw.get("sensitivity"),
            None,
            (),
            config_path,
            conn_name,
            "sensitivity",
            label=label,
        ),
        _SENSITIVITIES,
        config_path,
        conn_name,
        "sensitivity",
        label,
    )
    looks_like = _coerce_vocabulary(
        _coerce_pattern_list(
            raw.get("looks_like"),
            None,
            (),
            config_path,
            conn_name,
            "looks_like",
            label=label,
        ),
        _LOOKS_LIKE_PATTERNS,
        config_path,
        conn_name,
        "looks_like",
        label,
    )

    # A rule naming no target would cover every column, never what an author means.
    if not (columns or sensitivity or looks_like):
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label} names no `columns`, `sensitivity` "
            f"or `looks_like`, so it targets nothing. Give it one of the three.",
        )

    primitive = raw.get("with", "mask")

    if primitive not in _REDACTION_PRIMITIVES:
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label}.with must be one of "
            f"{_REDACTION_PRIMITIVES}, got {primitive!r}.",
        )

    return RedactRule(
        columns=columns,
        sensitivity=sensitivity,
        looks_like=looks_like,
        with_=primitive,
    )


def _reject_misplaced_keys(
    defaults_raw: dict[str, Any],
    body: dict[str, Any],
    config_path: Path,
    conn_name: str,
) -> None:
    """Refuse a `scope:` block and the rule keys authors hoist above it.

    Unknown keys drop silently, so any of these shapes would load clean and narrow nothing.
    The list is not a schema - each entry is a place authors land often enough to name.
    """

    for where, block in (("defaults", defaults_raw), (f"connection {conn_name!r}", body)):
        if "scope" in block:
            raise ConfigError(
                f"{config_path}: {where}: `scope` is not read here. Move `sample` and `filter` "
                f"into a `rules:` list, where each entry carries its own `include`/`exclude`.",
            )

        for key, remediation_tail in _RULE_ONLY_KEYS.items():
            if key in block:
                raise ConfigError(
                    f"{config_path}: {where}: `{key}` is not read here - it narrows a rule's own "
                    f"tables, not the connection. Move it into a `rules:` list: "
                    f'rules: [{{include: ["schema.table"], {key}: {block[key]!r}'
                    f"{remediation_tail}}}].",
                )


def _coerce_min_rows(value: Any, config_path: Path, conn_name: str, label: str) -> int | None:
    """Validate a rule's size condition; absent stays absent.

    Zero is refused, not read as a no-op: it silently excludes tables the catalog cannot size.
    """

    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label}.min_rows must be a positive "
            f"integer, got {value!r}.",
        )

    if value < 1:
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label}.min_rows is {value}, which selects "
            f"every table the catalog can size and no table it cannot. Drop the key to let the "
            f"rule match every table it names.",
        )

    return value


def _coerce_max_rows_scanned(value: Any, config_path: Path, field_label: str) -> int | None:
    """Validate one row-count ceiling; absent stays absent.

    Zero or negative is refused: "read nothing" is not a narrowing a producer can act on.
    """

    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{config_path}: {field_label} must be a positive integer, got {value!r}.",
        )

    if value < 1:
        raise ConfigError(
            f"{config_path}: {field_label} is {value}, which forbids reading any row. Use a "
            f"positive row count, or drop the key to leave the table unbounded.",
        )

    return value


def _coerce_sample(value: Any, config_path: Path, conn_name: str, label: str) -> float | None:
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label}.sample must be a number in "
            f"(0, 1], got {value!r}.",
        )

    if not 0 < value <= 1:
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label}.sample {value} is outside (0, 1].",
        )

    return float(value)


def _coerce_predicate(value: Any, config_path: Path, conn_name: str, label: str) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label}.filter must be a non-empty string, "
            f"got {value!r}.",
        )

    return value.strip()


def _coerce_statistics_overrides(
    raw: Any,
    config_path: Path,
    conn_name: str,
    label: str,
) -> dict[str, Any]:
    """Validate a partial statistics block into StatisticsConfig constructor kwargs."""

    overrides = _coerce_statistics_integers(raw, config_path, conn_name, label)

    if raw and "percentiles" in raw:
        overrides["percentiles"] = _coerce_percentiles(
            raw["percentiles"],
            config_path,
            conn_name,
            label=label,
        )

    return overrides


def _coerce_statistics_integers(
    raw: Any,
    config_path: Path,
    conn_name: str,
    label: str,
) -> dict[str, Any]:
    """Validate the integer statistics keys present in one block, naming where it sits.

    Shared by the connection, `defaults` and rule levels, so one malformed value reports one
    way. Absent or empty means no overrides; a non-mapping is refused, not read as absent.
    """

    if raw is None:
        return {}

    if not isinstance(raw, dict):
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label}.statistics must be a mapping, "
            f"got {type(raw).__name__}.",
        )

    overrides: dict[str, Any] = {}

    for key in _STATISTICS_INT_KEYS:
        if key not in raw:
            continue

        value = raw[key]

        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(
                f"{config_path}: connection {conn_name!r}: {label}.statistics.{key}: expected "
                f"integer, got {value!r}.",
            )

        overrides[key] = value

    return overrides


def _parse_diff(
    conn_block: Any,
    defaults_block: Any,
    config_path: Path,
    conn_name: str,
) -> DiffConfig:
    """Merge defaults <- per-connection for diff; deep-merge stat_change_threshold.

    Each block is validated where it is written, so an error names the block the author typed.
    """

    defaults_diff = _diff_block(defaults_block, config_path, conn_name, "defaults")
    conn_diff = _diff_block(conn_block, config_path, conn_name, f"connection {conn_name!r}")

    thresholds = dict(STAT_CHANGE_THRESHOLD_DEFAULT)
    thresholds.update(
        _coerce_stat_change_threshold(
            defaults_diff.get("stat_change_threshold"),
            config_path,
            conn_name,
            "defaults",
        ),
    )
    thresholds.update(
        _coerce_stat_change_threshold(
            conn_diff.get("stat_change_threshold"),
            config_path,
            conn_name,
            f"connection {conn_name!r}",
        ),
    )

    return DiffConfig(stat_change_threshold=thresholds)


def _diff_block(raw: Any, config_path: Path, conn_name: str, label: str) -> dict[str, Any]:
    """One `diff` block as a mapping; absent or empty means the spec defaults govern.

    Any other value under the key is refused rather than read as absent.
    """

    if raw is None:
        return {}

    if not isinstance(raw, dict):
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label}.diff must be a mapping, "
            f"got {type(raw).__name__}.",
        )

    return raw


def _coerce_stat_change_threshold(
    raw: Any,
    config_path: Path,
    conn_name: str,
    label: str,
) -> dict[str, float]:
    """Validate one `stat_change_threshold` block into per-stat fractions.

    An unknown key is refused rather than left to fall back to `default` silently; accepted
    keys are SPEC 2.6.9's. Absent or empty means no overrides.
    """

    where = f"{config_path}: connection {conn_name!r}: {label}.diff.stat_change_threshold"

    if raw is None:
        return {}

    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping, got {type(raw).__name__}.")

    out: dict[str, float] = {}

    for key, value in raw.items():
        if key not in STAT_CHANGE_THRESHOLD_DEFAULT:
            accepted = ", ".join(sorted(STAT_CHANGE_THRESHOLD_DEFAULT))

            raise ConfigError(
                f"{where}.{key} is not a threshold this reads, so it would be ignored and "
                f"`default` applied in its place. Accepted keys: {accepted}.",
            )

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}.{key}: expected a number in [0, 1], got {value!r}.")

        if not 0 <= value <= 1:
            raise ConfigError(
                f"{where}.{key} is {value}, which is outside [0, 1] - a threshold is the "
                f"fraction of a value's own size that a change has to exceed to be shown.",
            )

        out[key] = float(value)

    return out


def _coerce_percentiles(
    value: Any,
    config_path: Path,
    conn_name: str,
    *,
    label: str = "",
) -> tuple[int, ...]:
    """Validate a percentiles list; `label` names the rule when the list came from one."""

    subject = f"{label}.statistics.percentiles" if label else "percentiles"
    prefix = f"{subject}: " if label else ""

    if not isinstance(value, list) or not value:
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {subject} must be a non-empty list of integers in 1..99.",
        )

    result: list[int] = []

    for v in value:
        if not isinstance(v, int) or isinstance(v, bool):
            raise ConfigError(
                f"{config_path}: connection {conn_name!r}: {prefix}percentile {v!r} is not an integer.",
            )

        if not 1 <= v <= 99:
            raise ConfigError(
                f"{config_path}: connection {conn_name!r}: {prefix}percentile {v} out of range (1..99).",
            )

        result.append(v)

    return tuple(result)


def _coerce_pattern_list(
    conn_value: Any,
    default_value: Any,
    default: tuple[str, ...],
    config_path: Path,
    conn_name: str,
    field: str,
    *,
    label: str = "",
) -> tuple[str, ...]:
    """Coerce a selector list; `label` names the rule when the list came from one."""

    source = conn_value if conn_value is not None else default_value

    if source is None:
        return default

    if not isinstance(source, list) or not all(isinstance(p, str) for p in source):
        qualified = f"{label}.{field}" if label else field

        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {qualified} must be a list of strings, "
            f"got {source!r}.",
        )

    return tuple(p.lower() for p in source)


def _coerce_vocabulary(
    values: tuple[str, ...],
    vocabulary: frozenset[str],
    config_path: Path,
    conn_name: str,
    field: str,
    label: str,
) -> tuple[str, ...]:
    """Refuse a redact target outside its closed set, naming the accepted values.

    A misspelled target covers nothing but loads clean and looks redacted. Runs after
    `_coerce_pattern_list` lowercases, so `PERSONAL_NAME` still matches.
    """

    unknown = [v for v in values if v not in vocabulary]

    if unknown:
        raise ConfigError(
            f"{config_path}: connection {conn_name!r}: {label}.{field} entries must each be one "
            f"of {tuple(sorted(vocabulary))}, got {unknown[0]!r}.",
        )

    return values


def _resolve_max_age_days(
    conn_value: Any,
    default_value: Any,
    config_path: Path,
    conn_name: str,
) -> int:
    """Settle the connection's freshness threshold from its own block or `defaults`.

    Both blocks are validated even when the connection wins, so a bad `defaults` value errors.
    """

    own = _coerce_max_age_days(conn_value, config_path, f"connection {conn_name!r}: max_age_days")
    inherited = _coerce_max_age_days(default_value, config_path, "defaults: max_age_days")

    if own is not None:
        return own

    return inherited if inherited is not None else MAX_AGE_DAYS_DEFAULT


def _resolve_max_rows_scanned(
    conn_value: Any,
    default_value: Any,
    config_path: Path,
    conn_name: str,
) -> int | None:
    """Settle the connection's row-count ceiling from its own block or `defaults`.

    Absence means no ceiling rather than a default, so no catalog estimate is fetched for it.
    """

    own = _coerce_max_rows_scanned(
        conn_value,
        config_path,
        f"connection {conn_name!r}: max_rows_scanned",
    )
    inherited = _coerce_max_rows_scanned(default_value, config_path, "defaults: max_rows_scanned")

    return own if own is not None else inherited


def _resolve_ceiling(cap: int, estimate: int | None) -> float | None:
    """The fraction a row-count ceiling resolves to against a catalog estimate.

    Pure in `cap` and `estimate` alone (SPEC 2.2.8's determinism), and snapped DOWN the
    `CEILING_GRID_RATIO` grid so it never reads past the ceiling. `None` means no narrowing:
    the estimate is absent, or the cap already covers the table.
    """

    if estimate is None or estimate <= 0 or cap >= estimate:
        return None

    raw = cap / estimate
    steps = math.floor(math.log(raw, CEILING_GRID_RATIO))
    snapped = CEILING_GRID_RATIO**steps

    while snapped > raw:
        steps -= 1
        snapped = CEILING_GRID_RATIO**steps

    return snapped


def _coerce_max_age_days(value: Any, config_path: Path, field_label: str) -> int | None:
    """Validate one freshness threshold; absent stays absent.

    Zero is legal - `age_days < max_age_days` is always false, so every table re-extracts.
    A negative is refused: it would hold every table permanently stale.
    """

    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{config_path}: {field_label}: expected integer, got {value!r}.")

    if value < 0:
        raise ConfigError(
            f"{config_path}: {field_label} is {value}, which no print can ever satisfy: every "
            f"table re-extracts on every run and `check` reports every one of them stale. Use 0 "
            f"to ask for that deliberately, or a positive number of days.",
        )

    return value


def _coerce_bool(
    conn_value: Any,
    default_value: Any,
    default: bool,
    field_label: str,
    config_path: Path,
) -> bool:
    source = conn_value if conn_value is not None else default_value

    if source is None:
        return default

    if not isinstance(source, bool):
        raise ConfigError(f"{config_path}: {field_label}: expected true or false, got {source!r}.")

    return source


def _resolve_path(value: str | os.PathLike[str], project_root: Path) -> Path:
    p = Path(value).expanduser()

    if not p.is_absolute():
        p = (project_root / p).resolve()

    return p
