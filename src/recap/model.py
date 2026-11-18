"""Core data model: fact versions, states, predicates and identifiers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class State(str, Enum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    RETRACTED = "retracted"
    DISPUTED = "disputed"


DETERMINISTIC = "deterministic"
REVIEWED = "reviewed"

PRIMARY = "primary"
CORROBORATED = "corroborated"
AUTHORITY_SCORE: dict[str, float] = {PRIMARY: 1.0, CORROBORATED: 0.75}

EVENT = "event"
STATE_KIND = "state"


@dataclass(frozen=True)
class PredicateSpec:
    """How a predicate is scored, rechecked and deduplicated.

    ``kind`` is ``"event"`` for things that happened once (a release) and
    ``"state"`` for things whose current value changes (a status).
    ``dominance`` means a newer fact in the same (entity, scope, channel)
    group makes older ones redundant. ``attached_to`` names the predicate a
    row annotates (reviewed highlights attach to a release).
    """

    name: str
    kind: str
    priority: float
    recheck_hours: int
    dominance: bool = False
    attached_to: str | None = None


PREDICATES: dict[str, PredicateSpec] = {}


def register_predicate(spec: PredicateSpec) -> None:
    """Add or replace a predicate. Extensions call this at import time."""
    if spec.kind not in (EVENT, STATE_KIND):
        raise ValueError(f"predicate kind must be 'event' or 'state', got {spec.kind!r}")
    if not 0.0 <= spec.priority <= 1.0:
        raise ValueError("predicate priority must be in [0, 1]")
    if spec.recheck_hours <= 0:
        raise ValueError("recheck_hours must be positive")
    PREDICATES[spec.name] = spec


for _spec in (
    PredicateSpec("release", EVENT, 0.7, 168, dominance=True),
    PredicateSpec("release_highlights", EVENT, 0.3, 168, attached_to="release"),
    PredicateSpec("deprecation", EVENT, 1.0, 168),
    PredicateSpec("removal", EVENT, 1.0, 168),
    PredicateSpec("end_of_support", EVENT, 1.0, 168),
    PredicateSpec("status", STATE_KIND, 0.7, 48),
    PredicateSpec("observation", STATE_KIND, 0.3, 48),
    PredicateSpec("notice", EVENT, 0.3, 48),
):
    register_predicate(_spec)


def predicate_spec(name: str) -> PredicateSpec:
    try:
        return PREDICATES[name]
    except KeyError:
        raise KeyError(f"unknown predicate {name!r}") from None


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


Key = tuple[str, str, str, str]


def key_to_str(key: Key) -> str:
    return "|".join(key)


def key_from_str(text: str) -> Key:
    parts = text.split("|")
    if len(parts) == 3:
        parts.append("")
    if len(parts) != 4:
        raise ValueError("key must be 'entity|predicate|scope|subject'")
    return (parts[0], parts[1], parts[2], parts[3])


_VERSION_PREFIX = re.compile(r"^[vV]?(\d+(?:\.\d+){0,5})")
VERSION_PARTS = 6


def version_tuple(version: str) -> tuple[int, ...] | None:
    """Numeric release tuple, padded to six parts, or ``None`` if unparseable.

    Pre-release suffixes are ignored: ``2.2.0rc1`` and ``2.2.0`` share ``(2, 2, 0, 0, 0, 0)``.
    """
    m = _VERSION_PREFIX.match(version or "")
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    return tuple(parts + [0] * (VERSION_PARTS - len(parts)))


def release_subject(version: str, channel: str) -> str:
    """Key subject for a release: the same version on two channels is two events."""
    return f"{version}@{channel}"


def make_fact_id(source_id: str, entry_id: str, predicate: str, scope: str, subject: str) -> str:
    raw = "\x1f".join((source_id, entry_id, predicate, scope, subject)).encode("utf-8")
    return "fx_" + hashlib.sha256(raw).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class Evidence:
    canonical_url: str
    blob_sha256: str
    byte_range: tuple[int, int] | None
    parser_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_url": self.canonical_url,
            "blob_sha256": self.blob_sha256,
            "byte_range": list(self.byte_range) if self.byte_range else None,
            "parser_version": self.parser_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        rng = data.get("byte_range")
        return cls(
            canonical_url=str(data.get("canonical_url", "")),
            blob_sha256=str(data.get("blob_sha256", "")),
            byte_range=(int(rng[0]), int(rng[1])) if rng else None,
            parser_version=str(data.get("parser_version", "")),
        )


SEMANTIC_FIELDS = (
    "entity",
    "entity_label",
    "predicate",
    "scope",
    "subject",
    "value",
    "qualifiers",
    "event_at",
    "effective_at",
    "valid_to",
    "published_at",
    "explicit_expires_at",
    "state",
    "supersedes",
)


def content_hash(fields: Mapping[str, Any]) -> str:
    """Hash of the fields that change what a fact claims."""
    payload = {}
    for name in SEMANTIC_FIELDS:
        value = fields.get(name)
        if isinstance(value, State):
            value = value.value
        if isinstance(value, tuple):
            value = list(value)
        payload[name] = value
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, eq=False)
class FactVersion:
    """One stored version of a fact. Versions are append-only."""

    seq: int
    fact_id: str
    source_id: str
    entry_id: str
    entity: str
    entity_label: str
    predicate: str
    scope: str
    subject: str
    value: dict
    qualifiers: dict
    event_at: int | None
    effective_at: int | None
    valid_to: int | None
    published_at: int | None
    observed_at: int
    explicit_expires_at: int | None
    state: State
    supersedes: tuple[str, ...]
    source_revision: int | None
    content_hash: str
    origin: str
    evidence: Evidence
    topics: tuple[str, ...]
    authority: str

    @property
    def key(self) -> Key:
        return (self.entity, self.predicate, self.scope, self.subject)

    @property
    def change_time(self) -> int:
        for value in (self.effective_at, self.event_at, self.published_at):
            if value is not None:
                return value
        return self.observed_at

    @property
    def publish_time(self) -> int:
        return self.published_at if self.published_at is not None else self.observed_at

    @property
    def channel(self) -> str:
        return str(self.qualifiers.get("channel", ""))

    def semantic_fields(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in SEMANTIC_FIELDS}


@dataclass(frozen=True)
class ParsedFact:
    """A typed proposition produced by an adapter, before it is stored."""

    entry_id: str
    entity: str
    entity_label: str
    predicate: str
    scope: str = "global"
    subject: str = ""
    value: dict = field(default_factory=dict)
    qualifiers: dict = field(default_factory=dict)
    event_at: int | None = None
    effective_at: int | None = None
    valid_to: int | None = None
    published_at: int | None = None
    explicit_expires_at: int | None = None
    state: State = State.ACTIVE
    supersedes: tuple[str, ...] = ()
    source_revision: int | None = None
    evidence_url: str = ""
    byte_range: tuple[int, int] | None = None

    def fact_id(self, source_id: str) -> str:
        return make_fact_id(source_id, self.entry_id, self.predicate, self.scope, self.subject)

    def semantic_fields(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in SEMANTIC_FIELDS}
