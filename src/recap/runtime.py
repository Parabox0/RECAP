"""The request-path API: ``assemble``, ``resolve``, ``lookup`` (spec section 4)."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .config import HARD_CAP_TOKENS, ProfileManifest, RecapConfig
from .index import Index
from .model import PREDICATES, key_from_str, key_to_str
from .ranking import Candidate, PointInTime, Ranker
from .render import RenderError, render_body, render_line, render_packet, wrapper
from .select import Item, select
from .store import Store
from .timeutil import HOUR, TimeLike, hour_bucket, iso, parse_time
from .tokens import TokenCounter, counter_for

HIT = "hit"
EMPTY = "empty"
DEGRADED = "degraded"


@dataclass(frozen=True)
class AssembleRequest:
    profile_id: str
    profile_version: int
    model_id: str
    context_window: int
    base_input_tokens: int
    reserved_output_tokens: int
    serializer_version: str = "v1"
    budget: int | None = None
    as_of: TimeLike = None
    mode: str = "live"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssembleRequest":
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown request fields: {sorted(unknown)}")
        return cls(**data)


@dataclass
class AssembleResult:
    status: str
    snapshot_id: int
    added_input_tokens_max: int = 0
    suppression_allowed: bool = False
    packet_id: str | None = None
    rendered_data: str | None = None
    fact_ids: list[str] = field(default_factory=list)
    refs: dict[str, str] = field(default_factory=dict)
    cache_until: int | None = None
    reason: str | None = None
    as_of: int | None = None
    effective_budget: int | None = None
    elapsed_ms: float = 0.0
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cache_until"] = iso(self.cache_until)
        data["as_of"] = iso(self.as_of)
        return data


def _empty(reason: str, snapshot: int = 0, **extra: Any) -> AssembleResult:
    return AssembleResult(status=EMPTY, snapshot_id=snapshot, reason=reason, **extra)


class _Deadline(Exception):
    pass


class Recap:
    """In-process RECAP service bound to one store and configuration."""

    def __init__(
        self,
        config: RecapConfig,
        store: Store | None = None,
        *,
        clock: Callable[[], float] = time.time,
        metrics: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self.settings = config.settings
        self.store = store or Store(config.db_path)
        self.clock = clock
        self.metrics = metrics
        self.index = Index(config.sources)
        self.ranker = Ranker(self.index, self.settings)
        self._tokenizers: dict[str, Callable[[str], int]] = {}
        self._lock = threading.RLock()
        self._cache: OrderedDict[tuple, AssembleResult] = OrderedDict()
        self._cache_facts: dict[tuple, frozenset[str]] = {}
        self._last_refresh = float("-inf")
        self.refresh(force=True)

    # -- setup -------------------------------------------------------------------------

    def register_tokenizer(self, model_id: str, count: Callable[[str], int]) -> None:
        """Supply an exact tokenizer for a model configured with ``counting = "exact"``."""
        with self._lock:
            self._tokenizers[model_id] = count

    def now(self) -> int:
        return int(self.clock())

    def refresh(self, force: bool = False) -> int:
        """Load new store rows into the index. Called automatically on reads."""
        with self._lock:
            mono = time.monotonic()
            if not force and mono - self._last_refresh < self.settings.index_refresh_seconds:
                return self.index.watermark
            self._last_refresh = mono
            wm = self.store.watermark()
            if wm > self.index.watermark:
                self.index.apply(self.store.load_since(self.index.watermark, wm))
                if self.index.last_retracted:
                    self.purge_facts(self.index.last_retracted)
            return self.index.watermark

    def purge_facts(self, fact_ids: set[str] | frozenset[str]) -> int:
        """Drop cached packets that mention any of ``fact_ids``."""
        with self._lock:
            doomed = [k for k, facts in self._cache_facts.items() if facts & set(fact_ids)]
            for k in doomed:
                self._cache.pop(k, None)
                self._cache_facts.pop(k, None)
            return len(doomed)

    def _snapshot(self, mode: str, T: int) -> int:
        self.refresh(force=(mode == "replay"))
        if mode == "replay":
            return min(self.store.snapshot_for_time(T), self.index.watermark)
        return self.index.watermark

    # -- assemble ------------------------------------------------------------------------

    def assemble(self, request: AssembleRequest | dict[str, Any]) -> AssembleResult:
        start = time.perf_counter()
        try:
            req = request if isinstance(request, AssembleRequest) else AssembleRequest.from_dict(request)
        except (TypeError, ValueError):
            return self._finish(_empty("invalid_request"), start, None)
        if req.mode not in ("live", "replay"):
            return self._finish(_empty("invalid_request"), start, req)
        deadline = start + self.settings.deadline_ms / 1000.0 if req.mode == "live" else None
        try:
            with self._lock:
                result = self._assemble(req, deadline)
        except _Deadline:
            result = _empty("timeout", self.index.watermark)
        return self._finish(result, start, req)

    def _finish(self, result: AssembleResult, start: float, req: AssembleRequest | None) -> AssembleResult:
        result.elapsed_ms = round((time.perf_counter() - start) * 1000.0, 3)
        if self.metrics is not None:
            try:
                self.metrics(
                    {
                        "status": result.status,
                        "reason": result.reason,
                        "profile_id": req.profile_id if req else None,
                        "profile_version": req.profile_version if req else None,
                        "model_id": req.model_id if req else None,
                        "mode": req.mode if req else None,
                        "snapshot_id": result.snapshot_id,
                        "added_input_tokens_max": result.added_input_tokens_max,
                        "fact_ids": list(result.fact_ids),
                        "elapsed_ms": result.elapsed_ms,
                        "cache_hit": result.cache_hit,
                    }
                )
            except Exception:  # metrics must never break the request path
                pass
        return result

    @staticmethod
    def _check(deadline: float | None) -> None:
        if deadline is not None and time.perf_counter() > deadline:
            raise _Deadline()

    def _assemble(self, req: AssembleRequest, deadline: float | None) -> AssembleResult:
        model = self.config.models.get(req.model_id)
        if model is None:
            return _empty("unsupported_model", self.index.watermark)
        if req.serializer_version != model.serializer_version:
            return _empty("model_mismatch", self.index.watermark)
        counter = counter_for(model, self._tokenizers)
        if counter is None:
            return _empty("unsupported_model", self.index.watermark)
        profile = self.config.profile(req.profile_id, req.profile_version)
        if profile is None:
            return _empty("unknown_profile", self.index.watermark)
        try:
            T = parse_time(req.as_of) if req.as_of is not None else self.now()
        except (TypeError, ValueError):
            return _empty("invalid_request", self.index.watermark)
        W = self._snapshot(req.mode, T)
        if not self.index.has_facts(W):
            return _empty("cold_index", W, as_of=T)
        if profile.kind == "ranked" and not profile.topics and not profile.entities:
            return _empty("empty_profile", W, as_of=T)
        budget = profile.budget
        if req.budget is not None:
            if isinstance(req.budget, bool) or not isinstance(req.budget, int) or not 0 <= req.budget <= HARD_CAP_TOKENS:
                return _empty("invalid_request", W, as_of=T)
            budget = min(budget, req.budget)  # a request may lower the profile's budget, never raise it
        for value in (req.context_window, req.base_input_tokens, req.reserved_output_tokens):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return _empty("invalid_request", W, as_of=T)
        headroom = req.context_window - req.base_input_tokens - req.reserved_output_tokens - self.settings.safety_margin_tokens
        effective = min(budget, headroom)
        open_tag, close_tag = wrapper(T)
        wrapper_cost = counter.wrapper_cost(open_tag, close_tag)
        if effective - wrapper_cost <= 0:
            return _empty("low_budget", W, as_of=T, effective_budget=max(effective, 0))

        T_bucket = hour_bucket(T)
        cache_key = (profile.id, profile.version, W, model.id, model.serializer_version, effective, T_bucket)
        if req.mode == "live":
            cached = self._cache.get(cache_key)
            if cached is not None and cached.cache_until is not None and T < cached.cache_until:
                self._cache.move_to_end(cache_key)
                hit = AssembleResult(**{**cached.__dict__})
                hit.fact_ids = list(cached.fact_ids)
                hit.refs = dict(cached.refs)
                hit.cache_hit = True
                hit.as_of = T
                return hit

        pit = PointInTime(self.index, W, T)
        candidates, _stats = self.ranker.candidates(profile, model, W, T, pit)
        self._check(deadline)
        if not candidates:
            return _empty("nothing_selected", W, as_of=T, effective_budget=effective)

        try:
            bodies = [render_line(c.version, c.cls, "f0") for c in candidates]
        except RenderError:
            return _empty("malformed_row", W, as_of=T, effective_budget=effective)
        costs = [counter.line_cost(b) for b in bodies]
        line_budget = effective - wrapper_cost
        chosen_ranks: list[int] = []
        packet_text = ""
        lines: list[str] = []
        added = 0
        for _attempt in range(3):
            items = [
                Item(rank=i, group=c.version.entity, u=c.u, cost=costs[i])
                for i, c in enumerate(candidates)
            ]
            chosen_ranks = select(
                items,
                budget=line_budget,
                max_items=self.settings.max_facts,
                per_group=self.settings.per_entity_cap,
            )
            self._check(deadline)
            if not chosen_ranks:
                return _empty("nothing_selected", W, as_of=T, effective_budget=effective)
            lines = [render_line(candidates[r].version, candidates[r].cls, f"f{n}") for n, r in enumerate(chosen_ranks, 1)]
            packet_text = render_packet(open_tag, close_tag, lines)
            added = wrapper_cost + sum(costs[r] for r in chosen_ranks)
            exact = counter.recount(packet_text)
            if exact is None or exact <= effective:
                if exact is not None:
                    added = exact
                break
            line_budget -= exact - effective
        else:
            return _empty("budget_overrun_guard", W, as_of=T, effective_budget=effective)

        chosen = [candidates[r] for r in chosen_ranks]
        healthy, health_change = self._profile_sources_healthy(profile, chosen, W, T)
        status = HIT if healthy else DEGRADED
        # The packet holds until the hour bucket ends or anything examined can change.
        cache_until = T_bucket + HOUR
        for t in (pit.next_transition, health_change):
            if t is not None:
                cache_until = min(cache_until, t)
        refs = {f"f{n}": c.fact_id for n, c in enumerate(chosen, 1)}
        digest = hashlib.sha256(repr((cache_key, tuple(refs.values()))).encode()).hexdigest()
        packet_id = "pk_" + digest[:24]
        record = {
            "packet_id": packet_id,
            "snapshot_id": W,
            "as_of": T,
            "mode": req.mode,
            "profile_id": profile.id,
            "profile_version": profile.version,
            "model_id": model.id,
            "status": status,
            "cache_until": cache_until,
            "expires_min": min(c.view.expires_at for c in chosen),
            "rendered": packet_text,
            "refs": {ref: self._as_packed(c, lines[n]) for n, (ref, c) in enumerate(zip(refs, chosen))},
        }
        self.store.put_packet(packet_id, T, W, record, list(refs.values()))
        result = AssembleResult(
            status=status,
            snapshot_id=W,
            added_input_tokens_max=added,
            suppression_allowed=status == HIT,
            packet_id=packet_id,
            rendered_data=packet_text,
            fact_ids=list(refs.values()),
            refs=refs,
            cache_until=cache_until,
            reason=None if status == HIT else "source_paused_or_stale",
            as_of=T,
            effective_budget=effective,
        )
        self._check(deadline)
        if req.mode == "live":
            self._cache[cache_key] = result
            self._cache_facts[cache_key] = frozenset(result.fact_ids)
            while len(self._cache) > self.settings.cache_size:
                old, _ = self._cache.popitem(last=False)
                self._cache_facts.pop(old, None)
        return result

    @staticmethod
    def _as_packed(c: Candidate, line: str) -> dict[str, Any]:
        v = c.version
        return {
            "fact_id": c.fact_id,
            "line": line,
            "class": c.cls,
            "canonical_url": v.evidence.canonical_url,
            "evidence_hash": v.evidence.blob_sha256,
            "effective_at": v.effective_at,
            "published_at": v.published_at,
            "checked_at": c.view.checked_at,
            "expires_at": c.view.expires_at,
            "scope": v.scope,
            "supporting": list(c.supporting),
            "utility": c.u,
        }

    def _profile_sources(self, profile: ProfileManifest, chosen: list[Candidate]) -> set[str]:
        topics = set(profile.topics)
        entities = set(profile.entities)
        ids = {c.version.source_id for c in chosen}
        if profile.kind == "ranked":
            for sid, src in self.config.sources.items():
                if topics.intersection(src.topics) or entities.intersection(src.entities):
                    ids.add(sid)
        return ids

    def _profile_sources_healthy(self, profile: ProfileManifest, chosen: list[Candidate], W: int, T: int) -> tuple[bool, int | None]:
        """Whether every source the profile relies on is healthy, and when that can next change."""
        healthy, change = True, None
        for sid in sorted(self._profile_sources(profile, chosen)):
            status, _, last_ok = self.index.source_status(sid, W, T)
            if status != "ok":
                healthy = False
            elif last_ok is not None:
                stale_at = last_ok + self.config.sources[sid].max_evidence_age_hours * HOUR + 1
                change = stale_at if change is None else min(change, stale_at)
        return healthy, change

    # -- resolve ---------------------------------------------------------------------------

    def resolve(self, packet_id: str, ref: str, at: TimeLike = None, mode: str = "live") -> dict[str, Any]:
        """Provenance for one packet line: as packed, and as it stands at ``at``."""
        record = self.store.get_packet(packet_id)
        if record is None:
            return {"packet_id": packet_id, "ref": ref, "error": "unknown_packet"}
        entry = record["refs"].get(ref)
        if entry is None:
            matches = [(r, e) for r, e in record["refs"].items() if e["fact_id"] == ref]
            if not matches:
                return {"packet_id": packet_id, "ref": ref, "error": "unknown_ref"}
            ref, entry = matches[0]
        T = parse_time(at) if at is not None else self.now()
        with self._lock:
            W = self._snapshot(mode, T)
            pit = PointInTime(self.index, W, T)
            fact_id = entry["fact_id"]
            view = pit.view(fact_id)
            ok, reason = pit.valid(fact_id)
            status, current, _ = pit.key_check(fact_id)
            revoked = self.index.packet_revocation(packet_id, W, T)
            superseded_by = pit.superseded_by(fact_id)
            if superseded_by is None and status == "outdated":
                superseded_by = current
            current_info = {
                "state": view.version.state.value if view else None,
                "class": pit.admitted(fact_id),
                "checked_at": iso(view.checked_at) if view else None,
                "expires_at": iso(view.expires_at) if view else None,
                "scope": view.version.scope if view else None,
                "superseded_by": superseded_by,
                "valid_at_time": bool(ok and not revoked),
                "reason": "packet_revoked" if (ok and revoked) else reason,
            }
        as_packed = dict(entry)
        for k in ("effective_at", "published_at", "checked_at", "expires_at"):
            as_packed[k] = iso(as_packed.get(k))
        return {
            "packet_id": packet_id,
            "ref": ref,
            "fact_id": entry["fact_id"],
            "snapshot_id": W,
            "at": iso(T),
            "as_packed": as_packed,
            "current": current_info,
            "packet_revoked": revoked,
        }

    def session_status(self, packet_id: str, at: TimeLike = None) -> dict[str, Any]:
        """Whether the first-turn packet must now be treated as historical."""
        record = self.store.get_packet(packet_id)
        if record is None:
            return {"packet_id": packet_id, "historical": True, "reason": "unknown_packet"}
        T = parse_time(at) if at is not None else self.now()
        with self._lock:
            W = self._snapshot("live", T)
            revoked = self.index.packet_revocation(packet_id, W, T)
            pit = PointInTime(self.index, W, T)
            invalid = [r for r, e in sorted(record["refs"].items()) if not pit.valid(e["fact_id"])[0]]
        if revoked:
            return {"packet_id": packet_id, "historical": True, "reason": "revoked", "detail": revoked}
        if T >= record["expires_min"]:
            return {"packet_id": packet_id, "historical": True, "reason": "expired"}
        if invalid:
            return {"packet_id": packet_id, "historical": True, "reason": "fact_no_longer_valid", "refs": invalid}
        return {"packet_id": packet_id, "historical": False, "reason": None, "expires_at": iso(record["expires_min"])}

    # -- lookup ----------------------------------------------------------------------------

    def lookup(
        self,
        *,
        key: str | None = None,
        entity: str | None = None,
        predicate: str | None = None,
        scope: str | None = None,
        as_of: TimeLike = None,
        mode: str = "live",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Exact-key or entity lookup against the index (the freshness tool)."""
        subject = None
        if key is not None:
            try:
                k = key_from_str(key)
            except ValueError as exc:
                return {"error": str(exc), "rows": [], "covered": False}
            entity, predicate, scope = k[0], k[1], k[2]
            if key.count("|") == 3:
                subject = k[3]
        if not entity:
            return {"error": "entity or key is required", "rows": [], "covered": False}
        if predicate is not None and predicate not in PREDICATES:
            return {"error": f"unknown predicate {predicate!r}", "rows": [], "covered": False}
        T = parse_time(as_of) if as_of is not None else self.now()
        with self._lock:
            W = self._snapshot(mode, T)
            pit = PointInTime(self.index, W, T)
            rows = []
            for fid in sorted(self.index.by_entity.get(entity, ())):
                view = pit.view(fid)
                if view is None:
                    continue
                v = view.version
                if predicate is not None and v.predicate != predicate:
                    continue
                if scope is not None and v.scope != scope:
                    continue
                if subject is not None and v.subject != subject:
                    continue
                ok, _ = pit.valid(fid)
                if not ok:
                    continue
                cls = pit.admitted(fid)
                try:
                    text = render_body(v, cls)
                except RenderError:
                    continue
                rows.append(
                    {
                        "fact_id": fid,
                        "key": key_to_str(v.key),
                        "class": cls,
                        "text": text,
                        "entity": v.entity,
                        "predicate": v.predicate,
                        "scope": v.scope,
                        "subject": v.subject,
                        "value": v.value,
                        "qualifiers": {k2: q for k2, q in v.qualifiers.items() if k2 != "target_fact_id"},
                        "effective_at": iso(v.effective_at),
                        "published_at": iso(v.published_at),
                        "checked_at": iso(view.checked_at),
                        "expires_at": iso(view.expires_at),
                        "canonical_url": v.evidence.canonical_url,
                        "_order": (-v.change_time, fid),
                    }
                )
            rows.sort(key=lambda r: r["_order"])
            for r in rows:
                r.pop("_order")
            covering = [
                s for s in self.config.sources.values()
                if entity in s.entities and (predicate is None or predicate in s.fact_types)
            ]
            statuses = [self.index.source_status(s.id, W, T) for s in covering]
        if not covering:
            source_status = "unenrolled"
        elif any(st[0] == "ok" for st in statuses):
            source_status = "ok"
        else:
            source_status = "paused"
        last_checked = max((st[2] for st in statuses if st[2] is not None), default=None)
        coverage = min((s.coverage_start for s in covering if s.coverage_start is not None), default=None)
        return {
            "snapshot_id": W,
            "as_of": iso(T),
            "rows": rows[:limit],
            "covered": bool(rows) and source_status == "ok",
            "source_status": source_status,
            "last_checked_at": iso(last_checked),
            "coverage_start": iso(coverage),
        }
