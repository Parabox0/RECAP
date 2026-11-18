"""Synthetic data for latency benchmarks.

``build_synthetic`` writes facts straight into a store (bypassing adapters)
and attests all of them recently, so nearly every fact is admissible. That
is a worse case for the candidate pool than real traffic.
"""

from __future__ import annotations

import random
import statistics
import time
from typing import Any

from .config import RecapConfig
from .model import DETERMINISTIC, Evidence, State, content_hash, make_fact_id
from .runtime import AssembleRequest, Recap
from .store import Store
from .timeutil import DAY, HOUR

TOPICS = [f"topic_{i:02d}" for i in range(12)]


def synthetic_config(db_path: str, n_sources: int = 20) -> RecapConfig:
    rnd = random.Random(7)
    sources = []
    for i in range(n_sources):
        sources.append(
            {
                "id": f"src-{i:02d}",
                "adapter": "atom",
                "url": f"https://source{i}.example.org/feed.atom",
                "permitted_origins": [f"https://source{i}.example.org"],
                "permitted_paths": ["/"],
                "fact_types": ["release", "deprecation", "status"],
                "attests_current": True,
                "topics": rnd.sample(TOPICS, 3),
                "entities": [f"bench:e{j}" for j in range(i * 20, i * 20 + 20)],
                "options": {"entity": f"bench:e{i * 20}", "entity_label": "Bench", "title_pattern": "(?P<version>.+)"},
            }
        )
    profiles = [
        {
            "id": "bench",
            "version": 1,
            "topics": {TOPICS[0]: 1.0, TOPICS[1]: 0.9, TOPICS[2]: 0.8, TOPICS[3]: 0.7, TOPICS[4]: 0.6, TOPICS[5]: 0.5},
            "entities": [f"bench:e{j}" for j in range(0, 200, 20)],
        }
    ]
    models = [{"id": "bench-model", "knowledge_through": "2026-06-30", "counting": "byte_bound"}]
    return RecapConfig.build(db_path, sources=sources, profiles=profiles, models=models)


def build_synthetic(store: Store, config: RecapConfig, *, n_facts: int, now: int, seed: int = 1) -> None:
    rnd = random.Random(seed)
    srcs = list(config.sources.values())
    with store.batch(now - DAY) as b:
        written: dict[str, list[str]] = {}
        for n in range(n_facts):
            src = srcs[n % len(srcs)]
            entity = rnd.choice(src.entities)
            kind = rnd.random()
            published = now - rnd.randint(1, 170) * DAY - rnd.randint(0, 23) * HOUR
            if kind < 0.7:
                # Versions rise with publication time, as they do for real projects.
                version = f"1.{(published - (now - 200 * DAY)) // HOUR}.{n}"
                channel = "stable" if rnd.random() < 0.8 else "beta"
                predicate, subject = "release", f"{version}@{channel}"
                value, qualifiers = {"version": version}, {"channel": channel}
            elif kind < 0.85:
                predicate, subject = "deprecation", f"api v{n}"
                value, qualifiers = {"item": f"legacy api v{n}"}, {}
            else:
                predicate, subject = "status", f"component {n % 7}"
                value, qualifiers = {"status": rnd.choice(["nominal", "degraded", "safe mode"]), "component": f"unit {n % 7}"}, {}
            row = {
                "fact_id": make_fact_id(src.id, f"entry-{n}", predicate, "global", subject),
                "source_id": src.id,
                "entry_id": f"entry-{n}",
                "entity": entity,
                "entity_label": f"Entity {entity.split(':')[1]}",
                "predicate": predicate,
                "scope": "global",
                "subject": subject,
                "value": value,
                "qualifiers": qualifiers,
                "event_at": published,
                "effective_at": published,
                "valid_to": None,
                "published_at": published,
                "explicit_expires_at": None,
                "state": State.ACTIVE,
                "supersedes": (),
                "source_revision": published,
                "origin": DETERMINISTIC,
                "evidence": Evidence(src.url, "0" * 64, None, "1"),
                "topics": src.topics,
                "authority": "primary",
                "observed_at": now - DAY,
            }
            row["content_hash"] = content_hash(row)
            b.add_fact_version(row)
            written.setdefault(src.id, []).append(row["fact_id"])
    with store.batch(now - 600) as b:
        for src in srcs:
            seq = b.add_check(src.id, 200, "0" * 64, True, "ok")
            b.extend_attestations(src.id, written.get(src.id, []), None, seq)


def run_benchmark(recap: Recap, *, runs: int, now: int) -> dict[str, Any]:
    """Time ``assemble`` with the packet cache cleared before every call (cold)
    and with repeated identical requests (warm)."""

    def request(i: int) -> AssembleRequest:
        return AssembleRequest(
            profile_id="bench",
            profile_version=1,
            model_id="bench-model",
            context_window=200_000,
            base_input_tokens=2_000,
            reserved_output_tokens=4_096,
            budget=192 - (i % 64),
            as_of=now + i,
        )

    def summary(timings: list[float]) -> dict[str, float]:
        timings = sorted(timings)
        return {
            "p50_ms": round(statistics.median(timings), 3),
            "p95_ms": round(timings[int(0.95 * (len(timings) - 1))], 3),
            "max_ms": round(timings[-1], 3),
        }

    cold, warm = [], []
    statuses: dict[str, int] = {}
    for i in range(runs):
        recap.purge_facts(set(recap.index.fact_ids()))
        recap._cache.clear()
        t0 = time.perf_counter()
        res = recap.assemble(request(i))
        cold.append((time.perf_counter() - t0) * 1000)
        label = res.status if res.status != "empty" else f"empty:{res.reason}"
        statuses[label] = statuses.get(label, 0) + 1
    recap.assemble(request(0))
    for _ in range(runs):
        t0 = time.perf_counter()
        recap.assemble(request(0))
        warm.append((time.perf_counter() - t0) * 1000)
    return {"runs": runs, "cold": summary(cold), "warm": summary(warm), "statuses": statuses}
