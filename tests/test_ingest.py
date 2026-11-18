import json

from helpers import DAY, HOUR, T0, config, source

from recap.admin import resume_source
from recap.index import Index
from recap.ingest import FetchError, FetchResult, Poller
from recap.ingest import writer
from recap.ingest.adapters import ParseResult
from recap.model import ParsedFact, State
from recap.store import Store
from recap.timeutil import iso

ATOM = "http://www.w3.org/2005/Atom"


def atom(entries, prev=None):
    links = f'<link rel="prev-archive" href="{prev}"/>' if prev else ""
    body = "".join(
        f"<entry><id>{eid}</id><title>{title}</title><published>{iso(pub)}</published>"
        f"<updated>{iso(upd if upd is not None else pub)}</updated><summary>{summary}</summary></entry>"
        for eid, title, pub, upd, summary in entries
    )
    return f'<?xml version="1.0"?><feed xmlns="{ATOM}">{links}{body}</feed>'.encode()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeTransport:
    def __init__(self):
        self.responses = {}
        self.calls = []

    def queue(self, url, *results):
        self.responses.setdefault(url, []).extend(results)

    def get(self, url, policy, *, etag=None, last_modified=None, max_bytes=2_000_000):
        self.calls.append((url, etag))
        if not policy.permits(url):
            raise FetchError("not_permitted", url)
        item = self.responses[url].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(body, etag=None):
    return FetchResult(status=200, body=body, etag=etag)


def env(tmp_path, **src_over):
    src = source("feed", url="https://feed.example.org/releases.atom", **src_over)
    c = config(tmp_path, sources=[src])
    store = Store(c.db_path)
    clock = Clock(T0)
    transport = FakeTransport()
    poller = Poller(store, c, transport=transport, clock=clock)
    return c, store, clock, transport, poller


def facts(store):
    return {v.subject: v for v in store.list_latest_versions(limit=100)}


def test_ingest_attest_and_304(tmp_path):
    c, store, clock, tr, poller = env(tmp_path)
    url = c.sources["feed"].url
    feed = atom([("e1", "Lumen 1.0.0", T0 - DAY, None, "First"), ("e2", "Lumen 1.1.0", T0 - HOUR, None, "")])
    tr.queue(url, ok(feed, etag='"v1"'))
    out = poller.poll("feed")
    assert out.status == "ingested" and out.stats.new_versions == 2 and out.stats.attested == 2
    assert 0.9 * 1800 <= out.next_poll_at - T0 <= 1.1 * 1800
    clock.t = T0 + 30 * 60
    tr.queue(url, FetchResult(status=304))
    assert poller.poll("feed").status == "not_modified"
    assert tr.calls[-1] == (url, '"v1"')
    idx = Index(c.sources)
    idx.apply(store.load_since(0))
    v = facts(store)["1.1.0@stable"]
    assert idx.view(v.fact_id, idx.watermark, T0 + 3600).checked_at == T0 + 30 * 60


def test_revision_ordering(tmp_path):
    c, store, clock, tr, poller = env(tmp_path)
    url = c.sources["feed"].url
    tr.queue(url, ok(atom([("e1", "Lumen 1.0.0", T0 - DAY, T0 - DAY, "")])))
    poller.poll("feed")
    # A newer revision of the same entry that changes the claim is a correction.
    clock.t += 60
    tr.queue(url, ok(atom([("e1", "Lumen 1.0.0", T0 - 2 * DAY, T0 - HOUR, "")])))
    assert poller.poll("feed").stats.new_versions == 1
    # A delivery with an older revision is a replay, whatever its content.
    clock.t += 60
    tr.queue(url, ok(atom([("e1", "Lumen 1.0.0", T0 - DAY, T0 - DAY, "")])))
    stats = poller.poll("feed").stats
    assert stats.replays_ignored == 1 and stats.new_versions == 0
    assert facts(store)["1.0.0@stable"].published_at == T0 - 2 * DAY


