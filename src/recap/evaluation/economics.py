"""Per-conversation cost model (spec section 5, "Economics, per conversation").

All quantities are in token-equivalents at the uncached input rate, so
``uncached_rate`` defaults to 1 and ``cached_rate`` is the cached price as a
fraction of it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Breakdown:
    packet_cost: float
    search_cost: float
    break_even_rate: float


def packet_cost(packet_tokens: float, turns: int, *, cached_rate: float, uncached_rate: float = 1.0, lost_cache_savings: float = 0.0, amortized_pipeline: float = 0.0) -> float:
    if turns < 1:
        raise ValueError("a conversation has at least one turn")
    return packet_tokens * uncached_rate + packet_tokens * cached_rate * (turns - 1) + lost_cache_savings + amortized_pipeline


def search_cost(result_tokens: float, turns: int, *, search_turn: int = 1, fee: float = 0.0, cached_rate: float, uncached_rate: float = 1.0, follow_up_calls: float = 0.0) -> float:
    if not 1 <= search_turn <= turns:
        raise ValueError("search_turn must be within the conversation")
    return fee + result_tokens * uncached_rate + result_tokens * cached_rate * (turns - search_turn) + follow_up_calls


def break_even(
    *,
    packet_tokens: float,
    turns: int,
    cached_rate: float,
    search_tokens: float,
    search_fee: float = 0.0,
    search_turn: int = 1,
    lost_cache_savings: float = 0.0,
    amortized_pipeline: float = 0.0,
    follow_up_calls: float = 0.0,
) -> Breakdown:
    """The share of eligible conversations in which the packet must prevent a search."""
    p = packet_cost(packet_tokens, turns, cached_rate=cached_rate, lost_cache_savings=lost_cache_savings, amortized_pipeline=amortized_pipeline)
    s = search_cost(search_tokens, turns, search_turn=search_turn, fee=search_fee, cached_rate=cached_rate, follow_up_calls=follow_up_calls)
    return Breakdown(p, s, p / s if s > 0 else float("inf"))
