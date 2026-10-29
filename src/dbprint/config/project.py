"""Project config (`.dbprint.yaml`) - dataclasses, the one merge rule, upward-discovery loader.

Values cascade `defaults` -> connection -> each matching rule; the file is schema-validated first.
"""

from __future__ import annotations

import copy
import math
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from dbprint.spec.profile import DISABLEABLE
from .duration import parse_duration_seconds
from .merge import merge, merge_into
from .read import BUILTIN_READ, MODE_PARAMETERS, ObjectKind, ReadMode, fold_read
from .selectors import match
from .validation import format_problems, validate_project


ENUMERATION_THRESHOLD_DEFAULT = 50
TOP_N_VALUES_DEFAULT = 20
TOP_N_NULL_PATTERNS_DEFAULT = 20
LOOKS_LIKE_SAMPLE_SIZE_DEFAULT = 1000
PERCENTILES_DEFAULT = (1, 25, 50, 75, 99)
MAX_PARTS_DEFAULT = 20
MAX_PART_DEPTH_DEFAULT = 3
MAX_AGE_DAYS_DEFAULT = 7

# A `read.tables.max_rows` ceiling snaps its resolved fraction down this grid, so estimate drift
# stays within a step (ratio in CONFIG.md).
CEILING_GRID_RATIO = 1.1
STAT_CHANGE_THRESHOLD_DEFAULT = {
    "cardinality_ratio": 0.02,
    "percentile_pct": 0.05,
    "values_coverage": 0.05,
    "default": 0.01,
}
OUTPUT_DEFAULT = "prints"
MATERIALIZE_SAMPLE_DEFAULT = True
PARALLELISM_DEFAULT = 1


BUILTIN_LABEL = "built-in default"

RuleOutcome = Literal["applied", "name_mismatch", "below_min_rows", "size_unknown"]

# Credential key for the redaction salt - it lives with the passwords, not in `.dbprint.yaml`.
REDACTION_SALT_KEY = "redaction_salt"

ADAPTERS: tuple[str, ...] = (
    "postgres",
    "snowflake",
    "mysql",
    "duckdb",
    "clickhouse",
    "redshift",
    "databricks",
    "bigquery",
)

AdapterName = Literal[
    "postgres",
    "snowflake",
    "mysql",
    "duckdb",
    "clickhouse",
    "redshift",
    "databricks",
    "bigquery",
]


class ConfigError(ValueError):
    """Raised when the project config file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class StatisticsConfig:
    """What an adapter computes per SPEC 2.2; a switched-off feature arrives as a zero bound."""

    enumeration_threshold: int = ENUMERATION_THRESHOLD_DEFAULT
    top_n_values: int = TOP_N_VALUES_DEFAULT
    top_n_null_patterns: int = TOP_N_NULL_PATTERNS_DEFAULT
    looks_like_sample_size: int = LOOKS_LIKE_SAMPLE_SIZE_DEFAULT
    percentiles: tuple[int, ...] = PERCENTILES_DEFAULT
    max_parts: int = MAX_PARTS_DEFAULT
    max_part_depth: int = MAX_PART_DEPTH_DEFAULT


@dataclass(frozen=True)
class TablesConfig:
    """Which tables a connection profiles; a rule never widens this."""

    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunConfig:
    """How a run talks to the database: sessions at once, and the per-statement limit in seconds."""

    parallelism: int = PARALLELISM_DEFAULT
    statement_timeout: int | None = None


@dataclass(frozen=True)
class FreshnessConfig:
    """How old a print may grow before `generate` re-profiles it and `check` fails it."""

    max_age_days: int = MAX_AGE_DAYS_DEFAULT


@dataclass(frozen=True)
class ReadConfig:
    """How one object type's rows are read; only the parameters of `mode` are set."""

    mode: ReadMode
    fraction: float | None = None
    max_rows: int | None = None
    materialize: bool = MATERIALIZE_SAMPLE_DEFAULT
    filter: str | None = None


@dataclass(frozen=True)
class ReadSettings:
    """`profile.read`: tables are read whole and views described from the catalog by default."""

    tables: ReadConfig = field(default_factory=lambda: ReadConfig(mode="full"))
    views: ReadConfig = field(default_factory=lambda: ReadConfig(mode="catalog"))


