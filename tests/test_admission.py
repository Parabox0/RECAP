from helpers import DAY, HOUR, T0, config, put_fact

from recap.index import ACTIVE, ANNOUNCEMENT, Index
from recap.model import State
from recap.ranking import PointInTime
from recap.store import Store


def setup(tmp_path, **cfg):
    c = config(tmp_path, **cfg)
    store = Store(c.db_path)
    return c, store


def build_index(c, store):
    idx = Index(c.sources)
    idx.apply(store.load_since(0))
    return idx


def admit(idx, fid, T, W=None):
    W = idx.watermark if W is None else W
    view = idx.view(fid, W, T)
    return Index.admission(view, T) if view else (None, "missing")


def test_active_announcement_and_lapsed_schedule(tmp_path):
    c, store = setup(tmp_path)
    active = put_fact(store, now=T0 - DAY, published=T0 - 2 * DAY)
    sched = put_fact(store, now=T0 - DAY, predicate="removal", subject="api", value={"item": "old API"},
                     state=State.SCHEDULED, published=T0 - 2 * DAY, effective=T0 + DAY)
    idx = build_index(c, store)
    assert admit(idx, active.fact_id, T0) == (ACTIVE, "ok")
    assert admit(idx, sched.fact_id, T0) == (ANNOUNCEMENT, "ok")
    # Once the effective date passes without a confirming row, the schedule lapses.
    assert admit(idx, sched.fact_id, T0 + 2 * DAY) == (None, "lapsed_schedule")


def test_recheck_expiry_and_attestation_extension(tmp_path):
    c, store = setup(tmp_path)
    v = put_fact(store, now=T0)
    idx = build_index(c, store)
    assert admit(idx, v.fact_id, T0 + 167 * HOUR)[0] == ACTIVE
    assert admit(idx, v.fact_id, T0 + 169 * HOUR) == (None, "expired")
    # A later attesting check extends the interval and so the verification window.
    with store.batch(T0 + 100 * HOUR) as b:
        prev = store.get_runtime("src").last_ok_check_seq
        seq = b.add_check("src", 304, None, True, "not_modified")
        rows = store._query("SELECT last_seq FROM attestations WHERE fact_id = ?", (v.fact_id,))
        b.extend_all_attestations("src", rows[0]["last_seq"], seq)
    idx = build_index(c, store)
    assert admit(idx, v.fact_id, T0 + 169 * HOUR)[0] == ACTIVE
    view = idx.view(v.fact_id, idx.watermark, T0 + 169 * HOUR)
    assert view.checked_at == T0 + 100 * HOUR


def test_status_rechecks_within_48_hours(tmp_path):
    c, store = setup(tmp_path)
    v = put_fact(store, now=T0, predicate="status", subject="imager", value={"status": "nominal", "component": "imager"})
    idx = build_index(c, store)
    assert admit(idx, v.fact_id, T0 + 47 * HOUR)[0] == ACTIVE
    assert admit(idx, v.fact_id, T0 + 49 * HOUR) == (None, "expired")


def test_explicit_expiry_valid_to_and_unpublished(tmp_path):
    c, store = setup(tmp_path)
    exp = put_fact(store, now=T0, explicit_expires=T0 + HOUR, version="1.0.1")
    vt = put_fact(store, now=T0, predicate="status", subject="x", value={"status": "maintenance"}, valid_to=T0 + 2 * HOUR)
    future = put_fact(store, now=T0, published=T0 + 3 * HOUR, version="1.0.2")
    idx = build_index(c, store)
    assert admit(idx, exp.fact_id, T0 + 2 * HOUR) == (None, "expired")
    assert admit(idx, vt.fact_id, T0 + 3 * HOUR) == (None, "validity_ended")
    assert admit(idx, future.fact_id, T0 + HOUR) == (None, "not_yet_published")


def test_replay_sees_only_what_was_known(tmp_path):
    c, store = setup(tmp_path)
    v = put_fact(store, now=T0)
    W1 = store.watermark()
    from recap.admin import recall

    recall(store, now=T0 + 5 * HOUR, reason="test", fact_ids=[v.fact_id])
    idx = build_index(c, store)
    # Before the recall was observed the fact was valid; afterwards it is retracted.
    assert admit(idx, v.fact_id, T0 + HOUR)[0] == ACTIVE
    assert admit(idx, v.fact_id, T0 + 6 * HOUR) == (None, "retracted")
    # Pinned to the earlier snapshot, even a later time does not see the recall.
    assert admit(idx, v.fact_id, T0 + 6 * HOUR, W=W1)[0] == ACTIVE
    assert store.snapshot_for_time(T0 + HOUR) == W1


def test_unenrolled_source_is_never_served(tmp_path):
    c, store = setup(tmp_path)
    v = put_fact(store, now=T0)
    c2 = config(tmp_path, sources=[{**__import__("helpers").source("other")}])
    idx = Index(c2.sources)
    idx.apply(store.load_since(0))
    assert idx.view(v.fact_id, idx.watermark, T0) is None


def test_supersedes_excludes_old_fact(tmp_path):
    c, store = setup(tmp_path)
    old = put_fact(store, now=T0 - DAY, version="2.0.0", published=T0 - 2 * DAY)
    new = put_fact(store, now=T0 - HOUR, version="2.0.0", entry_id="correction", supersedes=(old.fact_id,), published=T0 - 2 * HOUR,
                   subject="2.0.0@stable-corrected")
    idx = build_index(c, store)
    pit = PointInTime(idx, idx.watermark, T0)
    assert pit.admitted(new.fact_id) == ACTIVE
    assert pit.admitted(old.fact_id) is None
    assert pit.reason(old.fact_id) == "superseded"
