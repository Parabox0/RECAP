"""Turn adapter output into store writes (spec section 2).

One fetch produces one atomic batch: a check row, any new fact versions,
attestations, staged review items and source events.

Ordering rules per entry:

* With a source revision: a delivery whose revision is not newer than the
  stored one is a replay and is ignored, whatever its content. A newer
  revision is accepted even if it reverts to earlier content.
* Without a revision: identical content is a no-op; content that returns the
  entry to a version seen before goes to review (it may be a replay); the
  first change is accepted as a correction; any later change goes to review,
  because without revisions the newer of two contents cannot be told apart.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..config import SourceConfig
from ..model import DETERMINISTIC, Evidence, FactVersion, ParsedFact, State, content_hash
from ..store import SourceRuntime, Store
from .adapters import ParseResult

DRIFT_MIN_REJECTED = 3


@dataclass
class IngestStats:
    new_versions: int = 0
    unchanged: int = 0
    replays_ignored: int = 0
    reverts_to_review: int = 0
    rejected: int = 0
    withdrawn: int = 0
    reviews_staged: int = 0
    attested: int = 0
    paused: str | None = None
    resumed: bool = False
    notes: list[str] = field(default_factory=list)


def _drift_reason(source: SourceConfig, result: ParseResult) -> str | None:
    if result.entries_seen > 0 and result.matched == 0 and source.adapter_class == "deterministic":
        return "parser_drift: no entries matched"
    considered = result.matched
    if considered and result.rejected >= DRIFT_MIN_REJECTED and result.rejected * 2 > considered:
        return f"parser_drift: {result.rejected} of {considered} entries failed validation"
    return None


def _gap_reason(rt: SourceRuntime, result: ParseResult) -> str | None:
    if result.complete_listing:
        return None
    if (
        result.sequence_first is not None
        and rt.last_sequence is not None
        and result.sequence_first > rt.last_sequence + 1
    ):
        return f"gap: sequence jumped from {rt.last_sequence} to {result.sequence_first}"
    if rt.newest_seen is not None and result.oldest_entry_time is not None and result.oldest_entry_time > rt.newest_seen:
        return "gap: oldest fetched entry is newer than the last entry seen"
    return None


def _validate(source: SourceConfig, pf: ParsedFact) -> bool:
    if pf.predicate not in source.fact_types:
        return False
    if pf.scope not in source.jurisdictions:
        return False
    if source.entities and pf.entity not in source.entities:
        return False
    return True


def ingest(
    store: Store,
    source: SourceConfig,
    result: ParseResult,
    *,
    now: int,
    body_sha: str,
    http_status: int = 200,
    check_gap: bool = True,
    resume_reasons: tuple[str, ...] = ("outage",),
) -> IngestStats:
    """Write one successful fetch. Returns what changed."""
    stats = IngestStats()
    rt = store.get_runtime(source.id)
    latest_event = store.latest_source_event(source.id)
    paused_reason = latest_event[1] if latest_event and latest_event[0] == "paused" else None

    with store.batch(now) as b:
        check_seq = b.add_check(source.id, http_status, body_sha, source.attests_current, "ok")
        prev_ok = rt.last_ok_check_seq
        rt.last_ok_check_seq = check_seq
        rt.failures = 0

        drift = _drift_reason(source, result)
        gap = _gap_reason(rt, result) if check_gap else None
        if drift or gap:
            reason = drift or gap
            if paused_reason != reason:
                b.add_source_event(source.id, "paused", reason)
            stats.paused = reason
            b.set_runtime(rt)
            return stats

        if paused_reason is not None and any(paused_reason.startswith(r) for r in resume_reasons):
            b.add_source_event(source.id, "ok", "recovered")
            stats.resumed = True

        present: list[str] = []
        by_entry: dict[str, list[ParsedFact]] = {}
        for pf in result.facts:
            if not _validate(source, pf):
                stats.rejected += 1
                continue
            by_entry.setdefault(pf.entry_id, []).append(pf)

        for entry_id in sorted(by_entry):
            facts = sorted(by_entry[entry_id], key=lambda f: (f.predicate, f.scope, f.subject))
            hashes = [content_hash(f.semantic_fields()) for f in facts]
            entry_hash = hashlib.sha256("\n".join(hashes).encode()).hexdigest()
            revisions = [f.source_revision for f in facts if f.source_revision is not None]
            revision = max(revisions) if revisions else None
            stored = b.get_entry_state(source.id, entry_id)
            if stored is not None:
                stored_rev, stored_hash = stored
                if entry_hash == stored_hash:
                    stats.unchanged += len(facts)
                    present.extend(f.fact_id(source.id) for f in facts)
                    continue
                if revision is not None and stored_rev is not None and revision <= stored_rev:
                    stats.replays_ignored += 1
                    continue
                if revision is None:
                    kind = None
                    if b.entry_hash_seen(source.id, entry_id, entry_hash):
                        kind = "revert"
                    elif b.entry_hash_count(source.id, entry_id) >= 2:
                        kind = "change"
                    if kind is not None:
                        if b.add_review(source.id, entry_id, None, kind, _describe(facts, kind), entry_hash):
                            stats.reviews_staged += 1
                        stats.reverts_to_review += 1
                        continue
            # A corrected entry that no longer yields a fact it used to (a fixed
            # version number, say) withdraws that fact.
            new_ids = {pf.fact_id(source.id) for pf in facts}
            if stored is not None:
                for old in b.latest_versions_for_entry(source.id, entry_id):
                    if old.fact_id not in new_ids and old.state is not State.RETRACTED:
                        b.add_fact_version(_retracted(old, now))
                        stats.withdrawn += 1
            for pf, chash in zip(facts, hashes):
                fid = pf.fact_id(source.id)
                present.append(fid)
                latest = b.latest_version(fid)
                if latest is not None and latest.content_hash == chash:
                    stats.unchanged += 1
                    continue
                b.add_fact_version(
                    {
                        **pf.semantic_fields(),
                        "fact_id": fid,
                        "source_id": source.id,
                        "entry_id": pf.entry_id,
                        "observed_at": now,
                        "source_revision": pf.source_revision,
                        "content_hash": chash,
                        "origin": DETERMINISTIC,
                        "evidence": Evidence(pf.evidence_url or source.url, body_sha, pf.byte_range, source.parser_version),
                        "topics": source.topics,
                        "authority": source.authority,
                    }
                )
                stats.new_versions += 1
            b.set_entry_state(source.id, entry_id, revision, entry_hash)

        if source.attests_current and present:
            b.extend_attestations(source.id, present, prev_ok, check_seq)
            stats.attested = len(set(present))

        for rc in result.reviews:
            sha = hashlib.sha256(rc.text.encode()).hexdigest()
            if b.add_review(source.id, rc.entry_id, rc.target_fact_id, rc.kind, rc.text, sha):
                stats.reviews_staged += 1

        if result.newest_entry_time is not None:
            rt.newest_seen = max(rt.newest_seen or result.newest_entry_time, result.newest_entry_time)
        if result.sequence_last is not None:
            rt.last_sequence = result.sequence_last
        b.set_runtime(rt)
    return stats


def _retracted(v: FactVersion, now: int) -> dict:
    row = {name: getattr(v, name) for name in (
        "fact_id", "source_id", "entry_id", "entity", "entity_label", "predicate", "scope", "subject", "value",
        "qualifiers", "event_at", "effective_at", "valid_to", "published_at", "explicit_expires_at", "supersedes",
        "source_revision", "origin", "evidence", "topics", "authority")}
    row["state"] = State.RETRACTED
    row["observed_at"] = now
    row["content_hash"] = content_hash(row)
    return row


def _describe(facts: list[ParsedFact], kind: str) -> str:
    parts = [f"{f.entity} {f.predicate} {f.subject or ''} {f.value}".strip() for f in facts]
    lead = "Entry reverted to earlier content" if kind == "revert" else "Entry changed again without a revision field"
    return f"{lead}: " + "; ".join(parts)


def record_not_modified(store: Store, source: SourceConfig, *, now: int) -> int:
    """A 304: the endpoint's content is unchanged since the last good fetch.

    For an attesting endpoint, everything the last good check attested is
    attested again. A non-attesting endpoint only records that it was reached.
    """
    rt = store.get_runtime(source.id)
    latest_event = store.latest_source_event(source.id)
    extended = 0
    with store.batch(now) as b:
        check_seq = b.add_check(source.id, 304, None, source.attests_current, "not_modified")
        if source.attests_current and rt.last_ok_check_seq is not None:
            extended = b.extend_all_attestations(source.id, rt.last_ok_check_seq, check_seq)
        rt.last_ok_check_seq = check_seq
        rt.failures = 0
        if latest_event and latest_event[0] == "paused" and (latest_event[1] or "").startswith("outage"):
            b.add_source_event(source.id, "ok", "recovered")
        b.set_runtime(rt)
    return extended


def record_failure(store: Store, source: SourceConfig, *, now: int, kind: str, http_status: int | None, outage_failures: int) -> bool:
    """Record a failed poll. Returns True if this failure paused the source."""
    rt = store.get_runtime(source.id)
    latest_event = store.latest_source_event(source.id)
    paused_now = False
    with store.batch(now) as b:
        b.add_check(source.id, http_status, None, False, f"error:{kind}")
        rt.failures += 1
        already_paused = bool(latest_event and latest_event[0] == "paused")
        if rt.failures >= outage_failures and not already_paused:
            b.add_source_event(source.id, "paused", f"outage: {rt.failures} consecutive failures ({kind})")
            paused_now = True
        b.set_runtime(rt)
    return paused_now


def record_parse_error(store: Store, source: SourceConfig, *, now: int, message: str) -> None:
    rt = store.get_runtime(source.id)
    with store.batch(now) as b:
        b.add_check(source.id, 200, None, False, "error:parse")
        b.add_source_event(source.id, "paused", f"parser_drift: {message}"[:300])
        b.set_runtime(rt)
