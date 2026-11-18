"""Configuration: source registry, profile manifests, model registry, settings.

Configuration is TOML on disk (see ``examples/quickstart``) but every object
can also be built directly from dictionaries for library use and tests.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .model import CORROBORATED, PREDICATES, PRIMARY
from .timeutil import parse_time


class ConfigError(ValueError):
    """Raised for invalid configuration."""


HARD_CAP_TOKENS = 256
DEFAULT_BUDGET = 192
MAX_FACTS = 8

_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_TOPIC = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
_ENTITY = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}:[a-z0-9][a-z0-9_.-]{0,127}$")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_ADAPTER_CLASSES = ("deterministic", "review")
_COUNTING = ("byte_bound", "calibrated", "exact")


def _err(where: str, message: str) -> ConfigError:
    return ConfigError(f"{where}: {message}")


def normalize_origin(url: str) -> str:
    """Return ``scheme://host[:port]`` with default ports removed."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        raise ValueError(f"not an http(s) URL: {url!r}")
    port = parts.port
    default = 443 if scheme == "https" else 80
    if port is None or port == default:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _str_list(value: Any, where: str, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
        raise _err(where, f"{name} must be a list of strings")
    return tuple(value)


@dataclass(frozen=True)
class Settings:
    deadline_ms: float = 40.0
    safety_margin_tokens: int = 64
    max_facts: int = MAX_FACTS
    per_entity_cap: int = 2
    selection_pool: int = 64
    top_topics: int = 6
    topic_take: int = 30
    entity_take: int = 20
    pool_cap: int = 200
    max_lookback_days: int = 180
    knowledge_margin_days: int = 30
    default_lookback_days: int = 30
    half_life_hours: float = 168.0
    index_refresh_seconds: float = 1.0
    cache_size: int = 4096
    outage_failures: int = 3

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Settings":
        data = dict(data or {})
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"settings: unknown keys {sorted(unknown)}")
        settings = cls(**data)
        if not 1 <= settings.max_facts <= MAX_FACTS:
            raise ConfigError(f"settings: max_facts must be between 1 and {MAX_FACTS}")
        if settings.per_entity_cap < 1:
            raise ConfigError("settings: per_entity_cap must be at least 1")
        if settings.selection_pool < 1 or settings.pool_cap < 1:
            raise ConfigError("settings: pool sizes must be positive")
        return settings