@dataclass(frozen=True)
class RedactRule:
    """One redaction rule: which columns it covers, and what it does to their values.

    Any of `columns` (globs over `<table>.<column>`), `sensitivity` or `looks_like` covers a
    column; rules apply in declaration order, and the last match wins.
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
class StatisticsProfile:
    """Per-column statistics tuning (SPEC 2.2)."""

    enumeration_threshold: int = ENUMERATION_THRESHOLD_DEFAULT
    top_n_values: int = TOP_N_VALUES_DEFAULT
    percentiles: tuple[int, ...] = PERCENTILES_DEFAULT


@dataclass(frozen=True)
class LooksLikeProfile:
    """Value-shape detection over a sample of each text-like column (SPEC 4.1)."""

    enabled: bool = True
    sample_size: int = LOOKS_LIKE_SAMPLE_SIZE_DEFAULT


@dataclass(frozen=True)
class NullPatternsProfile:
    """The table's `null_patterns` block (SPEC 2.2.13)."""

    enabled: bool = True
    top_n: int = TOP_N_NULL_PATTERNS_DEFAULT


@dataclass(frozen=True)
class PartsProfile:
    """Descent into array, record, map and JSON columns (SPEC 2.2.18)."""

    enabled: bool = True
    max: int = MAX_PARTS_DEFAULT
    max_depth: int = MAX_PART_DEPTH_DEFAULT


@dataclass(frozen=True)
class TimelineProfile:
    """The table's `timeline` block and each column's `populated` (SPEC 2.2.16)."""

    enabled: bool = True


@dataclass(frozen=True)
class SketchesProfile:
    """Value sketches (SPEC 2.2.14): the required columns, or every sketchable one."""

    enabled: bool = True
    columns: Literal["required", "all"] = "required"


@dataclass(frozen=True)
class RelationshipsProfile:
    """Foreign keys inferred from column naming (SPEC 2.3.8)."""

    infer: bool = True


@dataclass(frozen=True)
class ProfileConfig:
    """What is measured and how; resolved per connection, and per table once rules apply."""

    freshness: FreshnessConfig = field(default_factory=FreshnessConfig)
    statistics: StatisticsProfile = field(default_factory=StatisticsProfile)
    read: ReadSettings = field(default_factory=ReadSettings)
    looks_like: LooksLikeProfile = field(default_factory=LooksLikeProfile)
    null_patterns: NullPatternsProfile = field(default_factory=NullPatternsProfile)
    parts: PartsProfile = field(default_factory=PartsProfile)
    timeline: TimelineProfile = field(default_factory=TimelineProfile)
    sketches: SketchesProfile = field(default_factory=SketchesProfile)
    relationships: RelationshipsProfile = field(default_factory=RelationshipsProfile)
    redact: tuple[RedactRule, ...] = ()

    def disabled(self) -> tuple[str, ...]:
        """The measurements this profile switches off, sorted, as SPEC 2.2.21 names them."""

        off = {
            "looks_like": not self.looks_like.enabled,
            "null_patterns": not self.null_patterns.enabled,
            "parts": not self.parts.enabled,
            "relationships.infer": not self.relationships.infer,
            "sketches": not self.sketches.enabled,
            "timeline": not self.timeline.enabled,
        }

        return tuple(name for name in DISABLEABLE if off[name])

    def adapter_config(self) -> StatisticsConfig:
        """The adapter's statistics bounds; a switched-off sample or descent is a zero bound."""

        return StatisticsConfig(
            enumeration_threshold=self.statistics.enumeration_threshold,
            top_n_values=self.statistics.top_n_values,
            top_n_null_patterns=self.null_patterns.top_n,
            looks_like_sample_size=self.looks_like.sample_size if self.looks_like.enabled else 0,
            percentiles=self.statistics.percentiles,
            max_parts=self.parts.max if self.parts.enabled else 0,
            max_part_depth=self.parts.max_depth,
        )


@dataclass(frozen=True)
class RuleMatch:
    """The tables a rule governs: name patterns, and optionally a minimum catalog row count."""

    tables: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    min_rows: int | None = None


