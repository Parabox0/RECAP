"""Polling schedule: one conditional GET per interval with jitter and backoff."""

from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..config import RecapConfig, SourceConfig
from ..store import Store
from ..timeutil import DAY
from . import writer
from .adapters import ADAPTERS, Adapter, ParseError, ParseResult
from .fetch import FetchError, FetchResult, OriginPolicy, SafeFetcher

MAX_BACKOFF_SECONDS = 6 * 3600


class Transport(Protocol):
    def get(
        self,
        url: str,
        policy: OriginPolicy,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int = 2_000_000,
    ) -> FetchResult: ...


@dataclass
class PollOutcome:
    source_id: str
    status: str
    detail: str = ""
    stats: writer.IngestStats | None = None
    next_poll_at: int | None = None
    pages: int = 0
    extra: dict = field(default_factory=dict)


class Poller:
    def __init__(
        self,
        store: Store,
        config: RecapConfig,
        *,
        transport: Transport | None = None,
        adapters: dict[str, Adapter] | None = None,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ):
        self.store = store
        self.config = config
        self.transport = transport or SafeFetcher()
        self.adapters = adapters or ADAPTERS
        self.clock = clock
        self.rng = rng or random.Random()
        for src in config.sources.values():
            adapter = self.adapters.get(src.adapter)
            if adapter is None:
                raise ValueError(f"source {src.id}: unknown adapter {src.adapter!r}")
            if adapter.adapter_class != src.adapter_class:
                raise ValueError(
                    f"source {src.id}: adapter {src.adapter!r} is {adapter.adapter_class}, source says {src.adapter_class}"
                )

    def _now(self) -> int:
        return int(self.clock())

    def _schedule(self, source: SourceConfig, base_seconds: float, now: int) -> int:
        jitter = self.rng.uniform(-0.1, 0.1)
        nxt = now + int(base_seconds * (1 + jitter))
        rt = self.store.get_runtime(source.id)
        rt.next_poll_at = nxt
        self.store.set_runtime(rt)
        return nxt

    def _backoff(self, source: SourceConfig, now: int, retry_after: int | None = None) -> int:
        rt = self.store.get_runtime(source.id)
        base = source.poll_interval_minutes * 60 * (2 ** min(max(rt.failures - 1, 0), 8))
        base = min(base, MAX_BACKOFF_SECONDS)
        if retry_after is not None:
            base = max(base, retry_after)
        return self._schedule(source, base, now)

    def due(self, now: int | None = None) -> list[str]:
        now = self._now() if now is None else now
        out = []
        for sid in sorted(self.config.sources):
            rt = self.store.get_runtime(sid)
            if rt.next_poll_at is None or rt.next_poll_at <= now:
                out.append(sid)
        return out

    def run_due(self) -> list[PollOutcome]:
        return [self.poll(sid) for sid in self.due()]

    def run(self, stop: threading.Event, tick_seconds: float = 5.0) -> None:  # pragma: no cover - loop
        while not stop.is_set():
            self.run_due()
            stop.wait(tick_seconds)

    def _blocked(self, source: SourceConfig) -> str | None:
        event = self.store.latest_source_event(source.id)
        if event and event[0] == "paused" and not (event[1] or "").startswith("outage"):
            return event[1] or "paused"
        return None

    def poll(self, source_id: str) -> PollOutcome:
        source = self.config.sources[source_id]
        now = self._now()
        blocked = self._blocked(source)
        if blocked:
            nxt = self._schedule(source, source.poll_interval_minutes * 60, now)
            return PollOutcome(source_id, "skipped_paused", blocked, next_poll_at=nxt)
        rt = self.store.get_runtime(source_id)
        policy = OriginPolicy.of(source.permitted_origins, source.permitted_paths)
        try:
            res = self.transport.get(
                source.url, policy, etag=rt.etag, last_modified=rt.last_modified, max_bytes=source.max_bytes
            )
        except FetchError as exc:
            writer.record_failure(
                self.store, source, now=now, kind=exc.kind, http_status=None,
                outage_failures=self.config.settings.outage_failures,
            )
            return PollOutcome(source_id, "error", str(exc), next_poll_at=self._backoff(source, now))
        if res.status == 304:
            writer.record_not_modified(self.store, source, now=now)
            nxt = self._schedule(source, source.poll_interval_minutes * 60, now)
            return PollOutcome(source_id, "not_modified", next_poll_at=nxt)
        if res.status != 200:
            writer.record_failure(
                self.store, source, now=now, kind=f"http_{res.status}", http_status=res.status,
                outage_failures=self.config.settings.outage_failures,
            )
            return PollOutcome(source_id, "error", f"HTTP {res.status}", next_poll_at=self._backoff(source, now, res.retry_after))
        return self._ingest_response(source, res, now, policy)

    def _parse(self, source: SourceConfig, res: FetchResult) -> ParseResult:
        return self.adapters[source.adapter].parse(source, res.body, content_type=res.content_type, url=res.final_url)

    def _ingest_response(self, source: SourceConfig, res: FetchResult, now: int, policy: OriginPolicy, *, backfill: bool = False) -> PollOutcome:
        try:
            result = self._parse(source, res)
        except ParseError as exc:
            writer.record_parse_error(self.store, source, now=now, message=str(exc))
            nxt = self._schedule(source, source.poll_interval_minutes * 60, now)
            return PollOutcome(source.id, "paused", f"parser_drift: {exc}", next_poll_at=nxt)
        rt = self.store.get_runtime(source.id)
        seen_before = rt.newest_seen
        pages = 1
        bodies = [res.body]
        # Recover a gap by following archive pages (RFC 5005) when the feed offers them.
        target = (now - self.config.settings.max_lookback_days * DAY) if backfill else rt.newest_seen
        while (
            result.next_page_url
            and pages < source.max_backfill_pages
            and target is not None
            and (result.oldest_entry_time is None or result.oldest_entry_time > target)
        ):
            try:
                page = self.transport.get(result.next_page_url, policy, max_bytes=source.max_bytes)
            except FetchError:
                break
            if page.status != 200:
                break
            try:
                page_result = self._parse(source, page)
            except ParseError:
                break
            bodies.append(page.body)
            result.merge(page_result)
            pages += 1
        body_sha = hashlib.sha256(b"".join(bodies)).hexdigest()
        stats = writer.ingest(self.store, source, result, now=now, body_sha=body_sha, check_gap=not backfill)
        rt = self.store.get_runtime(source.id)
        rt.etag, rt.last_modified = res.etag, res.last_modified
        if backfill and result.oldest_entry_time is not None:
            rt.oldest_backfilled = result.oldest_entry_time
        self.store.set_runtime(rt)
        nxt = self._schedule(source, source.poll_interval_minutes * 60, now)
        status = "paused" if stats.paused else "ingested"
        recovered = (
            result.complete_listing
            or seen_before is None
            or (result.oldest_entry_time is not None and result.oldest_entry_time <= seen_before)
        )
        return PollOutcome(source.id, status, stats.paused or "", stats=stats, next_poll_at=nxt, pages=pages,
                           extra={"gap_closed": recovered})

    def backfill(self, source_id: str, *, resume: bool = True) -> PollOutcome:
        """Fetch history back to the lookback limit, following archive pages.

        A successful backfill also resumes a source paused for a gap.
        """
        source = self.config.sources[source_id]
        now = self._now()
        policy = OriginPolicy.of(source.permitted_origins, source.permitted_paths)
        try:
            res = self.transport.get(source.url, policy, max_bytes=source.max_bytes)
        except FetchError as exc:
            return PollOutcome(source_id, "error", str(exc))
        if res.status != 200:
            return PollOutcome(source_id, "error", f"HTTP {res.status}")
        event = self.store.latest_source_event(source_id)
        outcome = self._ingest_response(source, res, now, policy, backfill=True)
        gap_paused = bool(event and event[0] == "paused" and (event[1] or "").startswith("gap"))
        if resume and gap_paused and outcome.status == "ingested":
            if outcome.extra.get("gap_closed"):
                with self.store.batch(now) as b:
                    b.add_source_event(source_id, "ok", "backfilled")
                outcome.extra["resumed"] = True
            else:
                outcome.extra["resumed"] = False
                outcome.detail = "backfill did not reach the last entry seen; source stays paused"
        return outcome

    def ingest_bytes(self, source_id: str, body: bytes, *, content_type: str | None = None, check_gap: bool = True) -> PollOutcome:
        """Run a source's adapter over a local document (fixtures, offline backfill)."""
        source = self.config.sources[source_id]
        now = self._now()
        res = FetchResult(status=200, body=body, final_url=source.url, content_type=content_type)
        try:
            result = self._parse(source, res)
        except ParseError as exc:
            writer.record_parse_error(self.store, source, now=now, message=str(exc))
            return PollOutcome(source_id, "paused", f"parser_drift: {exc}")
        stats = writer.ingest(
            self.store, source, result, now=now, body_sha=hashlib.sha256(body).hexdigest(), check_gap=check_gap
        )
        return PollOutcome(source_id, "paused" if stats.paused else "ingested", stats.paused or "", stats=stats)