def test_revert_without_revision_goes_to_review(tmp_path):
    c, store, clock, tr, poller = env(tmp_path)
    src = c.sources["feed"]

    def result(day):
        pf = ParsedFact(entry_id="x", entity="example:lumen", entity_label="Lumen", predicate="release",
                        subject="1.0.0@stable", value={"version": "1.0.0"}, qualifiers={"channel": "stable"},
                        effective_at=T0 - day * DAY, published_at=T0 - day * DAY)
        return ParseResult(facts=[pf], entries_seen=1, matched=1)

    assert writer.ingest(store, src, result(1), now=T0, body_sha="a").new_versions == 1
    assert writer.ingest(store, src, result(2), now=T0 + 1, body_sha="b").new_versions == 1
    reverted = writer.ingest(store, src, result(1), now=T0 + 2, body_sha="c")
    assert reverted.reverts_to_review == 1 and reverted.new_versions == 0
    assert [r["kind"] for r in store.list_reviews()] == ["revert"]


def test_review_queue_dedupes_highlights(tmp_path):
    c, store, clock, tr, poller = env(tmp_path, options={**source("x")["options"], "highlights": "review"})
    url = c.sources["feed"].url
    feed = atom([("e1", "Lumen 1.0.0", T0 - DAY, None, "&lt;b&gt;Big&lt;/b&gt; things")])
    tr.queue(url, ok(feed), ok(feed))
    poller.poll("feed")
    clock.t += 60
    poller.poll("feed")
    reviews = store.list_reviews()
    assert len(reviews) == 1 and reviews[0]["text"] == "Big things"


def test_outage_pause_and_recovery(tmp_path):
    c, store, clock, tr, poller = env(tmp_path)
    url = c.sources["feed"].url
    tr.queue(url, FetchError("timeout"), FetchError("timeout"), FetchError("timeout"))
    for _ in range(3):
        out = poller.poll("feed")
        clock.t += 60
    assert out.status == "error"
    assert store.latest_source_event("feed")[0] == "paused"
    tr.queue(url, ok(atom([("e1", "Lumen 1.0.0", T0 - DAY, None, "")])))
    out = poller.poll("feed")
    assert out.status == "ingested" and out.stats.resumed
    assert store.latest_source_event("feed") == ("ok", "recovered")


def test_rate_limit_backoff(tmp_path):
    c, store, clock, tr, poller = env(tmp_path)
    tr.queue(c.sources["feed"].url, FetchResult(status=429, retry_after=7200))
    out = poller.poll("feed")
    assert out.next_poll_at - T0 >= 0.9 * 7200