@dataclass(frozen=True)
class RuleConfig:
    """One profiling rule: the tables it matches and the partial `profile` it merges over them.

    `label` is the rule's config path (`defaults.rules[0]`), empty for a rule built in code.
    """

    match: RuleMatch = field(default_factory=RuleMatch)
    profile: Mapping[str, Any] = field(default_factory=dict)
    label: str = ""

    def matches(self, fqn: str, row_count: int | None = None) -> bool:
        """True when this rule governs a table of that name at that size."""

        return self.verdict(fqn, row_count) == "applied"

    def verdict(self, fqn: str, row_count: int | None = None) -> RuleOutcome:
        """Whether this rule governs a table of that name at that size, and if not, why.

        Without a `row_count` (catalog estimate) a `min_rows` rule is `size_unknown`, never applied.
        """

        if not self.matches_name(fqn):
            return "name_mismatch"

        if self.match.min_rows is None:
            return "applied"

        if row_count is None:
            return "size_unknown"

        return "applied" if row_count >= self.match.min_rows else "below_min_rows"

    def matches_name(self, fqn: str) -> bool:
        """True when the name matchers admit `fqn`, disregarding any size condition."""

        return match(fqn, list(self.match.tables), list(self.match.exclude))

    def written_mode(self, kind: ObjectKind) -> ReadMode | None:
        """The `read.<kind>.mode` this rule writes, if it writes one."""

        read = self.profile.get("read")
        block = read.get(kind) if isinstance(read, Mapping) else None

        return block.get("mode") if isinstance(block, Mapping) else None

    def sets(self, *path: str) -> bool:
        """True when this rule's `profile` writes the key at `path`."""

        node: Any = self.profile

        for key in path:
            if not isinstance(node, Mapping) or key not in node:
                return False

            node = node[key]

        return True


@dataclass(frozen=True)
class RuleVerdict:
    """One rule's outcome for one table: its config path, its matcher, and whether it applied."""

    label: str
    match: RuleMatch
    outcome: RuleOutcome


@dataclass(frozen=True)
class ResolvedProfile:
    """A table's merged `profile` tree, the layer that set each leaf, and every rule's verdict.

    A list is one leaf, since a later list replaces an earlier one whole.
    """

    values: Mapping[str, Any]
    origins: Mapping[tuple[str, ...], str]
    rules: tuple[RuleVerdict, ...] = ()


@dataclass(frozen=True)
class TableSettings:
    """Effective settings for one object once the rule cascade has been applied.

    `explicit`: a matching rule wrote `mode`. A `max_rows` ceiling resolves into `sample` (SPEC 2.2.8).
    """

    profile: ProfileConfig
    max_age_days: int
    mode: ReadMode = "full"
    explicit: bool = False
    sample: float | None = None
    filter: str | None = None
    materialize: bool = MATERIALIZE_SAMPLE_DEFAULT
    max_rows_scanned: int | None = None
    matched_rules: tuple[str, ...] = ()

    @property
    def statistics(self) -> StatisticsConfig:
        """The bounds the adapter computes this object's statistics under."""

        return self.profile.adapter_config()


@dataclass(frozen=True)
class DiffConfig:
    """Tuning for diff rendering per SPEC 2.6.9."""

    stat_change_threshold: dict[str, float] = field(
        default_factory=lambda: dict(STAT_CHANGE_THRESHOLD_DEFAULT),
    )


