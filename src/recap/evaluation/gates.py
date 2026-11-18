"""Evaluate the ship gates from graded replay results (spec section 5).

RECAP does not run the model or grade answers. Run your A (incumbent lookup)
and C (packet + verified suppression) systems over the frozen holdout, grade
them blindly, and write one JSON object per trace:

.. code-block:: json

    {"trace_id": "t1", "cluster": "event-42", "stratum": "fast_moving",
     "a": {"correct": true, "avoidable_lookups": 1},
     "c": {"correct": true, "avoidable_lookups": 0,
           "unsupported_current_claim": false,
           "first_turn_search": false, "added_latency_ms": 3.1}}

Hard-failure counts come from :func:`recap.evaluation.replay.run_fixtures`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from .stats import Interval, clopper_pearson_upper, cluster_bootstrap


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str
    value: Any = None


@dataclass
class GateReport:
    gates: list[Gate] = field(default_factory=list)
    n: int = 0

    @property
    def passed(self) -> bool:
        return bool(self.gates) and all(g.passed for g in self.gates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "n": self.n,
            "gates": [{"name": g.name, "passed": g.passed, "detail": g.detail, "value": g.value} for g in self.gates],
        }


def _corr_diff(records: Sequence[Mapping[str, Any]]) -> float:
    if not records:
        return 0.0
    return 100.0 * sum(int(bool(r["c"]["correct"])) - int(bool(r["a"]["correct"])) for r in records) / len(records)


def _lookup_reduction(records: Sequence[Mapping[str, Any]]) -> float:
    a = sum(float(r["a"].get("avoidable_lookups", 0)) for r in records)
    c = sum(float(r["c"].get("avoidable_lookups", 0)) for r in records)
    return 0.0 if a == 0 else 1.0 - c / a


def evaluate_gates(
    records: Sequence[Mapping[str, Any]],
    *,
    hard_failures: Mapping[str, int] | None = None,
    economics_net_savings: float | None = None,
    n_boot: int = 2000,
    seed: int = 0,
    correctness_margin_pp: float = -2.0,
    min_lookup_reduction: float = 0.20,
    max_unsupported_rate: float = 0.01,
    max_added_latency_ms: float = 50.0,
) -> GateReport:
    report = GateReport(n=len(records))
    if not records:
        report.gates.append(Gate("data", False, "no graded records"))
        return report
    cluster = lambda r: r.get("cluster", r.get("trace_id"))  # noqa: E731

    corr: Interval = cluster_bootstrap(records, _corr_diff, cluster, n_boot=n_boot, seed=seed)
    report.gates.append(
        Gate(
            "correctness_vs_A",
            corr.lower >= correctness_margin_pp,
            f"C-A = {corr.point:+.2f} pp, 95% CI [{corr.lower:+.2f}, {corr.upper:+.2f}]; need lower >= {correctness_margin_pp:+.1f}",
            {"point": corr.point, "lower": corr.lower, "upper": corr.upper},
        )
    )

    red: Interval = cluster_bootstrap(records, _lookup_reduction, cluster, n_boot=n_boot, seed=seed + 1)
    report.gates.append(
        Gate(
            "avoidable_lookups_vs_A",
            red.point >= min_lookup_reduction and red.lower > 0,
            f"reduction {red.point:.1%}, 95% CI [{red.lower:.1%}, {red.upper:.1%}]; need >= {min_lookup_reduction:.0%} and lower > 0",
            {"point": red.point, "lower": red.lower, "upper": red.upper},
        )
    )

    failures = sum(1 for r in records if r["c"].get("unsupported_current_claim"))
    upper = clopper_pearson_upper(failures, len(records))
    report.gates.append(
        Gate(
            "unsupported_current_claims",
            upper <= max_unsupported_rate,
            f"{failures} failing traces of {len(records)}; one-sided 95% upper bound {upper:.3%}; need <= {max_unsupported_rate:.0%}",
            {"failures": failures, "upper": upper},
        )
    )

    latencies = [
        float(r["c"]["added_latency_ms"])
        for r in records
        if not r["c"].get("first_turn_search") and r["c"].get("added_latency_ms") is not None
    ]
    if latencies:
        p95 = float(np.percentile(latencies, 95))
        report.gates.append(Gate("latency", p95 <= max_added_latency_ms, f"p95 added latency {p95:.1f} ms over {len(latencies)} non-search first turns; need <= {max_added_latency_ms:.0f}", p95))
    else:
        report.gates.append(Gate("latency", False, "no latency measurements for non-search first turns"))

    if hard_failures is None:
        report.gates.append(Gate("hard_failures", False, "no fixture report supplied"))
    else:
        total = sum(int(v) for v in hard_failures.values())
        detail = ", ".join(f"{k}={v}" for k, v in sorted(hard_failures.items())) or "none"
        report.gates.append(Gate("hard_failures", total == 0, f"{detail}; need all zero", dict(hard_failures)))

    if economics_net_savings is None:
        report.gates.append(Gate("economics", False, "net savings per eligible conversation not supplied"))
    else:
        ok = math.isfinite(economics_net_savings) and economics_net_savings > 0
        report.gates.append(Gate("economics", ok, f"net savings {economics_net_savings:.1f} token-equivalents per eligible conversation; need > 0", economics_net_savings))
    return report
