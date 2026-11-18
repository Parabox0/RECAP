"""Shared test helpers: build configs and write facts straight into a store."""

from __future__ import annotations

from typing import Any

from recap.config import RecapConfig
from recap.model import DETERMINISTIC, Evidence, FactVersion, State, content_hash, make_fact_id, release_subject
from recap.store import Store
from recap.timeutil import parse_time

T0 = parse_time("2026-11-18T10:00:00Z")
DAY = 86_400
HOUR = 3_600


def source(sid: str, **over: Any) -> dict[str, Any]:
    base = {
        "id": sid,
        "adapter": "atom",
        "url": f"https://{sid}.example.org/feed",
        "permitted_origins": [f"https://{sid}.example.org"],
        "permitted_paths": ["/"],
        "fact_types": ["release", "release_highlights", "deprecation", "removal", "end_of_support", "status", "observation", "notice"],
        "jurisdictions": ["global", "US", "EU"],
        "attests_current": True,
        "topics": ["software_releases"],
        "entities": [],
        "options": {"entity": "example:lumen", "entity_label": "Lumen", "title_pattern": r"^Lumen (?P<version>\S+)(?: (?P<channel>beta))?$"},
    }
    base.update(over)
    return base


def profile(pid: str = "software", **over: Any) -> dict[str, Any]:
    base = {
        "id": pid,
        "version": 1,
        "topics": {"software_releases": 1.0},
        "entities": ["example:lumen"],
        "scope": ["global"],
        "min_score": 0.55,
    }
    base.update(over)
    return base


MODEL = {"id": "m", "knowledge_through": "2026-06-30", "counting": "byte_bound"}


def config(tmp_path, sources=None, profiles=None, models=None, settings=None) -> RecapConfig:
    return RecapConfig.build(
        str(tmp_path / "recap.db"),
        sources=sources if sources is not None else [source("src")],
        profiles=profiles if profiles is not None else [profile()],
        models=models if models is not None else [MODEL],
        # A generous deadline keeps unit tests independent of machine load;
        # the timeout path has its own test.
        settings=settings if settings is not None else {"deadline_ms": 5000},
    )


def put_fact(
    store: Store,
    *,
    now: int,
    source_id: str = "src",
    entry_id: str | None = None,
    entity: str = "example:lumen",
    label: str = "Lumen",
    predicate: str = "release",
    subject: str | None = None,
    value: dict | None = None,
    qualifiers: dict | None = None,
    published: int | None = None,
    effective: int | None = None,
    state: State = State.ACTIVE,
    scope: str = "global",
    topics: tuple[str, ...] = ("software_releases",),
    authority: str = "primary",
    explicit_expires: int | None = None,
    valid_to: int | None = None,
    supersedes: tuple[str, ...] = (),
    attest: bool = True,
    fact_id: str | None = None,
    version: str = "1.0.0",
    channel: str = "stable",
) -> FactVersion:
    published = now - HOUR if published is None else published
    effective = published if effective is None else effective
    if predicate == "release":
        value = value or {"version": version}
        qualifiers = {"channel": channel, **(qualifiers or {})}
        subject = subject if subject is not None else release_subject(value["version"], channel)
    subject = subject or ""
    entry_id = entry_id or f"{predicate}:{subject}:{published}"
    row = {
        "fact_id": fact_id or make_fact_id(source_id, entry_id, predicate, scope, subject),
        "source_id": source_id,
        "entry_id": entry_id,
        "entity": entity,
        "entity_label": label,
        "predicate": predicate,
        "scope": scope,
        "subject": subject,
        "value": value or {},
        "qualifiers": qualifiers or {},
        "event_at": effective,
        "effective_at": effective,
        "valid_to": valid_to,
        "published_at": published,
        "explicit_expires_at": explicit_expires,
        "state": state,
        "supersedes": supersedes,
        "source_revision": published,
        "origin": DETERMINISTIC,
        "evidence": Evidence(f"https://{source_id}.example.org/e/{entry_id}", "ab" * 32, None, "1"),
        "topics": topics,
        "authority": authority,
        "observed_at": now,
    }
    row["content_hash"] = content_hash(row)
    with store.batch(now) as b:
        v = b.add_fact_version(row)
        if attest:
            seq = b.add_check(source_id, 200, "ab" * 32, True, "ok")
            b.extend_attestations(source_id, [v.fact_id], None, seq)
    return v


def check_ok(store: Store, source_id: str, now: int) -> int:
    with store.batch(now) as b:
        return b.add_check(source_id, 200, "cd" * 32, False, "ok")
