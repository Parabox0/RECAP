"""Statistics for the launch gates (spec section 5). Pure Python plus NumPy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Hashable, Sequence

import numpy as np


def _log_binom_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return (
        math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
        + k * math.log(p) + (n - k) * math.log1p(-p)
    )


def binom_cdf(x: int, n: int, p: float) -> float:
    if x < 0:
        return 0.0
    if x >= n:
        return 1.0
    terms = [_log_binom_pmf(k, n, p) for k in range(x + 1)]
    m = max(terms)
    if m == -math.inf:
        return 0.0
    return min(1.0, math.exp(m) * sum(math.exp(t - m) for t in terms))


def clopper_pearson_upper(failures: int, n: int, confidence: float = 0.95) -> float:
    """One-sided exact upper bound on a failure rate."""
    if n <= 0:
        return 1.0
    if not 0 <= failures <= n:
        raise ValueError("failures must be between 0 and n")
    if failures == n:
        return 1.0
    alpha = 1.0 - confidence
    lo, hi = failures / n, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if binom_cdf(failures, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def holdout_size(p_d: float, design_effect: float = 1.0, *, floor: int = 600, margin_pp: float = 2.0) -> int:
    """Traces needed for ~80% power at a non-inferiority margin, true difference zero.

    ``n >= p_d * d * ((1.96 + 0.84) / margin)^2``; with a 2-point margin the
    constant is 19,600.
    """
    if not 0 <= p_d <= 1 or design_effect < 1:
        raise ValueError("p_d must be in [0, 1] and the design effect at least 1")
    constant = ((1.96 + 0.84) / (margin_pp / 100.0)) ** 2
    return max(floor, math.ceil(round(constant * p_d * design_effect, 9)))


@dataclass(frozen=True)
class Interval:
    point: float
    lower: float
    upper: float


def cluster_bootstrap(
    records: Sequence,
    statistic: Callable[[Sequence], float],
    cluster_of: Callable[[object], Hashable],
    *,
    n_boot: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> Interval:
    """Percentile bootstrap that resamples whole clusters (events) with replacement."""
    clusters: dict[Hashable, list] = {}
    for r in records:
        clusters.setdefault(cluster_of(r), []).append(r)
    keys = sorted(clusters, key=repr)
    groups = [clusters[k] for k in keys]
    point = statistic(records)
    if len(groups) < 2:
        return Interval(point, point, point)
    rng = np.random.default_rng(seed)
    stats = np.empty(n_boot)
    for b in range(n_boot):
        picks = rng.integers(0, len(groups), len(groups))
        sample = [r for i in picks for r in groups[i]]
        stats[b] = statistic(sample)
    alpha = (1 - confidence) / 2
    return Interval(point, float(np.quantile(stats, alpha)), float(np.quantile(stats, 1 - alpha)))


def design_effect(records: Sequence, statistic: Callable[[Sequence], float], cluster_of: Callable[[object], Hashable], *, n_boot: int = 1000, seed: int = 0) -> float:
    """Variance of the statistic under cluster resampling over variance under
    independent resampling. At least 1."""
    rng = np.random.default_rng(seed)
    n = len(records)
    if n < 2:
        return 1.0
    iid = np.array([statistic([records[i] for i in rng.integers(0, n, n)]) for _ in range(n_boot)])
    clusters: dict[Hashable, list] = {}
    for r in records:
        clusters.setdefault(cluster_of(r), []).append(r)
    groups = [clusters[k] for k in sorted(clusters, key=repr)]
    if len(groups) < 2:
        return 1.0
    clustered = np.array(
        [statistic([r for i in rng.integers(0, len(groups), len(groups)) for r in groups[i]]) for _ in range(n_boot)]
    )
    v_iid = float(iid.var())
    if v_iid == 0:
        return 1.0
    return max(1.0, float(clustered.var()) / v_iid)