def test_parser_drift_pauses_until_signoff(tmp_path):
    c, store, clock, tr, poller = env(tmp_path)
    url = c.sources["feed"].url
    tr.queue(url, ok(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><feed xmlns="http://www.w3.org/2005/Atom"/>'))
    assert poller.poll("feed").status == "paused"
    assert store.latest_source_event("feed")[1].startswith("parser_drift")
    assert poller.poll("feed").status == "skipped_paused"
    resume_source(store, "feed", now=clock.t)
    tr.queue(url, ok(atom([("e1", "Lumen 1.0.0", T0 - DAY, None, "")])))
    assert poller.poll("feed").status == "ingested"


def test_no_matching_titles_is_drift(tmp_path):
    c, store, clock, tr, poller = env(tmp_path)
    tr.queue(c.sources["feed"].url, ok(atom([("e1", "Totally new title format", T0 - DAY, None, "")])))
    out = poller.poll("feed")
    assert out.status == "paused" and "no entries matched" in out.detail


def test_gap_detection_and_archive_recovery(tmp_path):
    c, store, clock, tr, poller = env(tmp_path)
    url = c.sources["feed"].url
    tr.queue(url, ok(atom([("e1", "Lumen 1.0.0", T0 - 10 * DAY, None, "")])))
    poller.poll("feed")
    # Ten releases later the feed only shows the newest two: entries may have rolled off.
    clock.t += DAY
    tr.queue(url, ok(atom([("e9", "Lumen 1.9.0", T0 - DAY, None, ""), ("e8", "Lumen 1.8.0", T0 - 2 * DAY, None, "")])))
    out = poller.poll("feed")
    assert out.status == "paused" and out.detail.startswith("gap")
    resume_source(store, "feed", now=clock.t)
    # The same situation with an RFC 5005 archive link is recovered by paging.
    archive = "https://feed.example.org/archive-1.atom"
    clock.t += DAY
    tr.queue(url, ok(atom([("e10", "Lumen 1.10.0", T0, None, ""), ("e9", "Lumen 1.9.0", T0 - DAY, None, "")], prev=archive)))
    tr.queue(archive, ok(atom([("e2", "Lumen 1.2.0", T0 - 9 * DAY, None, ""), ("e1", "Lumen 1.0.0", T0 - 10 * DAY, None, "")])))
    out = poller.poll("feed")
    assert out.status == "ingested" and out.pages == 2


def test_pypi_yanks_and_serials(tmp_path):
    src = source("pypi", adapter="pypi", url="https://pypi.example.org/pypi/quill/json",
                 options={"entity": "example:quill", "entity_label": "Quill"})
    c = config(tmp_path, sources=[src])
    store = Store(c.db_path)
    clock = Clock(T0)
    poller = Poller(store, c, clock=clock)

    def doc(serial, yanked):
        return json.dumps({"last_serial": serial, "releases": {
            "1.0.0": [{"upload_time_iso_8601": iso(T0 - 9 * DAY), "yanked": False}],
            "1.1.0": [{"upload_time_iso_8601": iso(T0 - DAY), "yanked": yanked}],
            "1.2.0b1": [{"upload_time_iso_8601": iso(T0 - HOUR), "yanked": False}],
            "2.0.0": [],
        }}).encode()

    poller.ingest_bytes("pypi", doc(10, False))
    f = facts(store)
    assert f["1.2.0b1@prerelease"].qualifiers["channel"] == "prerelease"
    assert "2.0.0@stable" not in f
    clock.t += 60
    poller.ingest_bytes("pypi", doc(12, True))
    assert facts(store)["1.1.0@stable"].state is State.RETRACTED
    clock.t += 60
    stats = poller.ingest_bytes("pypi", doc(11, False)).stats  # stale mirror replay
    assert stats.replays_ignored >= 1
    assert facts(store)["1.1.0@stable"].state is State.RETRACTED


def test_json_status_mapping_and_rejections(tmp_path):
    opts = {"items_path": "items", "status_map": {"OK": "nominal", "SAFE": "safe mode"}, "entity": "example:sat",
            "entity_label": "Sat-1", "component_field": "unit", "state_field": "state",
            "state_map": {"now": "active", "planned": "scheduled"}}
    src = source("st", adapter="json_status", url="https://st.example.org/status.json", options=opts, fact_types=["status"])
    c = config(tmp_path, sources=[src])
    store = Store(c.db_path)
    poller = Poller(store, c, clock=Clock(T0))
    items = [
        {"id": "a", "unit": "imager", "status": "SAFE", "state": "now", "effective": iso(T0 - HOUR), "published": iso(T0 - HOUR)},
        {"id": "b", "unit": "imager", "status": "EXPLODED", "state": "now", "effective": iso(T0), "published": iso(T0)},
    ]
    out = poller.ingest_bytes("st", json.dumps({"items": items}).encode())
    assert out.stats.new_versions == 1
    v = next(iter(facts(store).values()))
    assert v.value == {"status": "safe mode", "component": "imager"} and v.subject == "imager"
    bad = [dict(items[1], id=f"x{i}") for i in range(4)]
    out = poller.ingest_bytes("st", json.dumps({"items": bad}).encode())
    assert out.status == "paused" and "failed validation" in out.detail


def test_adapter_class_must_match(tmp_path):
    import pytest

    src = source("rv", adapter="review_page", url="https://rv.example.org/p.html")
    c = config(tmp_path, sources=[src])
    with pytest.raises(ValueError, match="adapter"):
        Poller(Store(c.db_path), c)
