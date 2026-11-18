import pytest
from helpers import DAY, HOUR, T0, MODEL, config, profile, put_fact, source

from recap.config import ModelEntry, ProfileManifest
from recap.index import Index
from recap.model import State
from recap.ranking import PointInTime, Ranker, freshness, utility, window_start
from recap.store import Store
from recap.timeutil import parse_time


def ranked(c, store, prof_id="software", model_id="m", T=T0):
    idx = Index(c.sources)
    idx.apply(store.load_since(0))
    ranker = Ranker(idx, c.settings)
    cands, stats = ranker.candidates(c.profile(prof_id), c.models[model_id], idx.watermark, T)
    return cands, stats


def subjects(cands):
    return [c.version.subject for c in cands]


def test_utility_formula_matches_spec_example():
    F = freshness(T0 - 8 * DAY + 4 * HOUR, T0, 168)
    assert utility(1.0, 0.7, 1.0, F) == pytest.approx(0.40 + 0.175 + 0.20 + 0.15 * F)
    assert freshness(T0, T0, 168) == 1.0
    assert freshness(T0 - 168 * HOUR, T0, 168) == pytest.approx(0.5)


def test_window_is_anchored_to_knowledge_cutoff(tmp_path):
    settings = config(tmp_path).settings
    m = ModelEntry.from_dict(MODEL)
    assert window_start(T0, m, settings) == max(T0 - 180 * DAY, m.knowledge_through - 30 * DAY)
    unknown = ModelEntry.from_dict({"id": "u", "counting": "byte_bound"})
    assert window_start(T0, unknown, settings) == T0 - 30 * DAY


def test_old_deprecation_unknown_to_model_is_eligible(tmp_path):
    c = config(tmp_path, models=[MODEL, {"id": "nocut", "counting": "byte_bound"}])
    store = Store(c.db_path)
    put_fact(store, now=T0 - HOUR, predicate="deprecation", subject="api-v1", value={"item": "API v1"},
             published=parse_time("2026-08-01"), effective=parse_time("2026-08-01"))
    cands, _ = ranked(c, store)
    assert "api-v1" in subjects(cands)
    cands, _ = ranked(c, store, model_id="nocut")
    assert "api-v1" not in subjects(cands)


def test_filters_run_before_truncation(tmp_path):
    c = config(tmp_path, profiles=[profile(entities=[], scope=["US"])])
    store = Store(c.db_path)
    for i in range(35):
        put_fact(store, now=T0 - HOUR, entity=f"example:e{i}", version=f"1.{i}.0", scope="EU", published=T0 - HOUR - i * 60)
    put_fact(store, now=T0 - HOUR, entity="example:us", version="9.0.0", scope="US", published=T0 - 5 * DAY)
    cands, stats = ranked(c, store)
    assert subjects(cands) == ["9.0.0@stable"]
    assert stats.dropped["out_of_scope"] == 35


def test_dominance_and_prerelease(tmp_path):
    c = config(tmp_path)
    store = Store(c.db_path)
    put_fact(store, now=T0, version="5.2.0", published=T0 - 30 * DAY)
    put_fact(store, now=T0, version="5.3.0", published=T0 - 8 * DAY)
    put_fact(store, now=T0, version="5.3.0", channel="beta", published=T0 - 20 * DAY)
    cands, stats = ranked(c, store)
    assert subjects(cands) == ["5.3.0@stable"]
    assert stats.dropped["dominated"] == 2
    put_fact(store, now=T0, version="6.0.0", channel="beta", published=T0 - DAY)
    cands, _ = ranked(c, store)
    assert set(subjects(cands)) == {"5.3.0@stable", "6.0.0@beta"}


def test_event_dispute_between_sources(tmp_path):
    c = config(tmp_path, sources=[source("a"), source("b")])
    store = Store(c.db_path)
    put_fact(store, now=T0, source_id="a", version="5.3.0", published=T0 - 2 * DAY)
    put_fact(store, now=T0, source_id="b", version="5.3.0", published=T0 - 3 * DAY)
    cands, stats = ranked(c, store)
    assert cands == []
    assert stats.dropped["disputed"] >= 1