@dataclass(frozen=True)
class SourceConfig:
    id: str
    adapter: str
    adapter_class: str
    url: str
    permitted_origins: tuple[str, ...]
    permitted_paths: tuple[str, ...]
    fact_types: tuple[str, ...]
    jurisdictions: tuple[str, ...] = ("global",)
    poll_interval_minutes: int = 30
    max_evidence_age_hours: int = 168
    parser_version: str = "1"
    authority: str = PRIMARY
    attests_current: bool = False
    topics: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    coverage_start: int | None = None
    max_bytes: int = 2_000_000
    max_backfill_pages: int = 20
    options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceConfig":
        data = dict(data)
        sid = data.get("id")
        where = f"source {sid!r}"
        if not isinstance(sid, str) or not _ID.match(sid):
            raise _err(where, "id must match [a-z0-9][a-z0-9_.-]*")
        adapter = data.get("adapter")
        if not isinstance(adapter, str) or not adapter:
            raise _err(where, "adapter is required")
        adapter_class = data.get("class", data.get("adapter_class", "deterministic"))
        if adapter_class not in _ADAPTER_CLASSES:
            raise _err(where, f"class must be one of {_ADAPTER_CLASSES}")
        url = data.get("url")
        if not isinstance(url, str):
            raise _err(where, "url is required")
        try:
            origin = normalize_origin(url)
            origins = tuple(normalize_origin(o) for o in _str_list(data.get("permitted_origins"), where, "permitted_origins"))
        except ValueError as exc:
            raise _err(where, str(exc)) from None
        if not origins:
            raise _err(where, "permitted_origins is required")
        if origin not in origins:
            raise _err(where, f"url origin {origin} is not in permitted_origins")
        if origin.startswith("http://") and not data.get("allow_insecure_http", False):
            raise _err(where, "plain http sources need allow_insecure_http = true")
        paths = _str_list(data.get("permitted_paths"), where, "permitted_paths")
        if not paths or not all(p.startswith("/") for p in paths):
            raise _err(where, "permitted_paths must be a non-empty list of absolute path prefixes")
        if not any((urlsplit(url).path or "/").startswith(p) for p in paths):
            raise _err(where, "url path is not under permitted_paths")
        fact_types = _str_list(data.get("fact_types"), where, "fact_types")
        if not fact_types:
            raise _err(where, "fact_types is required")
        for ft in fact_types:
            if ft not in PREDICATES:
                raise _err(where, f"unknown fact type {ft!r}")
        jurisdictions = _str_list(data.get("jurisdictions", ["global"]), where, "jurisdictions")
        for j in jurisdictions:
            if not _SCOPE.match(j):
                raise _err(where, f"bad jurisdiction {j!r}")
        authority = data.get("authority", PRIMARY)
        if authority not in (PRIMARY, CORROBORATED):
            raise _err(where, "authority must be 'primary' or 'corroborated'")
        topics = _str_list(data.get("topics"), where, "topics")
        for t in topics:
            if not _TOPIC.match(t):
                raise _err(where, f"bad topic id {t!r}")
        options = dict(data.get("options") or {})
        entities = list(_str_list(data.get("entities"), where, "entities"))
        if not entities:
            if isinstance(options.get("entity"), str):
                entities.append(options["entity"])
            for mapped in (options.get("entity_map") or {}).values():
                if isinstance(mapped, Mapping) and isinstance(mapped.get("entity"), str):
                    entities.append(mapped["entity"])
        for e in entities:
            if not _ENTITY.match(e):
                raise _err(where, f"bad entity id {e!r} (expected namespace:name)")
        poll = int(data.get("poll_interval_minutes", 30))
        if poll < 5:
            raise _err(where, "poll_interval_minutes must be at least 5")
        max_age = int(data.get("max_evidence_age_hours", 168))
        if max_age < 1:
            raise _err(where, "max_evidence_age_hours must be positive")
        coverage = data.get("coverage_start")
        return cls(
            id=sid,
            adapter=adapter,
            adapter_class=adapter_class,
            url=url,
            permitted_origins=origins,
            permitted_paths=paths,
            fact_types=fact_types,
            jurisdictions=jurisdictions,
            poll_interval_minutes=poll,
            max_evidence_age_hours=max_age,
            parser_version=str(data.get("parser_version", "1")),
            authority=authority,
            attests_current=bool(data.get("attests_current", False)),
            topics=topics,
            entities=tuple(dict.fromkeys(entities)),
            coverage_start=parse_time(coverage) if coverage is not None else None,
            max_bytes=int(data.get("max_bytes", 2_000_000)),
            max_backfill_pages=int(data.get("max_backfill_pages", 20)),
            options=options,
        )


