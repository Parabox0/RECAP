"""In-memory index over the append-only store.

One :class:`Index` serves every snapshot up to its watermark: each query names
a snapshot ``W`` (only rows with ``seq <= W`` count) and a time ``T`` (only
what RECAP had observed by ``T`` counts). Live serving uses the newest
watermark and the current time; replay pins both.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Mapping

from .config import SourceConfig
from .model import AUTHORITY_SCORE, PREDICATES, FactVersion, State, version_tuple
from .store import Delta
from .timeutil import HOUR

ACTIVE = "active"
ANNOUNCEMENT = "announcement"
OK_OUTCOMES = ("ok", "not_modified")


def group_key(v: FactVersion) -> tuple[str, str, str, str]:
    """Dominance group: newer facts in a group make older ones redundant."""
    return (v.entity, v.predicate, v.scope, v.channel)


@dataclass(frozen=True, slots=True)
class FactView:
    """A fact as known at (W, T), with its verification window."""

    version: FactVersion
    checked_at: int
    expires_at: int


class Index:
    def __init__(self, sources: Mapping[str, SourceConfig]):
        self.sources = dict(sources)
        self.watermark = 0
        self.generation = 0
        self._versions: dict[str, list[FactVersion]] = {}
        self._checks: dict[str, tuple[list[int], list[int]]] = {}
        self._attest: dict[str, dict[tuple[str, int], int]] = {}
        self._source_events: dict[str, list[tuple[int, int, str, str | None]]] = {}
        self._editorial: dict[str, list[tuple[int, int, str, str]]] = {}
        self._revocations: dict[str, list[tuple[int, int, str]]] = {}
        self.by_topic: dict[str, set[str]] = {}
        self.by_entity: dict[str, set[str]] = {}
        self.by_key: dict[tuple, set[str]] = {}
        self.by_group: dict[tuple, set[str]] = {}
        self.superseders: dict[str, set[str]] = {}
        # Every version, newest first, for early-terminating scans:
        # (-publish_time, fact_id, seq) per topic and entity, (-change_time, fact_id, seq) per group.
        self.topic_order: dict[str, list[tuple[int, str, int]]] = {}
        self.entity_order: dict[str, list[tuple[int, str, int]]] = {}
        self.group_order: dict[tuple, list[tuple[int, str, int]]] = {}
        # Release groups by version, highest first: ((parsed?, -parts...), -change_time, fact_id, seq).
        self.group_vorder: dict[tuple, list[tuple]] = {}
        self._first_seq: int | None = None
        self.last_retracted: set[str] = set()

    # -- loading -----------------------------------------------------------------

    def apply(self, delta: Delta) -> None:
        retracted: set[str] = set()
        dirty: list[list] = []
        for v in delta.versions:
            self._versions.setdefault(v.fact_id, []).append(v)
            pub_entry = (-v.publish_time, v.fact_id, v.seq)
            for topic in v.topics:
                self.by_topic.setdefault(topic, set()).add(v.fact_id)
                lst = self.topic_order.setdefault(topic, [])
                lst.append(pub_entry)
                dirty.append(lst)
            self.by_entity.setdefault(v.entity, set()).add(v.fact_id)
            lst = self.entity_order.setdefault(v.entity, [])
            lst.append(pub_entry)
            dirty.append(lst)
            self.by_key.setdefault(v.key, set()).add(v.fact_id)
            spec = PREDICATES.get(v.predicate)
            if spec is not None and spec.dominance:
                gk = group_key(v)
                self.by_group.setdefault(gk, set()).add(v.fact_id)
                lst = self.group_order.setdefault(gk, [])
                lst.append((-v.change_time, v.fact_id, v.seq))
                dirty.append(lst)
                vt = version_tuple(str(v.value.get("version", "")))
                vkey = (0, tuple(-x for x in vt)) if vt is not None else (1, ())
                lst = self.group_vorder.setdefault(gk, [])
                lst.append((vkey, -v.change_time, v.fact_id, v.seq))
                dirty.append(lst)
            for old in v.supersedes:
                self.superseders.setdefault(old, set()).add(v.fact_id)
            if v.state is State.RETRACTED:
                retracted.add(v.fact_id)
            if self._first_seq is None or v.seq < self._first_seq:
                self._first_seq = v.seq
        for lst in {id(x): x for x in dirty}.values():
            lst.sort()
        for seq, source_id, at, outcome in delta.checks:
            if outcome in OK_OUTCOMES:
                seqs, ats = self._checks.setdefault(source_id, ([], []))
                seqs.append(seq)
                ats.append(at)
        for fact_id, source_id, first_seq, last_seq in delta.attestations:
            self._attest.setdefault(fact_id, {})[(source_id, first_seq)] = last_seq
        for seq, source_id, at, status, reason in delta.source_events:
            self._source_events.setdefault(source_id, []).append((seq, at, status, reason))
        for seq, profile_id, fact_id, action, at in delta.editorial:
            self._editorial.setdefault(profile_id, []).append((seq, at, fact_id, action))
        for seq, packet_id, at, reason in delta.revocations:
            self._revocations.setdefault(packet_id, []).append((seq, at, reason))
        self.watermark = max(self.watermark, delta.upto)
        self.generation += 1
        self.last_retracted = retracted

    def has_facts(self, W: int) -> bool:
        return self._first_seq is not None and self._first_seq <= W

    def fact_ids(self) -> list[str]:
        return sorted(self._versions)

    # -- point-in-time reads -----------------------------------------------------------

    def version_at(self, fact_id: str, W: int, T: int) -> FactVersion | None:
        versions = self._versions.get(fact_id)
        if not versions:
            return None
        i = bisect_right(versions, W, key=_seq_of) - 1
        while i >= 0 and versions[i].observed_at > T:
            i -= 1
        return versions[i] if i >= 0 else None

    def next_observation(self, fact_id: str, W: int, T: int) -> int | None:
        """Earliest observation time after ``T`` of a version within snapshot ``W``."""
        versions = self._versions.get(fact_id)
        if not versions or len(versions) == 1 and versions[0].observed_at <= T:
            return None
        times = [v.observed_at for v in versions if v.seq <= W and v.observed_at > T]
        return min(times) if times else None

    def _latest_ok_check(self, source_id: str, W: int, T: int, lo_seq: int | None = None, hi_seq: int | None = None) -> int | None:
        found = self._checks.get(source_id)
        if not found:
            return None
        seqs, ats = found
        bound = W if hi_seq is None else min(W, hi_seq)
        j = bisect_right(seqs, bound) - 1
        k = bisect_right(ats, T) - 1
        m = min(j, k)
        if m < 0:
            return None
        if lo_seq is not None and seqs[m] < lo_seq:
            return None
        return ats[m]

    def checked_at(self, v: FactVersion, W: int, T: int) -> int:
        best = v.observed_at
        intervals = self._attest.get(v.fact_id)
        if intervals:
            for (source_id, first), last in intervals.items():
                if first > W:
                    continue
                at = self._latest_ok_check(source_id, W, T, lo_seq=first, hi_seq=last)
                if at is not None and at > best:
                    best = at
        return best

    def view(self, fact_id: str, W: int, T: int) -> FactView | None:
        v = self.version_at(fact_id, W, T)
        if v is None:
            return None
        source = self.sources.get(v.source_id)
        spec = PREDICATES.get(v.predicate)
        if source is None or spec is None or v.authority not in AUTHORITY_SCORE:
            return None
        checked = self.checked_at(v, W, T)
        recheck = min(spec.recheck_hours, source.max_evidence_age_hours) * HOUR
        expires = checked + recheck
        if v.explicit_expires_at is not None:
            expires = min(expires, v.explicit_expires_at)
        return FactView(v, checked, expires)

    @staticmethod
    def admission(view: FactView, T: int) -> tuple[str | None, str]:
        """Admission class at time ``T``: ``active``, ``announcement`` or ``None`` with a reason."""
        v = view.version
        if v.state is State.RETRACTED:
            return None, "retracted"
        if v.state is State.DISPUTED:
            return None, "disputed"
        if v.observed_at > T:
            return None, "not_yet_observed"
        if v.published_at is not None and v.published_at > T:
            return None, "not_yet_published"
        if T >= view.expires_at:
            return None, "expired"
        if v.state is State.ACTIVE:
            effective = v.change_time
            if effective > T:
                return None, "not_yet_effective"
            if v.valid_to is not None and T >= v.valid_to:
                return None, "validity_ended"
            return ACTIVE, "ok"
        if v.state is State.SCHEDULED:
            if v.effective_at is None:
                return None, "unverified_schedule"
            if v.effective_at > T:
                return ANNOUNCEMENT, "ok"
            return None, "lapsed_schedule"
        return None, "unknown_state"  # pragma: no cover

    # -- sources, editorial, revocations -----------------------------------------------

    def source_status(self, source_id: str, W: int, T: int) -> tuple[str, str | None, int | None]:
        """``(status, reason, last_ok_check_at)``; status is ok, paused, stale, never_checked or unenrolled."""
        source = self.sources.get(source_id)
        if source is None:
            return "unenrolled", None, None
        status, reason = "ok", None
        for seq, at, st, rs in reversed(self._source_events.get(source_id, [])):
            if seq <= W and at <= T:
                status, reason = st, rs
                break
        last_ok = self._latest_ok_check(source_id, W, T)
        if status == "paused":
            return "paused", reason, last_ok
        if last_ok is None:
            return "never_checked", None, None
        if T - last_ok > source.max_evidence_age_hours * HOUR:
            return "stale", None, last_ok
        return "ok", None, last_ok

    def editorial_picks(self, profile_id: str, W: int, T: int) -> list[str]:
        """Fact IDs an editor designated, oldest designation first."""
        picks: list[str] = []
        for seq, at, fact_id, action in self._editorial.get(profile_id, []):
            if seq > W or at > T:
                continue
            if fact_id in picks:
                picks.remove(fact_id)
            if action == "add":
                picks.append(fact_id)
        return picks

    def packet_revocation(self, packet_id: str, W: int, T: int) -> str | None:
        for seq, at, reason in self._revocations.get(packet_id, []):
            if seq <= W and at <= T:
                return reason
        return None


def _seq_of(v: FactVersion) -> int:
    return v.seq
