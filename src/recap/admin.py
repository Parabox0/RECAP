"""Operator actions: review decisions, recalls, editorial picks, source sign-off."""

from __future__ import annotations

from typing import Any, Mapping

from . import sanitize
from .config import RecapConfig
from .model import (
    PREDICATES,
    REVIEWED,
    Evidence,
    FactVersion,
    State,
    content_hash,
    make_fact_id,
)
from .render import SENTENCE_MAX, RenderError, render_body
from .store import Store
from .timeutil import parse_time


class AdminError(ValueError):
    pass


def _version_row(v: FactVersion, **changes: Any) -> dict[str, Any]:
    row = {
        "fact_id": v.fact_id,
        "source_id": v.source_id,
        "entry_id": v.entry_id,
        "entity": v.entity,
        "entity_label": v.entity_label,
        "predicate": v.predicate,
        "scope": v.scope,
        "subject": v.subject,
        "value": v.value,
        "qualifiers": v.qualifiers,
        "event_at": v.event_at,
        "effective_at": v.effective_at,
        "valid_to": v.valid_to,
        "published_at": v.published_at,
        "explicit_expires_at": v.explicit_expires_at,
        "state": v.state,
        "supersedes": v.supersedes,
        "source_revision": v.source_revision,
        "origin": v.origin,
        "evidence": v.evidence,
        "topics": v.topics,
        "authority": v.authority,
    }
    row.update(changes)
    row["content_hash"] = content_hash(row)
    return row


def recall(store: Store, *, now: int, reason: str, fact_ids: list[str] = (), source_id: str | None = None) -> dict[str, Any]:
    """Tombstone facts (or every fact of a source) and revoke packets that mention them.

    Recalling a source also pauses it, so nothing new is promoted until an
    operator signs it off again.
    """
    if not reason:
        raise AdminError("a recall needs a reason")
    targets: dict[str, FactVersion] = {}
    for fid in fact_ids:
        v = store.latest_version(fid)
        if v is None:
            raise AdminError(f"unknown fact {fid}")
        targets[fid] = v
    if source_id is not None:
        for v in store.latest_versions_for_source(source_id):
            targets[v.fact_id] = v
    packets = store.packets_with_facts(sorted(targets))
    tombstoned = 0
    with store.batch(now) as b:
        for fid in sorted(targets):
            v = targets[fid]
            if v.state is State.RETRACTED:
                continue
            b.add_fact_version(_version_row(v, state=State.RETRACTED, observed_at=now))
            tombstoned += 1
        for pid in packets:
            b.add_revocation(pid, f"recall: {reason}")
        if source_id is not None:
            b.add_source_event(source_id, "paused", f"recall: {reason}")
    return {"tombstoned": tombstoned, "packets_revoked": len(packets), "packet_ids": packets}


def pause_source(store: Store, source_id: str, *, now: int, reason: str) -> None:
    with store.batch(now) as b:
        b.add_source_event(source_id, "paused", reason)


def resume_source(store: Store, source_id: str, *, now: int, reason: str = "manual sign-off") -> None:
    with store.batch(now) as b:
        b.add_source_event(source_id, "ok", reason)


def editorial(store: Store, config: RecapConfig, profile_id: str, fact_id: str, *, action: str, now: int) -> None:
    profile = config.profile(profile_id)
    if profile is None or profile.kind != "editorial":
        raise AdminError(f"{profile_id!r} is not an editorial profile")
    if action == "add" and store.latest_version(fact_id) is None:
        raise AdminError(f"unknown fact {fact_id}")
    with store.batch(now) as b:
        b.add_editorial(profile_id, fact_id, action)


def reject_review(store: Store, review_id: int, *, decided_by: str, now: int, note: str | None = None) -> None:
    item = store.get_review(review_id)
    if item is None or item["status"] != "pending":
        raise AdminError(f"review {review_id} is not pending")
    with store.batch(now) as b:
        b.decide_review(review_id, "rejected", decided_by, None, note)


def approve_review(
    store: Store,
    config: RecapConfig,
    review_id: int,
    *,
    decided_by: str,
    now: int,
    text: str | None = None,
    fact: Mapping[str, Any] | None = None,
) -> FactVersion:
    """Approve a staged item, writing a ``reviewed`` fact.

    For highlights attached to a typed fact, pass the final ``text`` (the
    reviewer's wording, at most 160 characters). For an item from a
    review-class source, pass a typed ``fact`` dictionary.
    """
    item = store.get_review(review_id)
    if item is None or item["status"] != "pending":
        raise AdminError(f"review {review_id} is not pending")
    source = config.sources.get(item["source_id"])
    if source is None:
        raise AdminError(f"source {item['source_id']} is no longer enrolled")
    if item["target_fact_id"]:
        if text is None:
            raise AdminError("approving highlights needs the final text")
        row = _highlights_row(store, item, source.id, text)
    else:
        if fact is None:
            raise AdminError("approving an untargeted item needs a typed fact")
        row = _reviewed_fact_row(item, source, fact)
    try:
        render_body(_preview(row, now), "active" if row["state"] is State.ACTIVE else "announcement")
    except RenderError as exc:
        raise AdminError(f"fact would not render: {exc}") from None
    with store.batch(now) as b:
        version = b.add_fact_version({**row, "observed_at": now})
        b.decide_review(review_id, "approved", decided_by, row["value"].get("text"), None)
    return version


