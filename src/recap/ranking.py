"""Candidate pool, key-level checks and utility scoring (spec section 3)."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Iterable

from .config import ModelEntry, ProfileManifest, Settings
from .index import ACTIVE, ANNOUNCEMENT, FactView, Index, group_key
from .model import AUTHORITY_SCORE, EVENT, PREDICATES, State, canonical_json, version_tuple
from .timeutil import DAY, HOUR, date_str, hour_bucket

STABLE_CHANNEL = "stable"


@dataclass(slots=True)
class Candidate:
    fact_id: str
    view: FactView
    cls: str
    R: float
    I: float
    A: float
    F: float
    u: float
    supporting: tuple[str, ...] = ()

    @property
    def version(self):
        return self.view.version


@dataclass
class PoolStats:
    considered: int = 0
    eligible: int = 0
    pooled: int = 0
    after_key_checks: int = 0
    above_threshold: int = 0
    selected_pool: int = 0
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


def utility(R: float, I: float, A: float, F: float) -> float:
    return round(0.40 * R + 0.25 * I + 0.20 * A + 0.15 * F, 6)


def freshness(publish_time: int, T_bucket: int, half_life_hours: float) -> float:
    age_hours = max(0, T_bucket - publish_time) / HOUR
    return 2.0 ** (-age_hours / half_life_hours)


def window_start(T: int, model: ModelEntry, settings: Settings) -> int:
    if model.knowledge_through is None:
        return T - settings.default_lookback_days * DAY
    return max(T - settings.max_lookback_days * DAY, model.knowledge_through - settings.knowledge_margin_days * DAY)


def _sig(value: dict):
    """A cheap exact signature for a fact value (flat dicts of scalars in practice)."""
    try:
        sig = tuple(sorted(value.items()))
        hash(sig)
        return sig
    except TypeError:
        return canonical_json(value)


def _value_sig(view: FactView):
    return _sig(view.version.value)


class PointInTime:
    """Memoised reads of the index at snapshot ``W`` and time ``T``.

    Two levels of checks:

    * :meth:`valid`: is a fact a correct, current claim at (W, T)? Used for
      citations, ``resolve`` and ``lookup``.
    * :meth:`selection_ok`: is it also worth a packet line? Adds redundancy
      checks (dominance, restatement) on top of validity.
    """

    def __init__(self, index: Index, W: int, T: int):
        self.index = index
        self.W = W
        self.T = T
        self._versions: dict[str, object] = {}
        self._views: dict[str, FactView | None] = {}
        self._admit: dict[str, str | None] = {}
        self._reason: dict[str, str] = {}
        self._keys: dict[tuple, dict] = {}
        self._groups: dict[tuple, tuple[str | None, str | None]] = {}
        # Earliest time after T at which anything examined here can change
        # (an expiry, a validity end, an effective or publication date, a new
        # version becoming known). Results computed at T hold until then.
        self.next_transition: int | None = None

    def note(self, t: int | None) -> None:
        if t is not None and t > self.T and (self.next_transition is None or t < self.next_transition):
            self.next_transition = t

    # -- basic reads -----------------------------------------------------------------

    def version(self, fact_id: str):
        """The version of a fact as known at (W, T), enrolled or not."""
        if fact_id not in self._versions:
            self._versions[fact_id] = self.index.version_at(fact_id, self.W, self.T)
            self.note(self.index.next_observation(fact_id, self.W, self.T))
        return self._versions[fact_id]

    def scan(self, order: list[tuple]):
        """Yield fact IDs from an ordered index list, each once, at its version as of (W, T).

        Entries end with ``(fact_id, seq)``; an entry counts only if it is the
        version known at (W, T).
        """
        seen: set[str] = set()
        for entry in order:
            fid, seq = entry[-2], entry[-1]
            if fid in seen:
                continue
            v = self.version(fid)
            if v is None or v.seq != seq:
                continue
            seen.add(fid)
            yield fid

    def view(self, fact_id: str) -> FactView | None:
        if fact_id not in self._views:
            view = self.index.view(fact_id, self.W, self.T)
            self._views[fact_id] = view
            self.version(fact_id)
            if view is not None:
                v = view.version
                for t in (view.expires_at, v.valid_to, v.effective_at, v.event_at, v.published_at):
                    self.note(t)
        return self._views[fact_id]

    def admitted(self, fact_id: str) -> str | None:
        """Admission class, also requiring that no correction supersedes it."""
        if fact_id in self._admit:
            return self._admit[fact_id]
        view = self.view(fact_id)
        if view is None:
            self._admit[fact_id] = None
            self._reason[fact_id] = "unknown_or_unenrolled"
            return None
        cls, reason = Index.admission(view, self.T)
        if cls is not None and self.superseded_by(fact_id):
            cls, reason = None, "superseded"
        self._admit[fact_id] = cls
        self._reason[fact_id] = reason
        return cls

    def reason(self, fact_id: str) -> str:
        self.admitted(fact_id)
        return self._reason.get(fact_id, "unknown")

    def superseded_by(self, fact_id: str) -> str | None:
        """A correction supersedes whether or not it is itself still admitted
        (expired, or corrected again), unless it was retracted. That makes
        chains A <- B <- C exclude both A and B."""
        for other in sorted(self.index.superseders.get(fact_id, ())):
            if other == fact_id:
                continue
            v = self.version(other)
            if v is None or v.state is State.RETRACTED:
                continue
            if v.published_at is not None and v.published_at > self.T:
                self.note(v.published_at)
                continue
            return other
        return None

    # -- key-level checks ------------------------------------------------------------------

    def _key_info(self, key: tuple) -> dict:
        info = self._keys.get(key)
        if info is not None:
            return info
        active: list[str] = []
        announced: list[str] = []
        for fid in sorted(self.index.by_key.get(key, ())):
            cls = self.admitted(fid)
            if cls == ACTIVE:
                active.append(fid)
            elif cls == ANNOUNCEMENT:
                announced.append(fid)
        spec = PREDICATES[key[1]]
        info = {"kind": spec.kind, "disputed": False, "current": None, "sigs": {}, "ann_disputed": set()}
        if spec.kind == EVENT:
            members = active + announced
            sigs = {}
            for fid in members:
                v = self.view(fid).version
                sigs[fid] = (_sig(v.value), date_str(v.change_time))
            info["sigs"] = sigs
            info["disputed"] = len(set(sigs.values())) > 1
        else:
            # The newest effective active value is current. Announcements never
            # are: they take effect after T. Another source asserting a
            # different value, published after the current value took effect,
            # is a conflict rather than stale information.
            if active:
                def order(fid: str):
                    v = self.view(fid).version
                    return (v.change_time, v.publish_time, fid)

                current = max(active, key=order)
                cur = self.view(current).version
                sigs = {fid: _value_sig(self.view(fid)) for fid in active}
                info["current"] = current
                info["sigs"] = sigs
                for fid in active:
                    v = self.view(fid).version
                    if v.source_id != cur.source_id and sigs[fid] != sigs[current] and v.publish_time > cur.change_time:
                        info["disputed"] = True
                        break
            # Announcements for the same moment from different sources must agree.
            by_time: dict[int, set[str]] = {}
            for fid in announced:
                by_time.setdefault(self.view(fid).version.effective_at, set()).add(fid)
            for fids in by_time.values():
                if len({_value_sig(self.view(f)) for f in fids}) > 1:
                    info["ann_disputed"].update(fids)
        by_sig: dict = {}
        for fid, sig in info["sigs"].items():
            by_sig.setdefault(sig, []).append(fid)
        info["by_sig"] = {sig: tuple(fids) for sig, fids in by_sig.items()}
        self._keys[key] = info
        return info

    def key_check(self, fact_id: str) -> tuple[str, str | None, tuple[str, ...]]:
        """Check a fact against every other fact with the same key.

        Returns ``(status, current, agreeing)``. Status is ``ok``, ``disputed``
        or ``outdated`` (a newer value exists for a state key). ``agreeing``
        holds admitted facts that make the same claim, for citations.
        """
        view = self.view(fact_id)
        cls = self.admitted(fact_id)
        if view is None or cls is None:
            return ("not_admitted", None, ())
        info = self._key_info(view.version.key)
        if info["kind"] == EVENT:
            if info["disputed"]:
                return ("disputed", None, ())
            same = info["by_sig"].get(info["sigs"].get(fact_id), ())
            return ("ok", fact_id, tuple(f for f in same if f != fact_id))
        if cls == ANNOUNCEMENT:
            if fact_id in info["ann_disputed"]:
                return ("disputed", None, ())
            return ("ok", fact_id, ())
        if info["disputed"]:
            return ("disputed", None, ())
        current, sigs = info["current"], info["sigs"]
        if fact_id == current or sigs.get(fact_id) == sigs[current]:
            same = info["by_sig"][sigs[current]]
            return ("ok", current, tuple(f for f in same if f != fact_id))
        return ("outdated", current, ())

    # -- redundancy (selection only) ------------------------------------------------------

    def _group(self, gk: tuple) -> tuple[str | None, str | None]:
        """(highest active release, soonest announcement) for a release group.

        Actives are scanned highest version first (newest first among equal or
        unparseable versions) and the first admitted one wins. Announcements
        take effect after ``T``, so a newest-change-first scan can stop at the
        first row that is already in effect.
        """
        if gk in self._groups:
            return self._groups[gk]
        newest_active = None
        for fid in self.scan(self.index.group_vorder.get(gk, [])):
            if self.admitted(fid) == ACTIVE:
                newest_active = fid
                break
        announcements = []
        for fid in self.scan(self.index.group_order.get(gk, [])):
            v = self.version(fid)
            if v.change_time <= self.T:
                break
            if self.admitted(fid) == ANNOUNCEMENT:
                announcements.append((v.effective_at, fid))
        soonest = min(announcements)[1] if announcements else None
        self._groups[gk] = (newest_active, soonest)
        return self._groups[gk]

    def dominance_ok(self, fact_id: str) -> bool:
        """Only the highest active release and the soonest announcement in an
        (entity, scope, channel) group are worth a line. A pre-release is also
        redundant once a stable release of the same or a later version exists."""
        # Needs only stored versions, so it runs before the costlier verification window.
        v = self.version(fact_id)
        if v is None:
            return False
        if not PREDICATES[v.predicate].dominance:
            return True
        gk = group_key(v)
        newest_active, soonest = self._group(gk)
        scheduled = v.state is State.SCHEDULED
        winner = soonest if scheduled else newest_active
        if winner is None:
            return False
        # Another source's row for the same release is the same claim, not a rival.
        if winner != fact_id and self.version(winner).key != v.key:
            return False
        if not scheduled and v.channel and v.channel != STABLE_CHANNEL:
            stable_active, _ = self._group((gk[0], gk[1], gk[2], STABLE_CHANNEL))
            if stable_active is not None:
                mine = version_tuple(str(v.value.get("version", "")))
                theirs = version_tuple(str(self.version(stable_active).value.get("version", "")))
                if mine is not None and theirs is not None and theirs >= mine:
                    return False
        return True

    def attached_ok(self, fact_id: str, *, selection: bool = False) -> bool:
        """Rows attached to another fact (reviewed highlights) follow their target:
        valid while the target is valid, selectable while it is selectable."""
        view = self.view(fact_id)
        if view is None:
            return False
        spec = PREDICATES[view.version.predicate]
        if spec.attached_to is None:
            return True
        target = view.version.qualifiers.get("target_fact_id")
        if not isinstance(target, str) or self.admitted(target) is None:
            return False
        target_view = self.view(target)
        if target_view is None or target_view.version.predicate != spec.attached_to:
            return False
        if self.key_check(target)[0] != "ok":
            return False
        return self.dominance_ok(target) if selection else True

    def restates_known_value(self, fact_id: str, knowledge_through: int | None) -> bool:
        """True when an active state fact repeats the value already in effect at the model's cutoff."""
        if knowledge_through is None:
            return False
        view = self.view(fact_id)
        if view is None or PREDICATES[view.version.predicate].kind == EVENT or self.admitted(fact_id) != ACTIVE:
            return False
        v = view.version
        prior = None
        prior_key = None
        for fid in self.index.by_key.get(v.key, ()):
            if fid == fact_id:
                continue
            other = self.view(fid)
            if other is None or other.version.state not in (State.ACTIVE,):
                continue
            ov = other.version
            if ov.change_time > knowledge_through or ov.change_time >= v.change_time:
                continue
            if ov.published_at is not None and ov.published_at > self.T:
                continue
            k = (ov.change_time, ov.publish_time, fid)
            if prior_key is None or k > prior_key:
                prior, prior_key = other, k
        return prior is not None and _value_sig(prior) == _value_sig(view)

    # -- combined checks ---------------------------------------------------------------------

    def valid(self, fact_id: str) -> tuple[bool, str]:
        """Is the fact a correct claim to cite at (W, T)?"""
        if self.admitted(fact_id) is None:
            return False, self.reason(fact_id)
        status, _, _ = self.key_check(fact_id)
        if status != "ok":
            return False, status
        if not self.attached_ok(fact_id):
            return False, "target_not_current"
        return True, "ok"

    def selection_ok(self, fact_id: str, knowledge_through: int | None) -> tuple[bool, str]:
        """Validity plus redundancy checks for packet selection."""
        if self.admitted(fact_id) is None:
            return False, self.reason(fact_id)
        # Dominance first: it is memoised per release group and drops most rows.
        if not self.dominance_ok(fact_id):
            return False, "dominated"
        ok, reason = self.valid(fact_id)
        if not ok:
            return ok, reason
        if not self.attached_ok(fact_id, selection=True):
            return False, "target_dominated"
        view = self.view(fact_id)
        if PREDICATES[view.version.predicate].kind != EVENT and self.admitted(fact_id) == ACTIVE:
            _, current, _ = self.key_check(fact_id)
            # An older row from the same source repeating the current value adds nothing.
            if current != fact_id and self.view(current).version.source_id == view.version.source_id:
                return False, "older_copy"
            # Restatement is a property of the key's current value.
            if self.restates_known_value(current, knowledge_through):
                return False, "restatement"
        return True, "ok"