@dataclass(frozen=True)
class ConnectionConfig:
    """Per-connection settings; `tables` gate what is profiled, `profile` and `rules` say how."""

    name: str
    adapter: AdapterName
    auto: bool = False
    output: Path = field(default_factory=lambda: Path(OUTPUT_DEFAULT))
    tables: TablesConfig = field(default_factory=TablesConfig)
    run: RunConfig = field(default_factory=RunConfig)
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    rules: tuple[RuleConfig, ...] = ()
    diff: DiffConfig = field(default_factory=DiffConfig)
    assertions_raw: dict[str, Any] = field(default_factory=dict)
    redaction_salt: str | None = None
    dbt_manifest: Path | None = None
    # The layer each leaf of `profile` came from; a leaf absent here is the connection's own.
    profile_origins: Mapping[tuple[str, ...], str] = field(default_factory=dict)

    @property
    def print_root(self) -> Path:
        """The directory this connection's print lives in."""

        return self.output / self.name

    @property
    def reads_row_counts(self) -> bool:
        """True when any rule carries a size condition, or a ceiling is in force anywhere.

        Constant for a whole run, since the rule list is fixed at load. The engine reads it
        to decide whether to fetch a catalog estimate at all.
        """

        return self.profile.read.tables.max_rows is not None or any(
            rule.match.min_rows is not None or rule.sets("read", "tables", "max_rows")
            for rule in self.rules
        )

    def size_conditions_name(self, fqn: str) -> bool:
        """True when some rule or the connection's own ceiling would read a count for this table.

        The size test itself is disregarded: this separates "size mattered and could not be
        read" from "no size condition governs this table". A connection-level ceiling governs
        every table, unlike a rule's own condition.
        """

        if self.profile.read.tables.max_rows is not None:
            return True

        return any(
            (rule.match.min_rows is not None or rule.sets("read", "tables", "max_rows"))
            and rule.matches_name(fqn)
            for rule in self.rules
        )

    @property
    def any_infers(self) -> bool:
        """True when the connection or any rule leaves naming inference on for some table."""

        return self.profile.relationships.infer or any(
            rule.profile.get("relationships", {}).get("infer") is True for rule in self.rules
        )

    def min_rows_conditions_name(self, fqn: str) -> bool:
        """True when some rule selecting this name carries `min_rows` - narrower than
        `size_conditions_name`, a `read.tables.max_rows` ceiling not affecting `max_age_days`.
        """

        return any(
            rule.match.min_rows is not None and rule.matches_name(fqn) for rule in self.rules
        )

    def min_rows_decides_mode(self, fqn: str, kind: ObjectKind) -> bool:
        """True when some rule selecting this name carries `min_rows` and writes `read.<kind>.mode`."""

        return any(
            rule.match.min_rows is not None
            and rule.matches_name(fqn)
            and rule.written_mode(kind) is not None
            for rule in self.rules
        )

    def redaction_for(
        self,
        qualified: str,
        sensitivity: str | None,
        looks_like: str | None,
    ) -> str | None:
        """The primitive covering one column, or None when nothing redacts it; last match wins."""

        primitive: str | None = None

        for rule in self.profile.redact:
            if rule.covers(qualified, sensitivity, looks_like):
                primitive = rule.with_

        return primitive

    def redaction_for_part(
        self,
        qualified: str,
        column_sensitivity: str | None,
        column_looks_like: str | None,
        detections: tuple[tuple[str | None, str | None], ...],
    ) -> str | None:
        """The primitive covering one part of the column `qualified` names (SPEC 2.2.9).

        A rule covers it through its column or any `detections` pair (own or enclosing); last wins.
        """

        primitive: str | None = None

        for rule in self.profile.redact:
            if rule.covers(qualified, column_sensitivity, column_looks_like) or any(
                (sensitivity is not None and sensitivity in rule.sensitivity)
                or (looks_like is not None and looks_like in rule.looks_like)
                for sensitivity, looks_like in detections
            ):
                primitive = rule.with_

        return primitive

    def settings_for(
        self,
        fqn: str,
        kind: Literal["table", "view"],
        row_count: int | None = None,
    ) -> TableSettings:
        """Effective settings for one object: the connection's profile, then every matching rule.

        `row_count` (catalog estimate) gates size rules and resolves a ceiling; omitted, neither applies.
        """

        resolved = self.resolve_profile(fqn, row_count)
        key: ObjectKind = "views" if kind == "view" else "tables"
        applied = [v.outcome == "applied" for v in resolved.rules]
        tree = resolved.values
        block = tree["read"][key]
        mode = block["mode"]
        cap = block.get("max_rows") if mode == "sample" else None

        return TableSettings(
            profile=_typed_profile(tree),
            max_age_days=tree["freshness"]["max_age_days"],
            mode=mode,
            explicit=any(
                rule.written_mode(key) is not None
                for rule, hit in zip(self.rules, applied, strict=True)
                if hit
            ),
            sample=(_resolve_ceiling(cap, row_count) if cap is not None else block.get("fraction")),
            filter=block.get("filter") if mode == "filter" else None,
            materialize=block.get("materialize", MATERIALIZE_SAMPLE_DEFAULT),
            max_rows_scanned=cap,
            matched_rules=tuple(v.label for v in resolved.rules if v.outcome == "applied"),
        )

    def resolve_profile(self, fqn: str | None, row_count: int | None = None) -> ResolvedProfile:
        """The profile one table resolves to, with each leaf's origin and every rule's verdict.

        With no `fqn`, the connection's own profile and no rule layers.
        """

        own = f"connections.{self.name}.profile"
        tree = _profile_tree(self.profile)
        origins = {path: self.profile_origins.get(path, own) for path in _leaf_paths(tree)}
        blocks = {
            kind: _read_tree(
                getattr(self.profile.read, kind),
                materialize_written=("read", kind, "materialize") in self.profile_origins,
            )
            for kind in BUILTIN_READ
        }
        read_origins = {
            kind: {key: self.profile_origins.get(("read", kind, key), own) for key in block}
            for kind, block in blocks.items()
        }
        verdicts: list[RuleVerdict] = []

        for index, rule in enumerate(self.rules if fqn is not None else ()):
            label = rule.label or f"rules[{index}]"
            verdicts.append(RuleVerdict(label, rule.match, rule.verdict(str(fqn), row_count)))

            if verdicts[-1].outcome != "applied":
                continue

            merge_into(tree, origins, {k: v for k, v in rule.profile.items() if k != "read"}, label)

            for kind in BUILTIN_READ:
                if written := (rule.profile.get("read") or {}).get(kind):
                    blocks[kind] = fold_read(blocks[kind], written, label, read_origins[kind])

        for kind, block in blocks.items():
            if block["mode"] == "sample" and "materialize" not in block:
                block["materialize"] = MATERIALIZE_SAMPLE_DEFAULT
                read_origins[kind]["materialize"] = BUILTIN_LABEL

        # A rule writing no `mode` is validated against the connection's, but an earlier matching
        # rule may have switched it; a parameter the final mode does not take stays inert.
        tree["read"] = {
            kind: {
                key: value
                for key, value in block.items()
                if key == "mode" or key in MODE_PARAMETERS[block["mode"]]
            }
            for kind, block in blocks.items()
        }
        origins.update(
            {
                ("read", kind, key): label
                for kind, keys in read_origins.items()
                for key, label in keys.items()
                if key in tree["read"][kind]
            },
        )

        return ResolvedProfile(values=tree, origins=origins, rules=tuple(verdicts))