@dataclass(frozen=True)
class ProfileManifest:
    id: str
    version: int
    kind: str = "ranked"
    prompt_versions: tuple[str, ...] = ()
    topics: Mapping[str, float] = field(default_factory=dict)
    entities: tuple[str, ...] = ()
    scope: tuple[str, ...] = ("global",)
    deny: tuple[str, ...] = ()
    min_score: float = 0.55
    budget: int = DEFAULT_BUDGET
    max_editorial: int = 2

    @property
    def ref(self) -> tuple[str, int]:
        return (self.id, self.version)

    def top_topics(self, n: int) -> list[str]:
        ordered = sorted(self.topics.items(), key=lambda kv: (-kv[1], kv[0]))
        return [t for t, _ in ordered[:n]]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProfileManifest":
        data = dict(data)
        pid = data.get("id")
        where = f"profile {pid!r}"
        if not isinstance(pid, str) or not _ID.match(pid):
            raise _err(where, "id must match [a-z0-9][a-z0-9_.-]*")
        version = data.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise _err(where, "version must be a positive integer")
        kind = data.get("kind", "ranked")
        if kind not in ("ranked", "editorial"):
            raise _err(where, "kind must be 'ranked' or 'editorial'")
        topics_raw = data.get("topics") or {}
        if not isinstance(topics_raw, Mapping):
            raise _err(where, "topics must be a table of weights")
        topics: dict[str, float] = {}
        for t, w in topics_raw.items():
            if not _TOPIC.match(t):
                raise _err(where, f"bad topic id {t!r}")
            if isinstance(w, bool) or not isinstance(w, (int, float)) or not 0.0 <= float(w) <= 1.0:
                raise _err(where, f"topic weight for {t!r} must be in [0, 1]")
            topics[t] = float(w)
        entities = _str_list(data.get("entities"), where, "entities")
        for e in entities:
            if not _ENTITY.match(e):
                raise _err(where, f"bad entity id {e!r}")
        scope = _str_list(data.get("scope", ["global"]), where, "scope")
        deny = _str_list(data.get("deny"), where, "deny")
        min_score = float(data.get("min_score", 0.55))
        if not 0.0 <= min_score <= 1.0:
            raise _err(where, "min_score must be in [0, 1]")
        budget = int(data.get("budget", DEFAULT_BUDGET))
        if not 0 <= budget <= HARD_CAP_TOKENS:
            raise _err(where, f"budget must be between 0 and {HARD_CAP_TOKENS}")
        max_editorial = int(data.get("max_editorial", 2))
        if not 0 <= max_editorial <= 2:
            raise _err(where, "max_editorial must be 0, 1 or 2")
        return cls(
            id=pid,
            version=version,
            kind=kind,
            prompt_versions=_str_list(data.get("prompt_versions"), where, "prompt_versions"),
            topics=topics,
            entities=entities,
            scope=scope,
            deny=deny,
            min_score=min_score,
            budget=budget,
            max_editorial=max_editorial,
        )


