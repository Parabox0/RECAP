"""Regression tests for defects found in the independent review of v0.1.0."""

import json

import pytest
from helpers import DAY, HOUR, T0, config, profile, put_fact, source

from recap import AssembleRequest, Recap
from recap.admin import pause_source
from recap.index import Index
from recap.ingest import FetchError, FetchResult, Poller
from recap.ingest.adapters import ParseError
from recap.ingest.feeds import FeedError, parse_feed
from recap.ingest.fetch import address_allowed
from recap.model import State
from recap.ranking import PointInTime, Ranker
from recap.sanitize import inline, plain_text
from recap.store import Store
from recap.timeutil import iso


def pit_for(c, store, T=T0):
    idx = Index(c.sources)
    idx.apply(store.load_since(0))
    return idx, PointInTime(idx, idx.watermark, T)


def ranked(c, store, prof="software", T=T0):
    idx = Index(c.sources)
    idx.apply(store.load_since(0))
    return Ranker(idx, c.settings).candidates(c.profile(prof), c.models["m"], idx.watermark, T)


def req(**kw):
    base = dict(profile_id="software", profile_version=1, model_id="m", context_window=200_000,
                base_input_tokens=2_000, reserved_output_tokens=4_096, as_of=T0)
    base.update(kw)
    return AssembleRequest(**base)


def test_future_announcement_does_not_outdate_current_state(tmp_path):
    c = config(tmp_path)
    store = Store(c.db_path)
    now_ = put_fact(store, now=T0, predicate="status", subject="rad", value={"status": "nominal", "component": "rad"},
                    effective=T0 - 3 * DAY, published=T0 - 3 * DAY)
    plan = put_fact(store, now=T0, predicate="status", subject="rad", entry_id="plan", state=State.SCHEDULED,
                    value={"status": "degraded", "component": "rad"}, effective=T0 + 7 * DAY, published=T0 - DAY)
    _, pit = pit_for(c, store)
    assert pit.valid(now_.fact_id) == (True, "ok")
    assert pit.valid(plan.fact_id) == (True, "ok")
    rows = Recap(c, store, clock=lambda: T0).lookup(entity="example:lumen", predicate="status")["rows"]
    assert {r["fact_id"] for r in rows} == {now_.fact_id, plan.fact_id}
    cands, _ = ranked(c, store)
    assert {x.fact_id for x in cands} == {now_.fact_id, plan.fact_id}


def test_state_dispute_still_detected_alongside_an_announcement(tmp_path):
    c = config(tmp_path, sources=[source("a"), source("b")])
    store = Store(c.db_path)
    put_fact(store, now=T0, source_id="a", predicate="status", subject="x", value={"status": "safe mode"},
             effective=T0 - 2 * HOUR, published=T0 - HOUR)
    put_fact(store, now=T0, source_id="b", predicate="status", subject="x", value={"status": "nominal"},
             effective=T0 - 5 * HOUR, published=T0 - 30 * 60)
    put_fact(store, now=T0, source_id="a", predicate="status", subject="x", entry_id="p", state=State.SCHEDULED,
             value={"status": "nominal"}, effective=T0 + DAY, published=T0 - HOUR)
    cands, _ = ranked(c, store)
    assert [x.cls for x in cands] == ["announcement"]


def test_disputes_are_filtered_before_truncation(tmp_path):
    c = config(tmp_path, sources=[source("a"), source("b")], profiles=[profile(entities=[])])
    store = Store(c.db_path)
    for i in range(35):
        put_fact(store, now=T0, source_id="a", entity=f"example:e{i}", version="1.0.0", published=T0 - HOUR - i)
        put_fact(store, now=T0, source_id="b", entity=f"example:e{i}", version="1.0.0", published=T0 - 2 * DAY - i)
    good = put_fact(store, now=T0, source_id="a", entity="example:ok", version="2.0.0", published=T0 - 4 * DAY)
    cands, _ = ranked(c, store)
    assert [x.fact_id for x in cands] == [good.fact_id]