@dataclass(frozen=True)
class ProjectConfig:
    """Loaded project config plus the resolved project root directory."""

    project_root: Path
    connections: dict[str, ConnectionConfig]


def bind_redaction_salt(conn: ConnectionConfig, salt: str | None) -> ConnectionConfig:
    """Attach the resolved salt; a `hash` rule without one is refused, never defaulted.

    A variable exported empty resolves to `""`, so the test is for material, not for presence.
    """

    material = bool(salt and salt.strip())

    if not material and any(rule.with_ == "hash" for rule in conn.profile.redact):
        raise ConfigError(
            f"connection {conn.name!r}: a `redact` rule uses `with: hash` but no "
            f"`{REDACTION_SALT_KEY}` carries a value. Add it to the connection's entry in "
            f"~/.dbprint/connections.yaml, or set "
            f"DBPRINT_{conn.name.upper()}_{REDACTION_SALT_KEY.upper()} to something other than "
            f"an empty value. An unsalted digest is reversible by dictionary attack, so it is "
            f"refused rather than defaulted.",
        )

    return replace(conn, redaction_salt=salt if material else None)


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

    return _load_project_from(config_path)


def load_project_at(locator: str | Path) -> ProjectConfig:
    """Load .dbprint.yaml at an exact location - the file itself or its direct parent directory.

    Never walks up or scans down; raises `ConfigError` naming the exact path checked.
    """

    path = Path(locator).expanduser()

    if not path.is_absolute():
        path = path.resolve()

    config_path = path if path.name == ".dbprint.yaml" else path / ".dbprint.yaml"

    if not config_path.is_file():
        raise ConfigError(
            f"no .dbprint.yaml at {config_path}. --project names an exact location - the "
            f"config file itself, or the directory that directly contains it. No upward walk, "
            f"no downward scan.",
        )

    return _load_project_from(config_path)


