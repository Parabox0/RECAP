"""Command-line interface: ``recap <command> --help`` for details."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__, admin
from .config import ConfigError, load_config
from .evaluation import break_even, evaluate_gates, holdout_size, run_fixtures
from .runtime import AssembleRequest, Recap
from .store import Store
from .timeutil import iso, parse_time

EXAMPLE_DIR = Path(__file__).resolve().parent / "_example"


def _out(data: Any, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
    else:
        print(data)


def _clock(args: argparse.Namespace):
    if getattr(args, "now", None):
        fixed = parse_time(args.now)
        return lambda: fixed
    return time.time


def _load(args: argparse.Namespace):
    cfg = load_config(args.config)
    store = Store(cfg.db_path)
    return cfg, store


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.directory)
    if target.exists() and any(target.iterdir()):
        print(f"{target} is not empty", file=sys.stderr)
        return 1
    shutil.copytree(EXAMPLE_DIR, target, dirs_exist_ok=True)
    print(f"wrote example configuration to {target}")
    print(f"next: recap check-config -c {target / 'recap.toml'}")
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    from .ingest.adapters import ADAPTERS

    cfg = load_config(args.config)
    problems = []
    for src in cfg.sources.values():
        adapter = ADAPTERS.get(src.adapter)
        if adapter is None:
            problems.append(f"source {src.id}: unknown adapter {src.adapter!r}")
        elif adapter.adapter_class != src.adapter_class:
            problems.append(f"source {src.id}: adapter {src.adapter!r} is {adapter.adapter_class}, not {src.adapter_class}")
    for p in cfg.profiles.values():
        for t in p.topics:
            if not any(t in s.topics for s in cfg.sources.values()):
                problems.append(f"profile {p.id}@{p.version}: no source carries topic {t!r}")
    _out(
        {
            "ok": not problems,
            "problems": problems,
            "sources": sorted(cfg.sources),
            "profiles": sorted(f"{pid}@{v}" for pid, v in cfg.profiles),
            "models": sorted(cfg.models),
            "db": cfg.db_path,
        }
    )
    return 0 if not problems else 1


def cmd_poll(args: argparse.Namespace) -> int:
    from .ingest import Poller

    cfg, store = _load(args)
    poller = Poller(store, cfg, clock=_clock(args))
    if args.source:
        outcomes = [poller.poll(s) for s in args.source]
    elif args.loop:  # pragma: no cover - long-running
        import threading

        stop = threading.Event()
        try:
            poller.run(stop)
        except KeyboardInterrupt:
            stop.set()
        return 0
    else:
        outcomes = poller.run_due()
    _out([_outcome(o) for o in outcomes])
    return 0


def _outcome(o) -> dict[str, Any]:
    return {
        "source": o.source_id,
        "status": o.status,
        "detail": o.detail,
        "next_poll_at": iso(o.next_poll_at),
        "pages": o.pages,
        "stats": o.stats.__dict__ if o.stats else None,
        **o.extra,
    }


def cmd_backfill(args: argparse.Namespace) -> int:
    from .ingest import Poller

    cfg, store = _load(args)
    _out(_outcome(Poller(store, cfg, clock=_clock(args)).backfill(args.source)))
    return 0


def cmd_ingest_file(args: argparse.Namespace) -> int:
    from .ingest import Poller

    cfg, store = _load(args)
    body = Path(args.file).read_bytes()
    _out(_outcome(Poller(store, cfg, clock=_clock(args)).ingest_bytes(args.source, body, check_gap=not args.no_gap_check)))
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    cfg, store = _load(args)
    recap = Recap(cfg, store, clock=_clock(args))
    profile = cfg.profile(args.profile, args.profile_version)
    if profile is None:
        print(f"unknown profile {args.profile}", file=sys.stderr)
        return 1
    res = recap.assemble(
        AssembleRequest(
            profile_id=profile.id,
            profile_version=profile.version,
            model_id=args.model,
            context_window=args.context_window,
            base_input_tokens=args.base_input,
            reserved_output_tokens=args.reserved_output,
            serializer_version=args.serializer,
            budget=args.budget,
            as_of=args.as_of,
            mode="replay" if args.replay else "live",
        )
    )
    if args.text:
        print(res.rendered_data or f"(empty: {res.reason})")
    else:
        _out(res.to_dict())
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    cfg, store = _load(args)
    _out(Recap(cfg, store, clock=_clock(args)).resolve(args.packet_id, args.ref, at=args.at, mode="replay" if args.replay else "live"))
    return 0


def cmd_lookup(args: argparse.Namespace) -> int:
    cfg, store = _load(args)
    _out(Recap(cfg, store, clock=_clock(args)).lookup(key=args.key, entity=args.entity, predicate=args.predicate, scope=args.scope, as_of=args.as_of))
    return 0


def cmd_facts(args: argparse.Namespace) -> int:
    cfg, store = _load(args)
    rows = []
    for v in store.list_latest_versions(args.entity, args.limit):
        rows.append(
            {
                "fact_id": v.fact_id,
                "source": v.source_id,
                "entity": v.entity,
                "predicate": v.predicate,
                "scope": v.scope,
                "subject": v.subject,
                "value": v.value,
                "state": v.state.value,
                "effective_at": iso(v.effective_at),
                "published_at": iso(v.published_at),
                "origin": v.origin,
            }
        )
    _out(rows)
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    cfg, store = _load(args)
    now = int(_clock(args)())
    if args.action == "list":
        _out(store.list_reviews(None if args.all else "pending"))
        return 0
    if args.action == "approve":
        fact = json.loads(Path(args.fact).read_text()) if args.fact else None
        v = admin.approve_review(store, cfg, args.id, decided_by=args.by, now=now, text=args.text, fact=fact)
        _out({"approved": args.id, "fact_id": v.fact_id})
        return 0
    admin.reject_review(store, args.id, decided_by=args.by, now=now, note=args.note)
    _out({"rejected": args.id})
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    cfg, store = _load(args)
    _out(admin.recall(store, now=int(_clock(args)()), reason=args.reason, fact_ids=args.fact or [], source_id=args.source))
    return 0


def cmd_editorial(args: argparse.Namespace) -> int:
    cfg, store = _load(args)
    now = int(_clock(args)())
    if args.action == "list":
        recap = Recap(cfg, store, clock=_clock(args))
        _out(recap.index.editorial_picks(args.profile, recap.index.watermark, now))
        return 0
    admin.editorial(store, cfg, args.profile, args.fact_id, action=args.action, now=now)
    _out({"profile": args.profile, args.action: args.fact_id})
    return 0


def cmd_source(args: argparse.Namespace) -> int:
    cfg, store = _load(args)
    now = int(_clock(args)())
    if args.action == "status":
        recap = Recap(cfg, store, clock=_clock(args))
        out = {}
        for sid in sorted(cfg.sources):
            status, reason, last_ok = recap.index.source_status(sid, recap.index.watermark, now)
            rt = store.get_runtime(sid)
            out[sid] = {"status": status, "reason": reason, "last_ok_check": iso(last_ok), "next_poll_at": iso(rt.next_poll_at), "failures": rt.failures}
        _out(out)
        return 0
    if args.action == "resume":
        admin.resume_source(store, args.source, now=now, reason=args.reason or "manual sign-off")
    else:
        admin.pause_source(store, args.source, now=now, reason=args.reason or "manual pause")
    _out({"source": args.source, "action": args.action})
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from .tokens import CalibrationSample, fit_calibration

    samples = []
    with open(args.samples, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                d = json.loads(line)
                samples.append(CalibrationSample(d["packet"], int(d["actual"])))
    report = fit_calibration(samples, margin=args.margin, version=args.version, min_samples=args.min_samples)
    c = report.calibration
    print(f"# {report.samples} samples, {report.violations} violations, mean overhead {report.mean_overhead:.1%}, max ratio {report.max_ratio:.2f}")
    print("[model.calibration]")
    print(f"alpha = {c.alpha}\nbeta = {c.beta}\ngamma = {c.gamma}\nmargin = {c.margin}\nversion = \"{c.version}\"")
    return 0 if report.violations == 0 else 1


def cmd_gates(args: argparse.Namespace) -> int:
    records = [json.loads(line) for line in Path(args.graded).read_text().splitlines() if line.strip()]
    hard = None
    if args.fixtures_report:
        hard = json.loads(Path(args.fixtures_report).read_text())["hard_failures"]
    report = evaluate_gates(records, hard_failures=hard, economics_net_savings=args.net_savings, n_boot=args.boot)
    _out(report.to_dict())
    return 0 if report.passed else 1


def cmd_replay(args: argparse.Namespace) -> int:
    cfg, store = _load(args)
    text = Path(args.fixtures).read_text()
    fixtures = json.loads(text) if text.lstrip().startswith("[") else [json.loads(x) for x in text.splitlines() if x.strip()]
    report = run_fixtures(Recap(cfg, store), fixtures)
    _out(report.to_dict())
    return 0 if report.passed else 1


def cmd_economics(args: argparse.Namespace) -> int:
    b = break_even(
        packet_tokens=args.packet_tokens,
        turns=args.turns,
        cached_rate=args.cached_rate,
        search_tokens=args.search_tokens,
        search_fee=args.search_fee,
        search_turn=args.search_turn,
        amortized_pipeline=args.pipeline,
    )
    _out({"packet_cost": b.packet_cost, "search_cost": b.search_cost, "break_even_rate": round(b.break_even_rate, 4)})
    return 0


def cmd_holdout(args: argparse.Namespace) -> int:
    _out({"holdout_traces": holdout_size(args.p_d, args.design_effect)})
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from .bench import build_synthetic, run_benchmark, synthetic_config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = synthetic_config(str(Path(tmp) / "bench.db"))
        store = Store(cfg.db_path)
        now = parse_time("2026-11-18T12:00:00Z")
        build_synthetic(store, cfg, n_facts=args.facts, now=now)
        recap = Recap(cfg, store, clock=lambda: now)
        _out({"facts": args.facts, **run_benchmark(recap, runs=args.runs, now=now)})
        store.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover - long-running
    from .server import serve

    cfg, store = _load(args)
    httpd = serve(Recap(cfg, store), args.host, args.port)
    print(f"RECAP listening on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recap", description="RECAP reference implementation")
    p.add_argument("--version", action="version", version=f"recap {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def with_config(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("-c", "--config", default="recap.toml", help="path to recap.toml")
        sp.add_argument("--now", help="override the clock (ISO time), for testing and replays")
        return sp

    sp = sub.add_parser("init", help="write an example configuration")
    sp.add_argument("directory")
    sp.set_defaults(func=cmd_init)

    sp = with_config(sub.add_parser("check-config", help="validate configuration"))
    sp.set_defaults(func=cmd_check_config)

    sp = with_config(sub.add_parser("poll", help="poll due sources once (or --loop)"))
    sp.add_argument("--source", action="append", help="poll only this source (repeatable)")
    sp.add_argument("--loop", action="store_true", help="keep polling on schedule")
    sp.set_defaults(func=cmd_poll)

    sp = with_config(sub.add_parser("backfill", help="fetch history for a source"))
    sp.add_argument("source")
    sp.set_defaults(func=cmd_backfill)

    sp = with_config(sub.add_parser("ingest-file", help="run a source's adapter over a local file"))
    sp.add_argument("source")
    sp.add_argument("file")
    sp.add_argument("--no-gap-check", action="store_true")
    sp.set_defaults(func=cmd_ingest_file)

    sp = with_config(sub.add_parser("assemble", help="build a packet"))
    sp.add_argument("--profile", required=True)
    sp.add_argument("--profile-version", type=int)
    sp.add_argument("--model", required=True)
    sp.add_argument("--serializer", default="v1")
    sp.add_argument("--budget", type=int)
    sp.add_argument("--context-window", type=int, default=200_000)
    sp.add_argument("--base-input", type=int, default=2_000)
    sp.add_argument("--reserved-output", type=int, default=4_096)
    sp.add_argument("--as-of")
    sp.add_argument("--replay", action="store_true")
    sp.add_argument("--text", action="store_true", help="print only the packet text")
    sp.set_defaults(func=cmd_assemble)

    sp = with_config(sub.add_parser("resolve", help="provenance for a packet line"))
    sp.add_argument("packet_id")
    sp.add_argument("ref")
    sp.add_argument("--at")
    sp.add_argument("--replay", action="store_true")
    sp.set_defaults(func=cmd_resolve)

    sp = with_config(sub.add_parser("lookup", help="exact-key or entity lookup"))
    sp.add_argument("--key")
    sp.add_argument("--entity")
    sp.add_argument("--predicate")
    sp.add_argument("--scope")
    sp.add_argument("--as-of")
    sp.set_defaults(func=cmd_lookup)

    sp = with_config(sub.add_parser("facts", help="list stored facts (latest versions)"))
    sp.add_argument("--entity")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_facts)

    sp = with_config(sub.add_parser("review", help="review queue"))
    sp.add_argument("action", choices=["list", "approve", "reject"])
    sp.add_argument("id", type=int, nargs="?")
    sp.add_argument("--text", help="final highlights text (approve)")
    sp.add_argument("--fact", help="JSON file with a typed fact (approve, review-class sources)")
    sp.add_argument("--by", default="operator", help="reviewer name for the audit trail")
    sp.add_argument("--note")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_review)

    sp = with_config(sub.add_parser("recall", help="tombstone facts or a whole source"))
    sp.add_argument("--fact", action="append")
    sp.add_argument("--source")
    sp.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_recall)

    sp = with_config(sub.add_parser("editorial", help="editorial picks for the general profile"))
    sp.add_argument("action", choices=["add", "remove", "list"])
    sp.add_argument("profile")
    sp.add_argument("fact_id", nargs="?")
    sp.set_defaults(func=cmd_editorial)

    sp = with_config(sub.add_parser("source", help="source status, pause, resume"))
    sp.add_argument("action", choices=["status", "pause", "resume"])
    sp.add_argument("source", nargs="?")
    sp.add_argument("--reason")
    sp.set_defaults(func=cmd_source)

    sp = sub.add_parser("calibrate", help="fit a calibrated token bound from samples")
    sp.add_argument("--samples", required=True, help='JSONL of {"packet": ..., "actual": n}')
    sp.add_argument("--margin", type=float, default=0.05)
    sp.add_argument("--version", default="")
    sp.add_argument("--min-samples", type=int, default=10_000)
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("gates", help="evaluate ship gates from graded results")
    sp.add_argument("--graded", required=True)
    sp.add_argument("--fixtures-report")
    sp.add_argument("--net-savings", type=float)
    sp.add_argument("--boot", type=int, default=2000)
    sp.set_defaults(func=cmd_gates)

    sp = with_config(sub.add_parser("replay", help="run deterministic fixtures"))
    sp.add_argument("--fixtures", required=True)
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("economics", help="per-conversation break-even")
    sp.add_argument("--packet-tokens", type=float, required=True)
    sp.add_argument("--turns", type=int, required=True)
    sp.add_argument("--cached-rate", type=float, required=True)
    sp.add_argument("--search-tokens", type=float, required=True)
    sp.add_argument("--search-fee", type=float, default=0.0)
    sp.add_argument("--search-turn", type=int, default=1)
    sp.add_argument("--pipeline", type=float, default=0.0)
    sp.set_defaults(func=cmd_economics)

    sp = sub.add_parser("holdout-size", help="holdout traces needed per profile")
    sp.add_argument("--p-d", type=float, required=True)
    sp.add_argument("--design-effect", type=float, default=1.0)
    sp.set_defaults(func=cmd_holdout)

    sp = sub.add_parser("bench", help="latency benchmark on synthetic data")
    sp.add_argument("--facts", type=int, default=10_000)
    sp.add_argument("--runs", type=int, default=300)
    sp.set_defaults(func=cmd_bench)

    sp = with_config(sub.add_parser("serve", help="JSON over HTTP on localhost"))
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (ConfigError, admin.AdminError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # output piped into head and friends
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
