import shutil
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

import recap.cli
from recap import AssembleRequest, Recap, load_config
from recap.admin import pause_source, recall, resume_source
from recap.citations import extract_refs, review_draft
from recap.config import RecapConfig
from recap.ingest import Poller
from recap.placement import POLICY_INSTRUCTION, prepend_to_first_user_message
from recap.store import Store
from recap.timeutil import parse_time

EXAMPLE = Path(recap.cli.__file__).resolve().parent / "_example"
INGEST_AT = parse_time("2026-11-18T09:00:00Z")
T = parse_time("2026-11-18T10:00:00Z")


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def example(tmp_path, *, sources=("lumen-releases", "quill-pypi", "aurora-status", "lumen-policy")):
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    cfg = load_config(root / "recap.toml")
    cfg.settings = replace(cfg.settings, deadline_ms=5000)  # independent of machine load
    store = Store(cfg.db_path)
    clock = Clock(INGEST_AT)
    poller = Poller(store, cfg, clock=clock)
    files = {
        "lumen-releases": "lumen-releases.atom",
        "quill-pypi": "quill-pypi.json",
        "aurora-status": "aurora-status.json",
        "lumen-policy": "lumen-policy.html",
    }
    for sid in sources:
        poller.ingest_bytes(sid, (root / "fixtures" / files[sid]).read_bytes())
    clock.t = T
    return cfg, store, Recap(cfg, store, clock=clock), clock, poller, root


def req(**kw):
    base = dict(profile_id="software", profile_version=1, model_id="example-model", context_window=200_000,
                base_input_tokens=2_000, reserved_output_tokens=4_096)
    base.update(kw)
    return AssembleRequest(**base)


def test_hit_packet_respects_all_caps(tmp_path):
    cfg, store, rc, clock, _, _ = example(tmp_path)
    res = rc.assemble(req())
    assert res.status == "hit" and res.suppression_allowed
    assert res.rendered_data.startswith('<recap as_of="2026-11-18">\n[f1] ')
    assert res.added_input_tokens_max <= res.effective_budget <= 192
    assert len(res.fact_ids) <= 8
    entities = Counter(rc.index.version_at(f, res.snapshot_id, T).entity for f in res.fact_ids)
    assert max(entities.values()) <= 2
    assert extract_refs(res.rendered_data) == list(res.refs)
    # Same inputs, fresh process: same packet.
    again = Recap(cfg, Store(cfg.db_path), clock=clock).assemble(req())
    assert again.packet_id == res.packet_id and again.rendered_data == res.rendered_data


def test_geo_packet_has_status_and_announcement(tmp_path):
    _, _, rc, _, _, _ = example(tmp_path)
    res = rc.assemble(req(profile_id="geo"))
    assert "Aurora-1 imager status: safe mode since 2026-11-12." in res.rendered_data
    assert "Aurora-1 radiometer status scheduled: degraded from 2026-11-25." in res.rendered_data
    assert "nominal" not in res.rendered_data  # the May status is older than the window
    assert res.cache_until <= parse_time("2026-11-18T11:00:00Z")


def test_degraded_when_a_profile_source_is_paused(tmp_path):
    _, store, rc, clock, _, _ = example(tmp_path)
    pause_source(store, "quill-pypi", now=T, reason="manual")
    rc.refresh(force=True)
    res = rc.assemble(req())
    assert res.status == "degraded" and not res.suppression_allowed
    # Rows from a paused source age out on their normal schedule; they are not dropped at once.
    assert "Quill" in res.rendered_data
    clock.t = T + 8 * 86_400
    rc.refresh(force=True)
    later = rc.assemble(req(as_of=clock.t))
    assert later.rendered_data is None or "Quill" not in later.rendered_data