@dataclass(frozen=True)
class Calibration:
    alpha: float
    beta: float
    gamma: int
    margin: float = 0.05
    version: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str) -> "Calibration":
        try:
            cal = cls(
                alpha=float(data["alpha"]),
                beta=float(data["beta"]),
                gamma=int(data["gamma"]),
                margin=float(data.get("margin", 0.05)),
                version=str(data.get("version", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _err(where, f"calibration needs alpha, beta and gamma ({exc})") from None
        if cal.alpha <= 0 or cal.beta < 0 or cal.gamma < 0 or cal.margin < 0:
            raise _err(where, "calibration values must be non-negative (alpha positive)")
        return cal


@dataclass(frozen=True)
class ModelEntry:
    id: str
    knowledge_through: int | None
    counting: str
    serializer_version: str = "v1"
    calibration: Calibration | None = None
    framing_overhead: int = 8

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelEntry":
        data = dict(data)
        mid = data.get("id")
        where = f"model {mid!r}"
        if not isinstance(mid, str) or not mid or len(mid) > 128:
            raise _err(where, "id is required")
        counting = data.get("counting", "byte_bound")
        if counting not in _COUNTING:
            raise _err(where, f"counting must be one of {_COUNTING}")
        cal = None
        if counting == "calibrated":
            if not isinstance(data.get("calibration"), Mapping):
                raise _err(where, "calibrated counting needs a [model.calibration] table")
            cal = Calibration.from_dict(data["calibration"], where)
        kt = data.get("knowledge_through")
        return cls(
            id=mid,
            knowledge_through=parse_time(kt) if kt is not None else None,
            counting=counting,
            serializer_version=str(data.get("serializer_version", "v1")),
            calibration=cal,
            framing_overhead=int(data.get("framing_overhead", 8)),
        )


@dataclass
class RecapConfig:
    db_path: str
    sources: dict[str, SourceConfig] = field(default_factory=dict)
    profiles: dict[tuple[str, int], ProfileManifest] = field(default_factory=dict)
    models: dict[str, ModelEntry] = field(default_factory=dict)
    settings: Settings = field(default_factory=Settings)
    base_dir: str = "."

    def __post_init__(self) -> None:
        self._bindings: dict[str, tuple[str, int]] = {}
        for ref, profile in sorted(self.profiles.items()):
            if ref != profile.ref:
                raise ConfigError(f"profile key {ref} does not match manifest {profile.ref}")
            for pv in profile.prompt_versions:
                bound = self._bindings.get(pv)
                if bound and bound[0] != profile.id:
                    raise ConfigError(f"prompt version {pv!r} is bound to both {bound[0]!r} and {profile.id!r}")
                if not bound or bound[1] < profile.version:
                    self._bindings[pv] = profile.ref

    @classmethod
    def build(
        cls,
        db_path: str,
        sources: list[Mapping[str, Any] | SourceConfig] = (),
        profiles: list[Mapping[str, Any] | ProfileManifest] = (),
        models: list[Mapping[str, Any] | ModelEntry] = (),
        settings: Mapping[str, Any] | Settings | None = None,
    ) -> "RecapConfig":
        src = [s if isinstance(s, SourceConfig) else SourceConfig.from_dict(s) for s in sources]
        prof = [p if isinstance(p, ProfileManifest) else ProfileManifest.from_dict(p) for p in profiles]
        mods = [m if isinstance(m, ModelEntry) else ModelEntry.from_dict(m) for m in models]
        _no_duplicates([s.id for s in src], "source")
        _no_duplicates([p.ref for p in prof], "profile")
        _no_duplicates([m.id for m in mods], "model")
        return cls(
            db_path=db_path,
            sources={s.id: s for s in src},
            profiles={p.ref: p for p in prof},
            models={m.id: m for m in mods},
            settings=settings if isinstance(settings, Settings) else Settings.from_dict(settings),
        )

    def profile(self, profile_id: str, version: int | None = None) -> ProfileManifest | None:
        if version is not None:
            return self.profiles.get((profile_id, version))
        versions = [p for (pid, _), p in self.profiles.items() if pid == profile_id]
        return max(versions, key=lambda p: p.version) if versions else None

    def profile_for_prompt(self, prompt_version: str, fallback: str = "general") -> ProfileManifest | None:
        """The profile bound to a prompt version; unbound prompts get the general profile."""
        ref = self._bindings.get(prompt_version)
        if ref:
            return self.profiles[ref]
        return self.profile(fallback)


def _no_duplicates(items: list[Any], what: str) -> None:
    seen = set()
    for item in items:
        if item in seen:
            raise ConfigError(f"duplicate {what} {item!r}")
        seen.add(item)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"missing file: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def load_config(path: str | Path) -> RecapConfig:
    """Load ``recap.toml`` and the files it points to.

    ``recap.toml`` keys: ``db``, ``sources``, ``models``, ``profiles_dir`` and
    an optional ``[settings]`` table. Relative paths resolve against the
    directory holding ``recap.toml``.
    """
    path = Path(path)
    base = path.parent.resolve()
    root = _read_toml(path)
    unknown = set(root) - {"db", "sources", "models", "profiles_dir", "settings"}
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    sources_doc = _read_toml(base / root.get("sources", "sources.toml"))
    models_doc = _read_toml(base / root.get("models", "models.toml"))
    profiles_dir = base / root.get("profiles_dir", "profiles")
    profiles = []
    if profiles_dir.is_dir():
        for file in sorted(profiles_dir.glob("*.toml")):
            profiles.append(_read_toml(file))
    db = root.get("db", "recap.db")
    db_path = db if db == ":memory:" else str((base / db).resolve())
    cfg = RecapConfig.build(
        db_path=db_path,
        sources=sources_doc.get("source", []),
        profiles=profiles,
        models=models_doc.get("model", []),
        settings=root.get("settings"),
    )
    cfg.base_dir = str(base)
    return cfg