def _load_project_from(config_path: Path) -> ProjectConfig:
    """Parse one resolved `.dbprint.yaml` path into a `ProjectConfig`."""

    raw = _load_yaml(config_path)

    if problems := validate_project(raw):
        raise ConfigError(format_problems(config_path, problems))

    project_root = config_path.parent
    defaults = raw.get("defaults") or {}
    connections = {
        name: _build_connection(name, body, defaults, project_root)
        for name, body in raw["connections"].items()
    }

    return ProjectConfig(project_root=project_root, connections=connections)


def _find_project_config(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        candidate = d / ".dbprint.yaml"

        if candidate.is_file():
            return candidate

    return None


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_ConfigLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML - {exc}") from exc


class _ConfigLoader(yaml.SafeLoader):
    pass


# PyYAML's date-time keeps six fractional digits, where an assertion bound compares nine.
_ConfigLoader.add_constructor("tag:yaml.org,2002:timestamp", _ConfigLoader.construct_yaml_str)


_BUILTIN_PROFILE: dict[str, Any] = {
    "freshness": asdict(FreshnessConfig()),
    "statistics": {**asdict(StatisticsProfile()), "percentiles": list(PERCENTILES_DEFAULT)},
    "read": {kind: dict(block) for kind, block in BUILTIN_READ.items()},
    "looks_like": asdict(LooksLikeProfile()),
    "null_patterns": asdict(NullPatternsProfile()),
    "parts": asdict(PartsProfile()),
    "timeline": asdict(TimelineProfile()),
    "sketches": asdict(SketchesProfile()),
    "relationships": asdict(RelationshipsProfile()),
    "redact": [],
}


def _build_connection(
    name: str,
    body: dict[str, Any],
    defaults: dict[str, Any],
    project_root: Path,
) -> ConnectionConfig:
    def merged(key: str, base: Mapping[str, Any] | None = None) -> dict[str, Any]:
        layers = [("built-in", base or {}), ("defaults", defaults.get(key) or {})]

        return merge([*layers, (f"connections.{name}", body.get(key) or {})])[0]

    tables = merged("tables")
    run = merged("run")
    layers = [
        ("defaults.profile", defaults.get("profile") or {}),
        (f"connections.{name}.profile", body.get("profile") or {}),
    ]
    profile, origins = merge([(BUILTIN_LABEL, _BUILTIN_PROFILE), *layers])
    profile["read"] = {}

    for kind in BUILTIN_READ:
        block, read_origins = dict(BUILTIN_READ[kind]), {"mode": BUILTIN_LABEL}

        for label, layer in layers:
            if written := (layer.get("read") or {}).get(kind):
                block = fold_read(block, written, label, read_origins)

        profile["read"][kind] = block
        origins = {path: o for path, o in origins.items() if path[:2] != ("read", kind)}
        origins.update({("read", kind, key): o for key, o in read_origins.items()})

    timeout = run.get("statement_timeout")
    rules = tuple(
        _build_rule(rule, f"{where}.rules[{index}]")
        for where, block in (("defaults", defaults), (f"connections.{name}", body))
        for index, rule in enumerate(block.get("rules") or [])
    )
    output = body.get("output") or defaults.get("output") or OUTPUT_DEFAULT
    manifest = body.get("dbt_manifest")

    return ConnectionConfig(
        name=name,
        adapter=body["adapter"],
        auto=bool(body.get("auto")),
        output=resolve_project_path(output, project_root),
        tables=TablesConfig(
            include=tuple(p.lower() for p in tables.get("include", ["*"])),
            exclude=tuple(p.lower() for p in tables.get("exclude", [])),
        ),
        run=RunConfig(
            parallelism=run.get("parallelism", PARALLELISM_DEFAULT),
            statement_timeout=parse_duration_seconds(timeout) if timeout is not None else None,
        ),
        profile=_typed_profile(profile),
        rules=rules,
        diff=DiffConfig(
            stat_change_threshold={
                **STAT_CHANGE_THRESHOLD_DEFAULT,
                **merged("diff").get("stat_change_threshold", {}),
            },
        ),
        assertions_raw=dict(body.get("assertions") or {}),
        dbt_manifest=resolve_project_path(manifest, project_root) if manifest else None,
        profile_origins=origins,
    )


def _build_rule(raw: dict[str, Any], label: str) -> RuleConfig:
    matcher = raw.get("match") or {}

    return RuleConfig(
        match=RuleMatch(
            tables=tuple(p.lower() for p in matcher.get("tables", ["*"])),
            exclude=tuple(p.lower() for p in matcher.get("exclude", [])),
            min_rows=matcher.get("min_rows"),
        ),
        profile=_integral(copy.deepcopy(raw["profile"])),
        label=label,
    )


def _typed_profile(tree: Mapping[str, Any]) -> ProfileConfig:
    tree = _integral(tree)
    read, statistics = tree["read"], tree["statistics"]

    return ProfileConfig(
        freshness=FreshnessConfig(max_age_days=tree["freshness"]["max_age_days"]),
        statistics=StatisticsProfile(
            enumeration_threshold=statistics["enumeration_threshold"],
            top_n_values=statistics["top_n_values"],
            percentiles=tuple(statistics["percentiles"]),
        ),
        read=ReadSettings(tables=_typed_read(read["tables"]), views=_typed_read(read["views"])),
        looks_like=LooksLikeProfile(**tree["looks_like"]),
        null_patterns=NullPatternsProfile(**tree["null_patterns"]),
        parts=PartsProfile(**tree["parts"]),
        timeline=TimelineProfile(**tree["timeline"]),
        sketches=SketchesProfile(**tree["sketches"]),
        relationships=RelationshipsProfile(**tree["relationships"]),
        redact=tuple(
            rule
            if isinstance(rule, RedactRule)
            else RedactRule(
                columns=tuple(p.lower() for p in rule.get("columns", [])),
                sensitivity=tuple(rule.get("sensitivity", [])),
                looks_like=tuple(rule.get("looks_like", [])),
                with_=rule.get("with", "mask"),
            )
            for rule in tree["redact"]
        ),
    )


def _typed_read(block: Mapping[str, Any]) -> ReadConfig:
    return ReadConfig(
        mode=block["mode"],
        fraction=block.get("fraction"),
        max_rows=block.get("max_rows"),
        materialize=block.get("materialize", MATERIALIZE_SAMPLE_DEFAULT),
        filter=block.get("filter"),
    )


def _read_tree(read: ReadConfig, *, materialize_written: bool) -> dict[str, Any]:
    tree: dict[str, Any] = {"mode": read.mode}

    for key in ("fraction", "max_rows", "filter"):
        if (value := getattr(read, key)) is not None:
            tree[key] = value

    # Unwritten, the default is supplied once the fold knows the final mode; a config built in code
    # records no origins, so a non-default value is taken as written.
    if read.mode == "sample" and (
        materialize_written or read.materialize != MATERIALIZE_SAMPLE_DEFAULT
    ):
        tree["materialize"] = read.materialize

    return tree


def _leaf_paths(tree: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []

    for key, value in tree.items():
        if isinstance(value, Mapping):
            out += _leaf_paths(value, (*prefix, key))
        else:
            out.append((*prefix, key))

    return out


def _profile_tree(profile: ProfileConfig) -> dict[str, Any]:
    """`profile` as a mergeable tree; `redact` keeps its typed rules, since a list replaces whole."""

    tree = {
        key: asdict(getattr(profile, key))
        for key in (
            "freshness",
            "statistics",
            "looks_like",
            "null_patterns",
            "parts",
            "timeline",
            "sketches",
            "relationships",
        )
    }
    tree["statistics"]["percentiles"] = list(profile.statistics.percentiles)

    return {**tree, "redact": list(profile.redact)}


def _integral(node: Any) -> Any:
    """`7.0` passes the schema as an integer (JSON Schema 2020-12), so it is read as `7`."""

    if isinstance(node, dict):
        return {key: _integral(value) for key, value in node.items()}

    if isinstance(node, list):
        return [_integral(value) for value in node]

    if isinstance(node, float) and node.is_integer():
        return int(node)

    return node


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


def resolve_project_path(value: str | os.PathLike[str], project_root: Path) -> Path:
    """Expand `~` and anchor a relative path at the project root, never the working directory."""

    p = Path(value).expanduser()

    if not p.is_absolute():
        p = (project_root / p).resolve()

    return p