def test_degraded_when_source_never_checked(tmp_path):
    _, _, rc, _, _, _ = example(tmp_path, sources=("lumen-releases", "quill-pypi", "aurora-status"))
    assert rc.assemble(req()).status == "degraded"


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"model_id": "nope"}, "unsupported_model"),
        ({"serializer_version": "v2"}, "model_mismatch"),
        ({"profile_id": "nope"}, "unknown_profile"),
        ({"budget": 300}, "invalid_request"),
        ({"base_input_tokens": 199_000}, "low_budget"),
        ({"budget": 40}, "low_budget"),
        ({"mode": "bogus"}, "invalid_request"),
        ({"as_of": "not a time"}, "invalid_request"),
    ],
)
def test_empty_reasons(tmp_path, changes, reason):
    _, _, rc, _, _, _ = example(tmp_path)
    res = rc.assemble(req(**changes))
    assert res.status == "empty" and res.reason == reason and res.rendered_data is None


def test_cold_index_and_empty_profile(tmp_path):
    cfg = RecapConfig.build(
        str(tmp_path / "x.db"),
        sources=[],
        profiles=[{"id": "p", "version": 1}],
        models=[{"id": "m", "counting": "byte_bound"}],
    )
    rc = Recap(cfg)
    assert rc.assemble(req(profile_id="p", model_id="m")).reason == "cold_index"


def test_timeout_returns_empty(tmp_path):
    cfg, store, _, clock, _, _ = example(tmp_path)
    from dataclasses import replace

    cfg.settings = replace(cfg.settings, deadline_ms=0.0)
    rc = Recap(cfg, store, clock=clock)
    res = rc.assemble(req())
    assert res.status == "empty" and res.reason == "timeout"
    assert rc.assemble(req(mode="replay", as_of=T)).status == "hit"  # replay has no deadline


def test_cache_and_recall(tmp_path):
    _, store, rc, clock, _, _ = example(tmp_path)
    first = rc.assemble(req())
    second = rc.assemble(req())
    assert second.cache_hit and second.packet_id == first.packet_id
    recalled = first.fact_ids[0]
    out = recall(store, now=T + 60, reason="bad data", fact_ids=[recalled])
    assert out["packets_revoked"] >= 1
    clock.t = T + 120
    rc.refresh(force=True)
    third = rc.assemble(req(as_of=T + 120))
    assert not third.cache_hit and recalled not in third.fact_ids
    r = rc.resolve(first.packet_id, "f1", at=T + 120)
    assert r["current"]["valid_at_time"] is False and r["current"]["reason"] == "retracted"
    assert r["packet_revoked"].startswith("recall")
    assert rc.session_status(first.packet_id, at=T + 120)["reason"] == "revoked"


def test_resolve_and_session_expiry(tmp_path):
    _, _, rc, _, _, _ = example(tmp_path, sources=("aurora-status",))
    res = rc.assemble(req(profile_id="geo"))
    ref = next(r for r, f in res.refs.items() if "imager" in res.rendered_data.splitlines()[int(r[1:])])
    r = rc.resolve(res.packet_id, ref, at=T)
    assert r["current"]["valid_at_time"] and r["as_packed"]["canonical_url"].startswith("https://status.example.org/")
    assert rc.session_status(res.packet_id, at=T)["historical"] is False
    # Status rows recheck within 48 h; without a new check the packet becomes historical.
    later = T + 49 * 3600
    assert rc.session_status(res.packet_id, at=later)["historical"] is True
    assert rc.resolve(res.packet_id, ref, at=later)["current"]["reason"] == "expired"
    assert rc.resolve(res.packet_id, "f9")["error"] == "unknown_ref"
    assert rc.resolve("pk_missing", "f1")["error"] == "unknown_packet"


def test_replay_pins_what_was_known(tmp_path):
    cfg, store, rc, clock, poller, root = example(tmp_path, sources=())
    feed = (root / "fixtures" / "lumen-releases.atom").read_text()
    start = feed.index("  <entry>")
    end = feed.index("  </entry>", start) + len("  </entry>\n")
    older = feed[:start] + feed[end:]  # the same feed before 5.3.0 shipped
    clock.t = parse_time("2026-11-08T09:00:00Z")
    poller.ingest_bytes("lumen-releases", older.encode())
    clock.t = INGEST_AT
    poller.ingest_bytes("lumen-releases", feed.encode())
    clock.t = T
    rc.refresh(force=True)
    live = rc.assemble(req())
    past = rc.assemble(req(mode="replay", as_of="2026-11-09T12:00:00Z"))
    assert "Lumen 5.3.0 (stable)" in live.rendered_data
    assert "Lumen 5.3.0 (stable)" not in past.rendered_data
    assert past.snapshot_id < live.snapshot_id


