import json
import math

import pytest
from helpers import DAY, T0, config, profile, put_fact, source

from recap import Recap
from recap.admin import AdminError, approve_review, editorial, recall
from recap.evaluation import break_even, clopper_pearson_upper, evaluate_gates, holdout_size, run_fixtures
from recap.evaluation.stats import cluster_bootstrap, design_effect
from recap.model import State
from recap.store import Store


# -- admin -------------------------------------------------------------------------------


def test_recall_source_pauses_and_tombstones(tmp_path):
    c = config(tmp_path)
    store = Store(c.db_path)
    a = put_fact(store, now=T0, version="1.0.0")
    b = put_fact(store, now=T0, version="1.1.0")
    out = recall(store, now=T0 + 10, reason="compromised", source_id="src")
    assert out["tombstoned"] == 2
    assert store.latest_version(a.fact_id).state is State.RETRACTED
    assert store.latest_version(b.fact_id).state is State.RETRACTED
    assert store.latest_source_event("src") == ("paused", "recall: compromised")
    with pytest.raises(AdminError):
        recall(store, now=T0, reason="", fact_ids=[a.fact_id])


def test_editorial_requires_editorial_profile(tmp_path):
    c = config(tmp_path, profiles=[profile(), {"id": "general", "version": 1, "kind": "editorial"}])
    store = Store(c.db_path)
    v = put_fact(store, now=T0)
    editorial(store, c, "general", v.fact_id, action="add", now=T0)
    with pytest.raises(AdminError):
        editorial(store, c, "software", v.fact_id, action="add", now=T0)
    with pytest.raises(AdminError):
        editorial(store, c, "general", "fx_missing", action="add", now=T0)


def test_approve_untargeted_fact_is_validated(tmp_path):
    src = source("rv", adapter="review_page", **{"class": "review"}, fact_types=["deprecation"], entities=["example:lumen"])
    c = config(tmp_path, sources=[src])
    store = Store(c.db_path)
    with store.batch(T0) as b:
        b.add_review("rv", "page", None, "page", "The old API is deprecated.", "h1")
    rid = store.list_reviews()[0]["id"]
    with pytest.raises(AdminError, match="not allowed"):
        approve_review(store, c, rid, decided_by="r", now=T0, fact={"predicate": "removal", "entity": "example:lumen", "effective_at": "2026-11-01"})
    with pytest.raises(AdminError, match="render"):
        approve_review(store, c, rid, decided_by="r", now=T0, fact={"predicate": "deprecation", "entity": "example:lumen", "effective_at": "2026-11-01", "value": {}})
    v = approve_review(store, c, rid, decided_by="r", now=T0, fact={
        "predicate": "deprecation", "entity": "example:lumen", "entity_label": "Lumen", "subject": "old-api",
        "value": {"item": "old API"}, "effective_at": "2026-11-01", "published_at": "2026-11-01"})
    assert v.origin == "reviewed" and v.predicate == "deprecation"
    assert store.get_review(rid)["status"] == "approved"
    with pytest.raises(AdminError, match="not pending"):
        approve_review(store, c, rid, decided_by="r", now=T0, fact={})


# -- statistics, gates, economics -------------------------------------------------------------


def test_clopper_pearson_matches_spec_numbers():
    assert clopper_pearson_upper(0, 600) == pytest.approx(0.00498, abs=2e-5)
    assert clopper_pearson_upper(1, 600) == pytest.approx(0.00788, abs=2e-5)
    assert clopper_pearson_upper(2, 600) == pytest.approx(0.01046, abs=2e-5)
    assert clopper_pearson_upper(0, 300) == pytest.approx(0.00994, abs=2e-5)


def test_holdout_size_rule():
    assert holdout_size(0.02) == 600
    assert holdout_size(0.05) == 980
    assert holdout_size(0.05, 1.5) == 1470


def test_break_even_example():
    b = break_even(packet_tokens=240, turns=10, cached_rate=0.1, search_tokens=1500, search_fee=500)
    assert b.packet_cost == pytest.approx(456)
    assert b.search_cost == pytest.approx(3350)
    assert b.break_even_rate == pytest.approx(0.1361, abs=1e-4)


def test_cluster_bootstrap_and_design_effect():
    records = [{"c": i // 10, "x": (i // 10) % 2} for i in range(100)]
    mean = lambda rs: sum(r["x"] for r in rs) / len(rs)  # noqa: E731
    iv = cluster_bootstrap(records, mean, lambda r: r["c"], n_boot=500)
    assert iv.lower < 0.5 < iv.upper
    assert design_effect(records, mean, lambda r: r["c"], n_boot=300) > 2


def graded(n, *, c_correct=1.0, unsupported=0, c_lookups=0):
    out = []
    for i in range(n):
        out.append({
            "trace_id": f"t{i}", "cluster": f"e{i // 3}", "stratum": "s",
            "a": {"correct": True, "avoidable_lookups": 1},
            "c": {"correct": i < c_correct * n, "avoidable_lookups": c_lookups,
                  "unsupported_current_claim": i < unsupported, "first_turn_search": False, "added_latency_ms": 4.0},
        })
    return out


def test_gates_pass_and_fail():
    zero = {"expired_rows_injected": 0}
    good = evaluate_gates(graded(600, unsupported=1), hard_failures=zero, economics_net_savings=10, n_boot=300)
    assert good.passed, good.to_dict()
    bad = evaluate_gates(graded(600, unsupported=2), hard_failures=zero, economics_net_savings=10, n_boot=300)
    assert not bad.passed
    assert [g.name for g in bad.gates if not g.passed] == ["unsupported_current_claims"]
    worse = evaluate_gates(graded(600, c_correct=0.9), hard_failures=zero, economics_net_savings=10, n_boot=300)
    assert "correctness_vs_A" in [g.name for g in worse.gates if not g.passed]
    missing = evaluate_gates(graded(30), n_boot=100)
    assert {g.name for g in missing.gates if not g.passed} >= {"hard_failures", "economics"}
    assert not evaluate_gates([]).passed


# -- deterministic fixtures ---------------------------------------------------------------------


def test_fixture_runner(tmp_path):
    c = config(tmp_path)
    store = Store(c.db_path)
    v = put_fact(store, now=T0 - DAY, version="3.0.0")
    recall(store, now=T0 + 3600, reason="test", fact_ids=[v.fact_id])
    fixtures = [
        {"type": "packet", "id": "p", "as_of": T0, "profile_id": "software", "profile_version": 1, "model_id": "m",
         "expect": {"status": "hit", "includes": [v.fact_id]}},
        {"type": "citation", "id": "c-before", "packet": "p", "ref": "f1", "at": T0 + 60, "expect_valid": True},
        {"type": "citation", "id": "c-after", "packet": "p", "ref": "f1", "at": T0 + 7200, "expect_valid": False},
        {"type": "lookup", "id": "l", "entity": "example:lumen", "as_of": T0 + 7200, "expect": {"covered": False}},
    ]
    report = run_fixtures(Recap(c, store), fixtures)
    assert report.passed, json.dumps(report.to_dict(), indent=1)
    assert all(v == 0 for v in report.hard_failures.values())
