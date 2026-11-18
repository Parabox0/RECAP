"""Counting packet tokens locally (spec section 3, "Count against the actual model").

Three modes, all computed on the request path without an external call:

* ``byte_bound``: for byte-level tokenizers every token covers at least one
  UTF-8 byte, so the byte count is a guaranteed upper bound. Safe, but it
  overestimates by roughly 3-4x, so packets come out smaller.
* ``calibrated``: ``ceil((alpha * proxy + beta) * (1 + margin))`` per line plus
  a wrapper constant, with ``alpha``, ``beta`` and ``gamma`` fitted offline so
  the bound is never below the provider's count on the calibration set.
* ``exact``: a tokenizer callable supplied by the host that matches production.

Line costs are additive by construction: the rendered packet is
``open + "\\n" + sum(line + "\\n") + close``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from .config import Calibration, ModelEntry

_PROXY = re.compile(r"[A-Za-z]+|[0-9]|\s+|[^\sA-Za-z0-9]")


def proxy_count(text: str) -> int:
    """A deterministic pre-tokenizer count: letter runs, single digits,
    whitespace runs and every other character on its own."""
    return len(_PROXY.findall(text))


class TokenCounter:
    mode = "abstract"

    def line_cost(self, line: str) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def wrapper_cost(self, open_tag: str, close_tag: str) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def recount(self, packet_text: str) -> int | None:
        """Exact count of the finished packet, where the mode supports it."""
        return None


class ByteBoundCounter(TokenCounter):
    mode = "byte_bound"

    def __init__(self, framing_overhead: int = 8):
        self.framing_overhead = framing_overhead

    def line_cost(self, line: str) -> int:
        return len((line + "\n").encode("utf-8"))

    def wrapper_cost(self, open_tag: str, close_tag: str) -> int:
        return len((open_tag + "\n" + close_tag).encode("utf-8")) + self.framing_overhead


class CalibratedCounter(TokenCounter):
    mode = "calibrated"

    def __init__(self, calibration: Calibration):
        self.cal = calibration

    def line_cost(self, line: str) -> int:
        c = self.cal
        return math.ceil((c.alpha * proxy_count(line + "\n") + c.beta) * (1 + c.margin))

    def wrapper_cost(self, open_tag: str, close_tag: str) -> int:
        c = self.cal
        return math.ceil((c.alpha * proxy_count(open_tag + "\n" + close_tag) + c.gamma) * (1 + c.margin))


class ExactCounter(TokenCounter):
    mode = "exact"

    def __init__(self, count: Callable[[str], int], framing_overhead: int = 0):
        self._count = count
        self.framing_overhead = framing_overhead

    def line_cost(self, line: str) -> int:
        return int(self._count(line + "\n"))

    def wrapper_cost(self, open_tag: str, close_tag: str) -> int:
        return int(self._count(open_tag + "\n" + close_tag)) + self.framing_overhead

    def recount(self, packet_text: str) -> int | None:
        return int(self._count(packet_text)) + self.framing_overhead


def counter_for(model: ModelEntry, tokenizers: dict[str, Callable[[str], int]]) -> TokenCounter | None:
    """The counter for a model, or ``None`` when its counting mode is unsupported."""
    if model.counting == "byte_bound":
        return ByteBoundCounter(model.framing_overhead)
    if model.counting == "calibrated" and model.calibration is not None:
        return CalibratedCounter(model.calibration)
    if model.counting == "exact" and model.id in tokenizers:
        return ExactCounter(tokenizers[model.id], model.framing_overhead)
    return None


# -- offline calibration ---------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationSample:
    packet_text: str
    actual_tokens: int


@dataclass(frozen=True)
class CalibrationReport:
    calibration: Calibration
    samples: int
    violations: int
    mean_overhead: float
    max_ratio: float


def _features(packet_text: str) -> tuple[int, int]:
    lines = packet_text.split("\n")
    if len(lines) < 2:
        raise ValueError("a calibration packet needs an opening and a closing wrapper line")
    open_tag, close_tag, body = lines[0], lines[-1], lines[1:-1]
    proxy = proxy_count(open_tag + "\n" + close_tag) + sum(proxy_count(line + "\n") for line in body)
    return proxy, len(body)


def bound_for(counter: TokenCounter, packet_text: str) -> int:
    lines = packet_text.split("\n")
    return counter.wrapper_cost(lines[0], lines[-1]) + sum(counter.line_cost(line) for line in lines[1:-1])


def fit_calibration(
    samples: Sequence[CalibrationSample],
    *,
    margin: float = 0.05,
    version: str = "",
    min_samples: int = 10_000,
) -> CalibrationReport:
    """Fit ``alpha``, ``beta`` and ``gamma`` so the bound covers every sample.

    Each sample is a rendered packet (wrapper included) and the token count
    the provider reported for adding it. The search minimises the total bound
    over a grid of ``alpha`` and ``beta``; ``gamma`` is the smallest constant
    that leaves no sample above the bound.
    """
    if len(samples) < min_samples:
        raise ValueError(f"need at least {min_samples} calibration samples, got {len(samples)}")
    feats = np.array([_features(s.packet_text) for s in samples], dtype=np.float64)
    actual = np.array([s.actual_tokens for s in samples], dtype=np.float64)
    P, L = feats[:, 0], feats[:, 1]
    betas = np.arange(0.0, 8.0001, 0.25)
    best = None
    for alpha in np.arange(0.30, 3.0001, 0.01):
        resid = actual[None, :] - alpha * P[None, :] - betas[:, None] * L[None, :]
        gammas = np.maximum(np.ceil(resid.max(axis=1)), 0.0)
        totals = len(actual) * gammas + alpha * P.sum() + betas * L.sum()
        j = int(np.argmin(totals))
        cand = (float(totals[j]), round(float(alpha), 2), float(betas[j]), int(gammas[j]))
        if best is None or cand < best:
            best = cand
    _, alpha, beta, gamma = best
    cal = Calibration(alpha=alpha, beta=beta, gamma=gamma, margin=margin, version=version)
    counter = CalibratedCounter(cal)
    bounds = np.array([bound_for(counter, s.packet_text) for s in samples], dtype=np.float64)
    violations = int((bounds < actual).sum())
    overhead = float(((bounds - actual) / np.maximum(actual, 1)).mean())
    ratio = float((bounds / np.maximum(actual, 1)).max())
    return CalibrationReport(cal, len(samples), violations, overhead, ratio)
