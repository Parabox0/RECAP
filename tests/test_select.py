import itertools
import random

import pytest

from recap.select import Item, canonical_order, item_value, select


def brute_force(items, budget, max_items, per_group):
    seq = canonical_order([it for it in items if 0 < it.cost <= budget and it.u > 0])
    best, best_key = [], None
    for k in range(0, min(max_items, len(seq)) + 1):
        for combo in itertools.combinations(range(len(seq)), k):
            chosen = [seq[i] for i in combo]
            if sum(it.cost for it in chosen) > budget:
                continue
            groups = {}
            for it in chosen:
                groups[it.group] = groups.get(it.group, 0) + 1
            if any(n > per_group for n in groups.values()):
                continue
            value = sum(item_value(it) for it in chosen)
            indicator = tuple(1 if i in combo else 0 for i in range(len(seq)))
            key = (value, indicator)
            if best_key is None or key > best_key:
                best_key, best = key, sorted(it.rank for it in chosen)
    return best


@pytest.mark.parametrize("seed", range(60))
def test_dp_matches_brute_force(seed):
    rnd = random.Random(seed)
    n = rnd.randint(1, 10)
    items = [
        Item(rank=i, group=f"g{rnd.randint(0, 3)}", u=rnd.choice([0.6, 0.665, 0.7, 0.8, 0.85, 1.0, round(rnd.uniform(0.55, 1), 3)]), cost=rnd.randint(5, 60))
        for i in range(n)
    ]
    budget = rnd.randint(10, 200)
    max_items = rnd.randint(1, 8)
    per_group = rnd.randint(1, 3)
    assert select(items, budget=budget, max_items=max_items, per_group=per_group) == brute_force(items, budget, max_items, per_group)


def test_ties_are_deterministic():
    items = [Item(rank=i, group=f"g{i}", u=0.7, cost=10) for i in range(5)]
    assert select(items, budget=30, max_items=8) == [0, 1, 2]
    assert select(list(reversed(items)), budget=30, max_items=8) == [0, 1, 2]


def test_count_cap_does_not_let_short_routine_lines_push_out_an_important_one():
    # The v0.1 failure: ranking by utility per token fills the 8-fact cap with
    # short routine lines before a fresh exact-entity deprecation is considered.
    routine = [Item(rank=i + 1, group=f"r{i}", u=0.665, cost=12) for i in range(8)]
    deprecation = Item(rank=0, group="dep", u=1.0, cost=30)
    chosen = select(routine + [deprecation], budget=150, max_items=8)
    assert 0 in chosen
    assert len(chosen) == 8


def test_per_entity_cap():
    items = [Item(rank=i, group="same", u=0.9, cost=10) for i in range(5)]
    assert select(items, budget=200, max_items=8, per_group=2) == [0, 1]


def test_budget_and_degenerate_inputs():
    items = [Item(rank=0, group="a", u=0.9, cost=50)]
    assert select(items, budget=49) == []
    assert select(items, budget=0) == []
    assert select([], budget=100) == []
    with pytest.raises(ValueError):
        select(items, budget=2048)


def test_prefers_fewer_tokens_at_equal_utility():
    items = [Item(rank=0, group="a", u=0.8, cost=40), Item(rank=1, group="b", u=0.8, cost=20)]
    assert select(items, budget=45, max_items=1) == [1]