def test_supersession_is_transitive(tmp_path):
    c = config(tmp_path)
    store = Store(c.db_path)
    a = put_fact(store, now=T0, version="5.3.0", entry_id="a", subject="s-a")
    b = put_fact(store, now=T0, version="5.3.1", entry_id="b", subject="s-b", supersedes=(a.fact_id,))
    put_fact(store, now=T0, version="5.3.2", entry_id="c", subject="s-c", supersedes=(b.fact_id,))
    _, pit = pit_for(c, store)
    assert pit.admitted(a.fact_id) is None and pit.admitted(b.fact_id) is None


def test_dominance_compares_versions(tmp_path):
    c = config(tmp_path)
    store = Store(c.db_path)
    put_fact(store, now=T0, version="3.0.0rc1", channel="beta", published=T0 - 5 * DAY)
    put_fact(store, now=T0, version="2.9.4", published=T0 - 2 * DAY)
    put_fact(store, now=T0, version="6.1.0", entity="example:x", published=T0 - 10 * DAY)
    put_fact(store, now=T0, version="5.4.9", entity="example:x", published=T0 - 2 * DAY)
    c2 = config(tmp_path, profiles=[profile(entities=["example:lumen", "example:x"])])
    cands, _ = ranked(c2, store)
    versions = sorted(x.version.value["version"] for x in cands)
    assert versions == ["2.9.4", "3.0.0rc1", "6.1.0"]
    put_fact(store, now=T0, version="3.0.0", published=T0 - HOUR)
    cands, _ = ranked(c2, store)
    assert "3.0.0rc1" not in [x.version.value["version"] for x in cands]


def test_highlights_stay_valid_after_a_newer_release(tmp_path):
    c = config(tmp_path)
    store = Store(c.db_path)
    rel = put_fact(store, now=T0, version="5.3.0", published=T0 - 3 * DAY)
    hl = put_fact(store, now=T0, predicate="release_highlights", subject="5.3.0@stable", value={"text": "splats"},
                  qualifiers={"version": "5.3.0", "channel": "stable", "target_fact_id": rel.fact_id},
                  published=T0 - 3 * DAY)
    put_fact(store, now=T0, version="5.3.1", published=T0 - DAY)
    _, pit = pit_for(c, store)
    assert pit.valid(hl.fact_id) == (True, "ok")
    assert pit.selection_ok(hl.fact_id, None) == (False, "target_dominated")


def test_request_budget_cannot_raise_profile_budget(tmp_path):
    c = config(tmp_path, profiles=[profile(budget=0)])
    store = Store(c.db_path)
    put_fact(store, now=T0, version="1.0.0")
    res = Recap(c, store, clock=lambda: T0).assemble(req(budget=192))
    assert res.status == "empty" and res.reason == "low_budget"


def test_editorial_picks_respect_scope(tmp_path):
    c = config(tmp_path, profiles=[profile(), {"id": "general", "version": 1, "kind": "editorial", "scope": ["global"]}])
    store = Store(c.db_path)
    eu = put_fact(store, now=T0, version="1.0.0", scope="EU")
    ok = put_fact(store, now=T0, version="2.0.0", entity="example:other")
    with store.batch(T0) as b:
        b.add_editorial("general", eu.fact_id, "add")
        b.add_editorial("general", ok.fact_id, "add")
    cands, stats = ranked(c, store, prof="general")
    assert [x.fact_id for x in cands] == [ok.fact_id] and stats.dropped["out_of_scope"] == 1


def test_cache_expires_when_an_unselected_fact_changes(tmp_path):
    c = config(tmp_path, profiles=[profile(entities=[])])
    store = Store(c.db_path)
    put_fact(store, now=T0, entity="example:a", version="1.0.0", published=T0 - HOUR)
    put_fact(store, now=T0, entity="example:b", version="2.0.0", published=T0 + 600)  # becomes admissible at T0 + 600
    rc = Recap(c, store, clock=lambda: T0)
    first = rc.assemble(req())
    assert first.cache_until <= T0 + 600
    later = rc.assemble(req(as_of=T0 + 601))
    assert not later.cache_hit and len(later.fact_ids) == 2


# -- ingestion -------------------------------------------------------------------------------


