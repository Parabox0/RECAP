"""Exact packet selection (spec section 3, "Selection is exact, not greedy").

Choose the subset of candidates that maximises total utility subject to a
fact cap, a per-entity cap and a token budget. Ties go to fewer tokens, then
to a canonical order: candidates are arranged by entity (entities ordered by
their best-ranked candidate), then by rank, and the subset that includes the
earliest candidate at the first point of difference wins.

The objective is encoded as one integer per item,
``round(u * 1e6) * 1024 - cost``. Because total cost never exceeds 1023,
maximising the sum is exactly "most utility, then fewest tokens". A suffix
dynamic program over (items taken from the current entity, facts used,
tokens used) gives the optimum, and a forward pass that takes an item
whenever an optimal completion exists yields the canonical tie-break.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SCALE = 1_000_000
TOKEN_RADIX = 1024
_NEG = np.int64(-(1 << 60))


@dataclass(frozen=True, slots=True)
class Item:
    rank: int
    group: str
    u: float
    cost: int


def canonical_order(items: list[Item]) -> list[Item]:
    """Items arranged entity by entity, entities ordered by their best rank."""
    by_rank = sorted(items, key=lambda it: it.rank)
    group_pos: dict[str, int] = {}
    for it in by_rank:
        group_pos.setdefault(it.group, len(group_pos))
    return sorted(by_rank, key=lambda it: (group_pos[it.group], it.rank))


def item_value(it: Item) -> int:
    return int(round(it.u * SCALE)) * TOKEN_RADIX - it.cost


def select(items: list[Item], *, budget: int, max_items: int = 8, per_group: int = 2) -> list[int]:
    """Return the ranks of the chosen items, in rank order."""
    if budget <= 0 or max_items <= 0 or per_group <= 0:
        return []
    if budget >= TOKEN_RADIX:
        raise ValueError(f"budget must be below {TOKEN_RADIX}")
    seq = canonical_order([it for it in items if 0 < it.cost <= budget and it.u > 0])
    n = len(seq)
    if n == 0:
        return []
    K, B, G = max_items, budget, per_group
    vals = [item_value(it) for it in seq]
    same_next = [i + 1 < n and seq[i + 1].group == seq[i].group for i in range(n)]

    S = np.zeros((n + 1, G + 1, K + 1, B + 1), dtype=np.int64)
    take = np.empty((K + 1, B + 1), dtype=np.int64)
    for i in range(n - 1, -1, -1):
        w, v, same = seq[i].cost, vals[i], same_next[i]
        for g in range(G + 1):
            skip = S[i + 1, g if same else 0]
            if g < G:
                nxt = S[i + 1, g + 1 if same else 0]
                take.fill(_NEG)
                take[1:, w:] = nxt[:-1, : B + 1 - w] + v
                np.maximum(skip, take, out=S[i, g])
            else:
                S[i, g] = skip

    chosen: list[int] = []
    g, k, t = 0, K, B
    for i in range(n):
        w, same = seq[i].cost, same_next[i]
        g_take = g + 1 if same else 0
        if g < G and k >= 1 and t >= w and S[i + 1, g_take, k - 1, t - w] + vals[i] == S[i, g, k, t]:
            chosen.append(seq[i].rank)
            k, t, g = k - 1, t - w, g_take
        else:
            g = g if same else 0
    return sorted(chosen)