def test_lookup_coverage(tmp_path):
    _, store, rc, _, _, _ = example(tmp_path)
    out = rc.lookup(entity="example:quill", predicate="release")
    assert out["covered"] and out["source_status"] == "ok"
    texts = [r["text"] for r in out["rows"]]
    assert texts[0] == "Quill 2.2.0rc1 (prerelease) released 2026-11-01."
    assert not any("2.1.2" in t for t in texts)  # yanked
    assert rc.lookup(entity="example:unknown")["source_status"] == "unenrolled"
    assert rc.lookup(key="example:quill|release|global|2.1.0@stable")["rows"][0]["subject"] == "2.1.0@stable"
    assert rc.lookup(key="example:quill|release|global")["rows"]
    pause_source(store, "quill-pypi", now=T, reason="manual")
    rc.refresh(force=True)
    paused = rc.lookup(entity="example:quill")
    assert paused["source_status"] == "paused" and paused["covered"] is False
    resume_source(store, "quill-pypi", now=T)


def test_exact_counter_recount_guard(tmp_path):
    cfg, store, _, clock, _, _ = example(tmp_path)
    from recap.config import ModelEntry

    cfg.models["exact"] = ModelEntry.from_dict({"id": "exact", "counting": "exact", "knowledge_through": "2026-06-30"})
    rc = Recap(cfg, store, clock=clock)
    assert rc.assemble(req(model_id="exact")).reason == "unsupported_model"
    rc.register_tokenizer("exact", lambda s: len(s.encode()) // 3 + (5 if s.count("\n") > 3 else 0))
    res = rc.assemble(req(model_id="exact"))
    assert res.status in ("hit", "degraded")
    assert res.added_input_tokens_max <= res.effective_budget


def test_metrics_hook_has_no_text(tmp_path):
    cfg, store, _, clock, _, _ = example(tmp_path)
    events = []
    rc = Recap(cfg, store, clock=clock, metrics=events.append)
    rc.assemble(req())
    assert events and "rendered_data" not in events[0] and events[0]["status"] == "hit"
    assert set(events[0]) >= {"fact_ids", "added_input_tokens_max", "elapsed_ms"}


def test_prompt_binding_and_placement(tmp_path):
    cfg, _, rc, _, _, _ = example(tmp_path)
    assert cfg.profile_for_prompt("software-assistant@1").id == "software"
    assert cfg.profile_for_prompt("unbound@9").id == "general"
    res = rc.assemble(req())
    msgs = [{"role": "system", "content": POLICY_INSTRUCTION}, {"role": "user", "content": "What shipped in Lumen 5.3?"}]
    placed = prepend_to_first_user_message(msgs, res)
    assert placed[1]["content"][0]["text"] == res.rendered_data
    assert placed[1]["content"][1]["text"] == "What shipped in Lumen 5.3?"
    assert msgs[1]["content"] == "What shipped in Lumen 5.3?"  # input untouched


def test_review_draft_flow(tmp_path):
    _, store, rc, clock, _, _ = example(tmp_path)
    res = rc.assemble(req())
    ok = review_draft(rc, packet_id=res.packet_id, draft="Yes, see [f1].", suppression_allowed=True)
    assert ok.action == "release" and ok.reason == "citations_valid"
    assert review_draft(rc, packet_id=res.packet_id, draft="No citations here.", suppression_allowed=True).action == "release"
    assert review_draft(rc, packet_id=res.packet_id, draft="See [f1].", suppression_allowed=False).reason == "suppression_not_allowed"
    forged = review_draft(rc, packet_id=res.packet_id, draft="See [f7].", suppression_allowed=True)
    assert forged.action == "lookup" and forged.details["f7"] == "unknown_ref"
    recall(store, now=T + 10, reason="bad", fact_ids=[res.refs["f1"]])
    clock.t = T + 20
    rc.refresh(force=True)
    bad = review_draft(rc, packet_id=res.packet_id, draft="See [f1].", suppression_allowed=True)
    assert bad.action == "lookup" and bad.reason == "invalid_citations"