def _highlights_row(store: Store, item: Mapping[str, Any], source_id: str, text: str) -> dict[str, Any]:
    target = store.latest_version(item["target_fact_id"])
    if target is None or target.predicate != "release":
        raise AdminError("highlights must attach to a stored release fact")
    try:
        final = sanitize.inline(text, SENTENCE_MAX, "highlights")
    except ValueError as exc:
        raise AdminError(str(exc)) from None
    row = {
        "fact_id": make_fact_id(source_id, target.entry_id, "release_highlights", target.scope, target.subject),
        "source_id": source_id,
        "entry_id": target.entry_id,
        "entity": target.entity,
        "entity_label": target.entity_label,
        "predicate": "release_highlights",
        "scope": target.scope,
        "subject": target.subject,
        "value": {"text": final},
        "qualifiers": {
            "version": target.value.get("version", target.subject),
            "channel": target.channel or "stable",
            "target_fact_id": target.fact_id,
        },
        "event_at": target.event_at,
        "effective_at": target.effective_at,
        "valid_to": None,
        "published_at": target.published_at,
        "explicit_expires_at": None,
        "state": State.ACTIVE,
        "supersedes": (),
        "source_revision": None,
        "origin": REVIEWED,
        "evidence": target.evidence,
        "topics": target.topics,
        "authority": target.authority,
    }
    row["content_hash"] = content_hash(row)
    return row


def _reviewed_fact_row(item: Mapping[str, Any], source: Any, fact: Mapping[str, Any]) -> dict[str, Any]:
    predicate = fact.get("predicate")
    if predicate not in source.fact_types or predicate not in PREDICATES:
        raise AdminError(f"predicate {predicate!r} is not allowed for source {source.id}")
    entity = fact.get("entity")
    if source.entities and entity not in source.entities:
        raise AdminError(f"entity {entity!r} is not covered by source {source.id}")
    scope = fact.get("scope", "global")
    if scope not in source.jurisdictions:
        raise AdminError(f"scope {scope!r} is not allowed for source {source.id}")
    state = State(fact.get("state", "active"))
    if state not in (State.ACTIVE, State.SCHEDULED):
        raise AdminError("reviewed facts must be active or scheduled")
    subject = str(fact.get("subject", ""))
    entry_id = f"review:{item['id']}"
    row = {
        "fact_id": make_fact_id(source.id, entry_id, predicate, scope, subject),
        "source_id": source.id,
        "entry_id": entry_id,
        "entity": entity,
        "entity_label": str(fact.get("entity_label", entity)),
        "predicate": predicate,
        "scope": scope,
        "subject": subject,
        "value": dict(fact.get("value") or {}),
        "qualifiers": dict(fact.get("qualifiers") or {}),
        "event_at": parse_time(fact.get("event_at")),
        "effective_at": parse_time(fact.get("effective_at")),
        "valid_to": parse_time(fact.get("valid_to")),
        "published_at": parse_time(fact.get("published_at")),
        "explicit_expires_at": parse_time(fact.get("expires_at")),
        "state": state,
        "supersedes": tuple(fact.get("supersedes") or ()),
        "source_revision": None,
        "origin": REVIEWED,
        "evidence": Evidence(source.url, "", None, source.parser_version),
        "topics": source.topics,
        "authority": source.authority,
    }
    if row["effective_at"] is None:
        raise AdminError("reviewed facts need effective_at")
    row["content_hash"] = content_hash(row)
    return row


def _preview(row: Mapping[str, Any], now: int) -> FactVersion:
    return FactVersion(
        seq=0,
        fact_id=row["fact_id"],
        source_id=row["source_id"],
        entry_id=row["entry_id"],
        entity=row["entity"],
        entity_label=row["entity_label"],
        predicate=row["predicate"],
        scope=row["scope"],
        subject=row["subject"],
        value=row["value"],
        qualifiers=row["qualifiers"],
        event_at=row["event_at"],
        effective_at=row["effective_at"],
        valid_to=row["valid_to"],
        published_at=row["published_at"],
        observed_at=now,
        explicit_expires_at=row["explicit_expires_at"],
        state=row["state"],
        supersedes=tuple(row["supersedes"]),
        source_revision=row["source_revision"],
        content_hash=row["content_hash"],
        origin=row["origin"],
        evidence=row["evidence"] if isinstance(row["evidence"], Evidence) else Evidence.from_dict(row["evidence"]),
        topics=tuple(row["topics"]),
        authority=row["authority"],
    )
