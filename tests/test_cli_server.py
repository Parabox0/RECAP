import json
import threading
import urllib.request

import pytest

from recap import Recap, load_config
from recap.bench import build_synthetic, run_benchmark, synthetic_config
from recap.cli import main
from recap.server import serve
from recap.store import Store
from recap.timeutil import parse_time


def run(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr().out
    return code, out


def setup_example(tmp_path, capsys):
    d = tmp_path / "cfg"
    assert run(capsys, "init", str(d))[0] == 0
    cfg = str(d / "recap.toml")
    assert run(capsys, "check-config", "-c", cfg)[0] == 0
    for sid, f in [("lumen-releases", "lumen-releases.atom"), ("quill-pypi", "quill-pypi.json"),
                   ("aurora-status", "aurora-status.json"), ("lumen-policy", "lumen-policy.html")]:
        code, out = run(capsys, "ingest-file", "-c", cfg, "--now", "2026-11-18T09:00:00Z", sid, str(d / "fixtures" / f))
        assert code == 0 and json.loads(out)["status"] == "ingested"
    return d, cfg


def test_cli_end_to_end(tmp_path, capsys):
    d, cfg = setup_example(tmp_path, capsys)
    code, out = run(capsys, "assemble", "-c", cfg, "--now", "2026-11-18T10:00:00Z", "--profile", "software", "--model", "example-model")
    res = json.loads(out)
    assert code == 0 and res["status"] == "hit"
    code, out = run(capsys, "resolve", "-c", cfg, "--now", "2026-11-18T10:00:00Z", res["packet_id"], "f1")
    assert json.loads(out)["current"]["valid_at_time"] is True
    code, out = run(capsys, "review", "list", "-c", cfg)
    reviews = json.loads(out)
    hl = next(r for r in reviews if r["kind"] == "highlights")
    code, out = run(capsys, "review", "approve", str(hl["id"]), "-c", cfg, "--now", "2026-11-18T09:30:00Z",
                    "--text", "native Gaussian-splat import")
    assert code == 0
    code, out = run(capsys, "lookup", "-c", cfg, "--now", "2026-11-18T10:00:00Z", "--entity", "example:lumen")
    assert any("highlights (reviewed)" in r["text"] for r in json.loads(out)["rows"])
    code, out = run(capsys, "source", "status", "-c", cfg, "--now", "2026-11-18T10:00:00Z")
    assert json.loads(out)["quill-pypi"]["status"] == "ok"
    code, out = run(capsys, "economics", "--packet-tokens", "240", "--turns", "10", "--cached-rate", "0.1",
                    "--search-tokens", "1500", "--search-fee", "500")
    assert json.loads(out)["break_even_rate"] == pytest.approx(0.1361, abs=1e-4)
    code, out = run(capsys, "holdout-size", "--p-d", "0.05")
    assert json.loads(out)["holdout_traces"] == 980
    assert main(["assemble", "-c", str(tmp_path / "missing.toml"), "--profile", "x", "--model", "y"]) == 2


def test_cli_replay_fixtures(tmp_path, capsys):
    d, cfg = setup_example(tmp_path, capsys)
    fixtures = tmp_path / "fx.jsonl"
    fixtures.write_text("\n".join(json.dumps(x) for x in [
        {"type": "packet", "id": "p", "as_of": "2026-11-18T10:00:00Z", "profile_id": "geo", "profile_version": 1,
         "model_id": "example-model", "expect": {"status": "hit", "includes_keys": ["example:aurora-1|status|global|imager"]}},
        {"type": "citation", "id": "late", "packet": "p", "ref": "f1", "at": "2026-11-21T10:00:00Z", "expect_valid": False},
    ]))
    code, out = run(capsys, "replay", "-c", cfg, "--fixtures", str(fixtures))
    report = json.loads(out)
    assert code == 0 and report["passed"], report


def test_http_server(tmp_path, capsys):
    d, cfg = setup_example(tmp_path, capsys)
    now = parse_time("2026-11-18T10:00:00Z")
    rc = Recap(load_config(cfg), clock=lambda: now)
    httpd = serve(rc, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(path, payload):
        req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=10) as resp:
            return json.loads(resp.read())

    try:
        res = post("/v1/assemble", {"profile_id": "software", "profile_version": 1, "model_id": "example-model",
                                    "context_window": 200000, "base_input_tokens": 2000, "reserved_output_tokens": 4096})
        assert res["status"] == "hit"
        assert post("/v1/resolve", {"packet_id": res["packet_id"], "ref": "f1"})["current"]["valid_at_time"]
        assert post("/v1/lookup", {"entity": "example:quill"})["covered"]
        assert post("/v1/review_draft", {"packet_id": res["packet_id"], "draft": "See [f1].", "suppression_allowed": True})["action"] == "release"
        with opener.open(base + "/v1/health", timeout=10) as resp:
            assert json.loads(resp.read())["ok"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            post("/v1/assemble", {"bogus": 1})
        assert exc.value.code == 400
    finally:
        httpd.shutdown()


def test_latency_guard(tmp_path):
    cfg = synthetic_config(str(tmp_path / "b.db"))
    store = Store(cfg.db_path)
    now = parse_time("2026-11-18T12:00:00Z")
    build_synthetic(store, cfg, n_facts=10_000, now=now)
    rc = Recap(cfg, store, clock=lambda: now)
    out = run_benchmark(rc, runs=60, now=now)
    assert out["statuses"] == {"hit": 60}
    # The spec target is p95 <= 20 ms; the test allows headroom for slow CI machines.
    assert out["cold"]["p95_ms"] < 40