class Ranker:
    def __init__(self, index: Index, settings: Settings):
        self.index = index
        self.settings = settings

    def candidates(self, profile: ProfileManifest, model: ModelEntry, W: int, T: int, pit: PointInTime | None = None) -> tuple[list[Candidate], PoolStats]:
        s = self.settings
        pit = pit or PointInTime(self.index, W, T)
        stats = PoolStats()
        ws = window_start(T, model, s)
        window_moves = ws == T - s.max_lookback_days * DAY or model.knowledge_through is None
        if not window_moves:  # the window is pinned to the cutoff until the lookback limit catches up
            pit.note(model.knowledge_through - s.knowledge_margin_days * DAY + s.max_lookback_days * DAY + 1)
        scope = set(profile.scope)
        deny = set(profile.deny)
        entities = set(profile.entities)

        live: dict[str, FactView] = {}
        checked: dict[str, bool] = {}

        def eligible(fid: str) -> bool:
            """Every filter runs here, before any truncation."""
            if fid in checked:
                return checked[fid]
            stats.considered += 1
            ok = False
            v = pit.version(fid)
            # Cheap checks on the stored version first; the verification window comes last.
            if v is None:
                stats.drop("unknown")
            elif v.change_time < ws:
                stats.drop("outside_window")
            elif v.scope not in scope:
                stats.drop("out_of_scope")
            elif deny.intersection(v.topics):
                stats.drop("denied_topic")
            elif not pit.dominance_ok(fid):
                stats.drop("dominated")
            else:
                sel_ok, reason = pit.selection_ok(fid, model.knowledge_through)
                if sel_ok:
                    stats.eligible += 1
                    live[fid] = pit.view(fid)
                    ok = True
                    if window_moves:  # the fact leaves the window once T - lookback passes it
                        pit.note(v.change_time + (T - ws) + 1)
                else:
                    stats.drop(reason)
            checked[fid] = ok
            return ok

        if profile.kind == "editorial":
            return self._editorial(profile, W, T, pit, eligible, live, stats)

        def newest(order, limit: int) -> list[str]:
            out: list[str] = []
            for fid in pit.scan(order):
                if eligible(fid):
                    out.append(fid)
                    if len(out) >= limit:
                        break
            return out

        pool: set[str] = set()
        for topic in profile.top_topics(s.top_topics):
            pool.update(newest(self.index.topic_order.get(topic, []), s.topic_take))
        # The newest exact-entity matches across all profile entities: merge the
        # per-entity lists newest-first and stop at the limit.
        merged = heapq.merge(*(self.index.entity_order.get(e, []) for e in sorted(entities)))
        pool.update(newest(merged, s.entity_take))
        stats.pooled = len(pool)

        chosen: dict[tuple, Candidate] = {}
        T_bucket = hour_bucket(T)
        for fid in sorted(pool):
            status, current, agreeing = pit.key_check(fid)
            is_state = PREDICATES[pit.view(fid).version.predicate].kind != EVENT
            rep = self._representative(fid, current, agreeing, live, is_state)
            if rep is None:
                stats.drop("representative_not_eligible")
                continue
            view = live[rep]
            v = view.version
            cls = pit.admitted(rep)
            slot = (v.key, cls, v.effective_at if cls == ANNOUNCEMENT else None)
            if slot in chosen:
                continue
            R = 1.0 if v.entity in entities else max((profile.topics.get(t, 0.0) for t in v.topics), default=0.0)
            I = PREDICATES[v.predicate].priority
            A = AUTHORITY_SCORE[v.authority]
            F = freshness(v.publish_time, T_bucket, s.half_life_hours)
            u = utility(R, I, A, F)
            supporting = tuple(sorted(f for f in set(agreeing) | {fid} if f != rep and pit.admitted(f) is not None))
            chosen[slot] = Candidate(rep, view, cls, R, I, A, F, u, supporting)
        stats.after_key_checks = len(chosen)

        ranked = [c for c in chosen.values() if c.R > 0 and c.u >= profile.min_score - 1e-9]
        stats.above_threshold = len(ranked)
        ranked.sort(key=lambda c: (-c.u, -c.version.change_time, c.fact_id))
        ranked = ranked[: s.selection_pool]
        stats.selected_pool = len(ranked)
        return ranked, stats

    @staticmethod
    def _representative(fid: str, current: str | None, agreeing: Iterable[str], live: dict[str, FactView], is_state: bool) -> str | None:
        """One fact stands for each claim; the others become supporting citations.

        For a state key the current fact stands when it is eligible here. For
        an event key, or when the current fact is not eligible, the most
        authoritative and earliest-published agreeing fact stands.
        """
        if is_state and current is not None and current in live:
            return current
        options = {fid, *agreeing}
        if current is not None:
            options.add(current)
        eligible = [f for f in options if f in live]
        if not eligible:
            return None

        def best(f: str):
            v = live[f].version
            return (-AUTHORITY_SCORE[v.authority], v.publish_time, f)

        return min(eligible, key=best)

    def _editorial(self, profile, W, T, pit, eligible, live, stats) -> tuple[list[Candidate], PoolStats]:
        """Editor-designated facts pass the same filters as ranked ones, without R or min_score."""
        picks = self.index.editorial_picks(profile.id, W, T)
        picks = picks[-profile.max_editorial:] if profile.max_editorial else []
        out: list[Candidate] = []
        for fid in reversed(picks):  # most recent designation first
            if not eligible(fid):
                continue
            view = live[fid]
            out.append(Candidate(fid, view, pit.admitted(fid), 1.0, 1.0, AUTHORITY_SCORE[view.version.authority], 1.0, 1.0))
        stats.pooled = stats.after_key_checks = stats.above_threshold = stats.selected_pool = len(out)
        return out, stats