def test_agreeing_sources_become_supporting_citations(tmp_path):
    c = config(tmp_path, sources=[source("a"), source("b", authority="corroborated")])
    store = Store(c.db_path)
    a = put_fact(store, now=T0, source_id="a", version="5.3.0", published=T0 - 2 * DAY)
    b = put_fact(store, now=T0, source_id="b", version="5.3.0", published=T0 - 2 * DAY, authority="corroborated")
    cands, _ = ranked(c, store)
    assert len(cands) == 1
    assert cands[0].fact_id == a.fact_id
    assert cands[0].supporting == (b.fact_id,)


def test_state_dispute_requires_later_conflicting_publication(tmp_path):
    c = config(tmp_path, sources=[source("a"), source("b")])
    store = Store(c.db_path)
    put_fact(store, now=T0, source_id="a", predicate="status", subject="imager",
             value={"status": "safe mode", "component": "imager"}, effective=T0 - 2 * HOUR, published=T0 - HOUR)
    stale = put_fact(store, now=T0, source_id="b", predicate="status", subject="imager",
                     value={"status": "nominal", "component": "imager"}, effective=T0 - 5 * HOUR, published=T0 - 4 * HOUR)
    cands, _ = ranked(c, store)
    assert [c_.version.value["status"] for c_ in cands] == ["safe mode"]
    # Source b now asserts "nominal" after the safe-mode change took effect: a conflict.
    put_fact(store, now=T0, source_id="b", predicate="status", subject="imager", entry_id="b2",
             value={"status": "nominal", "component": "imager"}, effective=T0 - 5 * HOUR, published=T0 - 30 * 60)
    cands, stats = ranked(c, store)
    assert cands == []
    assert stale.fact_id


def test_restatement_of_known_state_is_dropped(tmp_path):
    c = config(tmp_path)
    store = Store(c.db_path)
    put_fact(store, now=T0, predicate="status", subject="imager", entry_id="old",
             value={"status": "nominal", "component": "imager"}, effective=parse_time("2026-06-01"), published=parse_time("2026-06-01"))
    put_fact(store, now=T0, predicate="status", subject="imager", entry_id="new",
             value={"status": "nominal", "component": "imager"}, effective=T0 - DAY, published=T0 - DAY)
    cands, stats = ranked(c, store)
    assert cands == []
    assert stats.dropped.get("restatement") == 1
    put_fact(store, now=T0, predicate="status", subject="imager", entry_id="newer",
             value={"status": "safe mode", "component": "imager"}, effective=T0 - HOUR, published=T0 - HOUR)
    cands, _ = ranked(c, store)
    assert [x.version.value["status"] for x in cands] == ["safe mode"]


def test_threshold_and_deny(tmp_path):
    c = config(tmp_path, profiles=[profile(entities=[], topics={"software_releases": 0.2, "weather": 1.0}, deny=["live_weather"])])
    store = Store(c.db_path)
    put_fact(store, now=T0, version="1.0.0", published=T0 - 30 * DAY)
    put_fact(store, now=T0, version="2.0.0", entity="example:w", topics=("weather", "live_weather"))
    cands, stats = ranked(c, store)
    assert cands == []
    assert stats.dropped["denied_topic"] == 1
    # R = 0.2, I = 0.7, A = 1, F ~ 0.05: u ~ 0.46, below 0.55.
    assert stats.after_key_checks == 1 and stats.above_threshold == 0


def test_editorial_profile(tmp_path):
    c = config(tmp_path, profiles=[profile(), {"id": "general", "version": 1, "kind": "editorial"}])
    store = Store(c.db_path)
    a = put_fact(store, now=T0, version="1.0.0")
    b = put_fact(store, now=T0, version="2.0.0", entity="example:quill")
    d = put_fact(store, now=T0, version="3.0.0", entity="example:third")
    with store.batch(T0) as bt:
        for f in (a, b, d):
            bt.add_editorial("general", f.fact_id, "add")
    cands, _ = ranked(c, store, prof_id="general")
    assert [x.fact_id for x in cands] == [d.fact_id, b.fact_id]  # newest two designations