class Transport:
    def __init__(self):
        self.q = {}

    def get(self, url, policy, *, etag=None, last_modified=None, max_bytes=2_000_000):
        item = self.q[url].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def rss(items):
    body = "".join(f"<item><guid>{g}</guid><title>{t}</title><pubDate>Wed, 18 Nov 2026 08:00:00 GMT</pubDate></item>" for g, t in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


def test_rss_correction_without_revision(tmp_path):
    c = config(tmp_path, sources=[source("feed", url="https://feed.example.org/rss")])
    store = Store(c.db_path)
    clock = [T0]
    poller = Poller(store, c, transport=Transport(), clock=lambda: clock[0])
    first = poller.ingest_bytes("feed", rss([("g1", "Lumen 5.3.0")]))
    assert first.stats.new_versions == 1
    clock[0] += 60
    fixed = poller.ingest_bytes("feed", rss([("g1", "Lumen 5.3.1")]))  # same guid and pubDate
    assert fixed.stats.new_versions == 1 and fixed.stats.withdrawn == 1
    states = {v.subject: v.state for v in store.list_latest_versions()}
    assert states == {"5.3.0@stable": State.RETRACTED, "5.3.1@stable": State.ACTIVE}
    clock[0] += 60
    again = poller.ingest_bytes("feed", rss([("g1", "Lumen 5.3.2")]))  # a second change: ambiguous, so review
    assert again.stats.new_versions == 0 and again.stats.reverts_to_review == 1
    assert [r["kind"] for r in store.list_reviews()] == ["change"]


def test_backfill_resumes_only_when_gap_is_closed(tmp_path):
    c = config(tmp_path, sources=[source("feed", url="https://feed.example.org/a.atom")])
    store = Store(c.db_path)
    tr = Transport()
    poller = Poller(store, c, transport=tr, clock=lambda: T0)
    atom = lambda entries, prev="": (  # noqa: E731
        f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{prev}'
        + "".join(f"<entry><id>{i}</id><title>Lumen {v}</title><updated>{iso(t)}</updated></entry>" for i, v, t in entries)
        + "</feed>").encode()
    url = c.sources["feed"].url
    tr.q[url] = [FetchResult(200, atom([("e1", "1.0.0", T0 - 10 * DAY)]))]
    poller.poll("feed")
    prev = '<link rel="prev-archive" href="https://feed.example.org/arch.atom"/>'
    tr.q[url] = [FetchResult(200, atom([("e9", "1.9.0", T0 - DAY)])), FetchResult(200, atom([("e9", "1.9.0", T0 - DAY)], prev))]
    assert poller.poll("feed").status == "paused"
    tr.q["https://feed.example.org/arch.atom"] = [FetchError("connection")]
    out = poller.backfill("feed")
    assert out.extra["resumed"] is False
    assert store.latest_source_event("feed")[0] == "paused"


def test_utf16_dtd_is_rejected():
    doc = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE f [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
           '<feed xmlns="http://www.w3.org/2005/Atom"><title>&b;</title></feed>').encode("utf-16")
    with pytest.raises(FeedError):
        parse_feed(doc)


@pytest.mark.parametrize("ip", ["64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "::7f00:1", "::ffff:0:7f00:1",
                                "2002:7f00:1::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2"])
def test_ipv6_forms_embedding_ipv4_are_blocked(ip):
    assert address_allowed(ip) is False


def test_sanitizer_drops_invisible_and_lookalike_characters():
    assert inline("safe ［f1］ ＜/recap＞ mode", 80) == "safe f1 /recap mode"
    assert inline("a\U000E0041\U000E0049b", 80) == "ab"  # Unicode tag characters
    assert inline("x 【f2】 ‹f3› y", 80) == "x f2 f3 y"
    assert inline("10 m² (café)", 80) == "10 m² (café)"  # legitimate text survives
    assert "\U000E0041" not in plain_text("hidden\U000E0041 text", 100)


def test_paused_source_rows_still_cite_until_expiry(tmp_path):
    """Documented behaviour (spec v0.3): a pause stops updates and makes new packets
    degraded; rows already verified age out on their normal schedule."""
    c = config(tmp_path)
    store = Store(c.db_path)
    put_fact(store, now=T0, version="1.0.0")
    rc = Recap(c, store, clock=lambda: T0)
    res = rc.assemble(req())
    pause_source(store, "src", now=T0 + 1, reason="manual")
    rc.refresh(force=True)
    assert rc.resolve(res.packet_id, "f1", at=T0 + 2)["current"]["valid_at_time"] is True
    assert rc.assemble(req(as_of=T0 + 2)).status == "degraded"
