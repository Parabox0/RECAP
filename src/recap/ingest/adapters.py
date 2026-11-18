"""Source adapters: turn a fetched document into typed facts.

Deterministic adapters promote only validated typed fields. Free text (release
notes, page bodies) becomes a :class:`ReviewCandidate` for a person to read;
nothing an adapter extracts from free text reaches a packet directly.

Writing an adapter: subclass :class:`Adapter`, implement :meth:`parse`, and
register it with :func:`register_adapter`. ``parse`` must be pure: no network
access and no side effects.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .. import sanitize
from ..config import SourceConfig, normalize_origin
from ..model import ParsedFact, State, make_fact_id, release_subject
from ..timeutil import parse_time
from .feeds import FeedError, parse_feed


class ParseError(ValueError):
    """The document did not have the expected structure (parser drift)."""


@dataclass
class ReviewCandidate:
    entry_id: str
    target_fact_id: str | None
    text: str
    kind: str = "highlights"


@dataclass
class ParseResult:
    facts: list[ParsedFact] = field(default_factory=list)
    reviews: list[ReviewCandidate] = field(default_factory=list)
    entries_seen: int = 0
    matched: int = 0
    rejected: int = 0
    complete_listing: bool = False
    oldest_entry_time: int | None = None
    newest_entry_time: int | None = None
    sequence_first: int | None = None
    sequence_last: int | None = None
    next_page_url: str | None = None

    def note_time(self, ts: int | None) -> None:
        if ts is None:
            return
        self.oldest_entry_time = ts if self.oldest_entry_time is None else min(self.oldest_entry_time, ts)
        self.newest_entry_time = ts if self.newest_entry_time is None else max(self.newest_entry_time, ts)

    def merge(self, other: "ParseResult") -> "ParseResult":
        seen = {(f.entry_id, f.predicate, f.scope, f.subject) for f in self.facts}
        for f in other.facts:
            if (f.entry_id, f.predicate, f.scope, f.subject) not in seen:
                self.facts.append(f)
        self.reviews.extend(other.reviews)
        self.entries_seen += other.entries_seen
        self.matched += other.matched
        self.rejected += other.rejected
        self.note_time(other.oldest_entry_time)
        self.note_time(other.newest_entry_time)
        self.next_page_url = other.next_page_url
        return self


class Adapter:
    name = "abstract"
    adapter_class = "deterministic"

    def parse(self, source: SourceConfig, body: bytes, *, content_type: str | None, url: str) -> ParseResult:  # pragma: no cover
        raise NotImplementedError


ADAPTERS: dict[str, Adapter] = {}


def register_adapter(adapter: Adapter) -> None:
    ADAPTERS[adapter.name] = adapter


def _evidence_url(source: SourceConfig, link: str) -> str:
    """Use the entry's own link only when it lies inside the source's permitted origins."""
    if link and len(link) <= 500:
        try:
            if normalize_origin(link) in source.permitted_origins:
                return link
        except ValueError:
            pass
    return source.url


class AtomReleaseAdapter(Adapter):
    """Release events from an Atom or RSS feed whose entry titles carry a version.

    Options: ``entity``, ``entity_label``, ``title_pattern`` (a regex with a
    ``version`` group and an optional ``channel`` group), ``default_channel``,
    ``channel_map``, ``scope``, ``highlights`` (``"review"`` stages entry text
    for review; ``"ignore"`` drops it).
    """

    name = "atom"

    def parse(self, source: SourceConfig, body: bytes, *, content_type: str | None, url: str) -> ParseResult:
        opts = source.options
        try:
            entity, label, pattern = opts["entity"], opts["entity_label"], re.compile(opts["title_pattern"])
        except (KeyError, re.error) as exc:
            raise ParseError(f"adapter options incomplete: {exc}") from None
        default_channel = opts.get("default_channel", "stable")
        channel_map: Mapping[str, str] = opts.get("channel_map", {})
        scope = opts.get("scope", "global")
        highlights = opts.get("highlights", "ignore")
        try:
            doc = parse_feed(body)
        except FeedError as exc:
            raise ParseError(str(exc)) from None
        out = ParseResult(complete_listing=doc.complete)
        out.next_page_url = doc.links.get("prev-archive") or doc.links.get("next")
        for e in doc.entries:
            out.entries_seen += 1
            published = e.published or e.updated
            out.note_time(published)
            m = pattern.fullmatch(e.title.strip())
            if not m:
                continue
            out.matched += 1
            try:
                if not e.id:
                    raise ValueError("entry without id")
                version = sanitize.version(m.group("version"))
                raw_channel = m.groupdict().get("channel")
                channel = sanitize.token(channel_map.get(raw_channel, raw_channel) if raw_channel else default_channel, "channel")
                if published is None:
                    raise ValueError("entry without a date")
            except ValueError:
                out.rejected += 1
                continue
            entry_id = sanitize.plain_text(e.id, 300)
            subject = release_subject(version, channel)
            out.facts.append(
                ParsedFact(
                    entry_id=entry_id,
                    entity=entity,
                    entity_label=label,
                    predicate="release",
                    scope=scope,
                    subject=subject,
                    value={"version": version},
                    qualifiers={"channel": channel},
                    event_at=published,
                    effective_at=published,
                    published_at=published,
                    source_revision=e.updated,  # None when the feed has no revision field
                    evidence_url=_evidence_url(source, e.link),
                )
            )
            if highlights == "review" and e.summary:
                out.reviews.append(
                    ReviewCandidate(
                        entry_id=entry_id,
                        target_fact_id=make_fact_id(source.id, entry_id, "release", scope, subject),
                        text=sanitize.plain_text(e.summary, 2000),
                    )
                )
        return out


_PRERELEASE = re.compile(r"(?i)(?:\d(?:a|b|c|rc|alpha|beta|pre|preview)\d*)|(?:dev\d*$)")


class PyPIAdapter(Adapter):
    """Releases and yanks from the PyPI JSON API (``/pypi/<name>/json``).

    A release is stable unless its version is a PEP 440 pre- or dev-release.
    A release whose files are all yanked is written as retracted.
    Options: ``entity``, ``entity_label``, ``project_url`` (optional template
    with ``{version}`` for evidence links).
    """

    name = "pypi"

    def parse(self, source: SourceConfig, body: bytes, *, content_type: str | None, url: str) -> ParseResult:
        opts = source.options
        try:
            entity, label = opts["entity"], opts["entity_label"]
        except KeyError as exc:
            raise ParseError(f"adapter options incomplete: {exc}") from None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ParseError(f"invalid JSON: {exc}") from None
        if not isinstance(data, dict) or not isinstance(data.get("releases"), dict):
            raise ParseError("missing 'releases' object")
        serial = data.get("last_serial")
        revision = int(serial) if isinstance(serial, int) and not isinstance(serial, bool) else None
        template = opts.get("project_url")
        out = ParseResult(complete_listing=True)
        for raw_version, files in sorted(data["releases"].items()):
            out.entries_seen += 1
            if not isinstance(files, list) or not files:
                continue
            out.matched += 1
            try:
                version = sanitize.version(raw_version)
                times = []
                for f in files:
                    if not isinstance(f, dict):
                        raise ValueError("bad file record")
                    t = parse_time(f.get("upload_time_iso_8601") or f.get("upload_time"))
                    if t is None:
                        raise ValueError("file without upload time")
                    times.append(t)
                published = min(times)
            except (ValueError, TypeError):
                out.rejected += 1
                continue
            out.note_time(published)
            yanked = all(bool(f.get("yanked")) for f in files)
            channel = "prerelease" if _PRERELEASE.search(version) else "stable"
            evidence = template.format(version=version) if isinstance(template, str) else ""
            out.facts.append(
                ParsedFact(
                    entry_id=version,
                    entity=entity,
                    entity_label=label,
                    predicate="release",
                    subject=release_subject(version, channel),
                    value={"version": version},
                    qualifiers={"channel": channel},
                    event_at=published,
                    effective_at=published,
                    published_at=published,
                    state=State.RETRACTED if yanked else State.ACTIVE,
                    source_revision=revision,
                    evidence_url=_evidence_url(source, evidence),
                )
            )
        return out


def _dig(data: Any, path: str) -> Any:
    cur = data
    for part in path.split(".") if path else []:
        if isinstance(cur, Mapping):
            cur = cur.get(part)
        else:
            return None
    return cur


class JsonStatusAdapter(Adapter):
    """Official status updates from a structured JSON feed.

    Options: ``items_path``; ``id_field``; ``status_field`` with a
    ``status_map`` allowlist (raw value to display text; unknown values are
    rejected); either ``entity`` + ``entity_label`` or ``entity_field`` +
    ``entity_map`` (raw value to ``{entity, label}``); optional
    ``component_field``; ``effective_field``; ``published_field``;
    ``updated_field`` (revision); optional ``valid_to_field``; optional
    ``state_field`` + ``state_map`` (raw to ``active``/``scheduled``); optional
    ``scope_field`` + ``scope_map``; optional ``sequence_field`` (top level,
    for gap detection); optional ``link_field``; ``complete`` (bool).
    """

    name = "json_status"

    def parse(self, source: SourceConfig, body: bytes, *, content_type: str | None, url: str) -> ParseResult:
        o = source.options
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ParseError(f"invalid JSON: {exc}") from None
        items = _dig(data, o.get("items_path", "items"))
        if not isinstance(items, list):
            raise ParseError("items_path does not point to a list")
        status_map: Mapping[str, str] = o.get("status_map") or {}
        if not status_map:
            raise ParseError("status_map is required")
        out = ParseResult(complete_listing=bool(o.get("complete", False)))
        seq_field = o.get("sequence_field")
        if seq_field:
            seq = _dig(data, seq_field)
            if isinstance(seq, int) and not isinstance(seq, bool):
                out.sequence_first = out.sequence_last = seq
        for item in items:
            out.entries_seen += 1
            if not isinstance(item, Mapping):
                out.rejected += 1
                continue
            out.matched += 1
            try:
                pf = self._fact(source, o, status_map, item)
            except (ValueError, TypeError, KeyError):
                out.rejected += 1
                continue
            out.note_time(pf.published_at)
            out.facts.append(pf)
        return out

    @staticmethod
    def _fact(source: SourceConfig, o: Mapping[str, Any], status_map: Mapping[str, str], item: Mapping[str, Any]) -> ParsedFact:
        raw_id = item.get(o.get("id_field", "id"))
        if raw_id is None:
            raise ValueError("item without id")
        entry_id = sanitize.inline(str(raw_id), 120, "id")
        raw_status = item.get(o.get("status_field", "status"))
        if raw_status not in status_map:
            raise ValueError(f"status {raw_status!r} not in status_map")
        status = sanitize.inline(status_map[raw_status], 80, "status")
        if "entity_field" in o:
            mapped = (o.get("entity_map") or {}).get(item.get(o["entity_field"]))
            if not isinstance(mapped, Mapping):
                raise ValueError("entity not in entity_map")
            entity, label = mapped["entity"], mapped["label"]
        else:
            entity, label = o["entity"], o["entity_label"]
        component = None
        if o.get("component_field"):
            raw_component = item.get(o["component_field"])
            if raw_component is not None:
                cmap = o.get("component_map")
                component = cmap.get(raw_component) if cmap else raw_component
                if component is None:
                    raise ValueError("component not in component_map")
                component = sanitize.inline(str(component), 80, "component")
        effective = parse_time(item.get(o.get("effective_field", "effective")))
        published = parse_time(item.get(o.get("published_field", "published")))
        updated = parse_time(item.get(o["updated_field"])) if o.get("updated_field") else None
        valid_to = parse_time(item.get(o["valid_to_field"])) if o.get("valid_to_field") else None
        if effective is None or published is None:
            raise ValueError("item needs effective and published times")
        state = State.ACTIVE
        if o.get("state_field"):
            raw_state = item.get(o["state_field"])
            mapped_state = (o.get("state_map") or {"active": "active", "scheduled": "scheduled"}).get(raw_state)
            if mapped_state not in ("active", "scheduled"):
                raise ValueError("unknown item state")
            state = State(mapped_state)
        scope = "global"
        if o.get("scope_field"):
            raw_scope = item.get(o["scope_field"])
            scope = (o.get("scope_map") or {}).get(raw_scope, raw_scope) if raw_scope is not None else "global"
            scope = sanitize.token(str(scope), "scope")
        value = {"status": status}
        if component:
            value["component"] = component
        link = item.get(o["link_field"]) if o.get("link_field") else ""
        return ParsedFact(
            entry_id=entry_id,
            entity=entity,
            entity_label=label,
            predicate="status",
            scope=scope,
            subject=component or "",
            value=value,
            qualifiers={},
            event_at=effective,
            effective_at=effective,
            valid_to=valid_to,
            published_at=published,
            state=state,
            source_revision=updated,
            evidence_url=_evidence_url(source, link if isinstance(link, str) else ""),
        )


class ReviewPageAdapter(Adapter):
    """A page with no usable structure. Its text is staged for review whenever
    it changes; reviewers write typed facts by hand."""

    name = "review_page"
    adapter_class = "review"

    def parse(self, source: SourceConfig, body: bytes, *, content_type: str | None, url: str) -> ParseResult:
        text = sanitize.plain_text(body.decode("utf-8", errors="replace"), 8000)
        out = ParseResult(entries_seen=1, matched=1)
        if text:
            out.reviews.append(ReviewCandidate(entry_id="page", target_fact_id=None, text=text, kind="page"))
        return out


for _adapter in (AtomReleaseAdapter(), PyPIAdapter(), JsonStatusAdapter(), ReviewPageAdapter()):
    register_adapter(_adapter)
