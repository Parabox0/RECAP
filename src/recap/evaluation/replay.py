"""Deterministic fixtures for the hard-failure gate (spec section 5).

These fixtures need no model: they replay ``assemble``, ``resolve`` and
``lookup`` at pinned times and check the invariants that must never fail:
no expired or lapsed rows injected, no budget or cap overruns, and no invalid
citation that verifies as valid.

Fixture lines (JSON objects):

* ``{"type": "packet", "id": ..., "as_of": ..., "profile_id": ..., "profile_version": ...,
  "model_id": ..., "context_window": ..., "base_input_tokens": ..., "reserved_output_tokens": ...,
  "budget": optional, "expect": {"status": ..., "includes": [fact ids], "excludes": [...],
  "includes_keys": [...], "excludes_keys": [...]}}``
* ``{"type": "citation", "id": ..., "packet": <packet fixture id>, "ref": "f1", "at": ...,
  "expect_valid": bool}``
* ``{"type": "lookup", "id": ..., "entity": ..., "predicate": optional, "as_of": ...,
  "expect": {"covered": bool, "includes": [...], "excludes": [...]}}``
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..config import HARD_CAP_TOKENS
from ..model import key_to_str
from ..runtime import AssembleRequest, Recap

HARD_FAILURE_KINDS = (
    "expired_rows_injected",
    "lapsed_rows_injected",
    "budget_overruns",
    "cap_violations",
    "uncaught_invalid_citations",
)


@dataclass
class FixtureReport:
    hard_failures: dict[str, int] = field(default_factory=lambda: {k: 0 for k in HARD_FAILURE_KINDS})
    expectation_failures: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.expectation_failures == 0 and sum(self.hard_failures.values()) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "hard_failures": self.hard_failures,
            "expectation_failures": self.expectation_failures,
            "results": self.results,
        }


def _packet_fixture(recap: Recap, fx: Mapping[str, Any], report: FixtureReport) -> dict[str, Any]:
    req = AssembleRequest(
        profile_id=fx["profile_id"],
        profile_version=int(fx["profile_version"]),
        model_id=fx["model_id"],
        context_window=int(fx.get("context_window", 200_000)),
        base_input_tokens=int(fx.get("base_input_tokens", 2_000)),
        reserved_output_tokens=int(fx.get("reserved_output_tokens", 4_096)),
        serializer_version=fx.get("serializer_version", "v1"),
        budget=fx.get("budget"),
        as_of=fx["as_of"],
        mode="replay",
    )
    res = recap.assemble(req)
    problems: list[str] = []
    if res.packet_id:
        if res.added_input_tokens_max > (res.effective_budget or 0) or res.added_input_tokens_max > HARD_CAP_TOKENS:
            report.hard_failures["budget_overruns"] += 1
            problems.append("budget_overrun")
        entities = Counter()
        for ref, fid in res.refs.items():
            resolved = recap.resolve(res.packet_id, ref, at=fx["as_of"], mode="replay")
            current = resolved["current"]
            entities[recap.index.version_at(fid, res.snapshot_id, res.as_of).entity] += 1
            if not current["valid_at_time"]:
                kind = "lapsed_rows_injected" if current["reason"] == "lapsed_schedule" else "expired_rows_injected"
                report.hard_failures[kind] += 1
                problems.append(f"{ref}:{current['reason']}")
        if len(res.refs) > recap.settings.max_facts or any(n > recap.settings.per_entity_cap for n in entities.values()):
            report.hard_failures["cap_violations"] += 1
            problems.append("cap_violation")
    expect = fx.get("expect") or {}
    failed: list[str] = []
    if "status" in expect and res.status != expect["status"]:
        failed.append(f"status {res.status} != {expect['status']}")
    included = set(res.fact_ids)
    included_keys = set()
    for fid in res.fact_ids:
        v = recap.index.version_at(fid, res.snapshot_id, res.as_of)
        if v is not None:
            included_keys.add(key_to_str(v.key))
    for fid in expect.get("includes", []):
        if fid not in included:
            failed.append(f"missing {fid}")
    for fid in expect.get("excludes", []):
        if fid in included:
            failed.append(f"unexpected {fid}")
    for k in expect.get("includes_keys", []):
        if k not in included_keys:
            failed.append(f"missing key {k}")
    for k in expect.get("excludes_keys", []):
        if k in included_keys:
            failed.append(f"unexpected key {k}")
    if failed:
        report.expectation_failures += 1
    return {"id": fx.get("id"), "type": "packet", "status": res.status, "reason": res.reason, "packet_id": res.packet_id,
            "fact_ids": res.fact_ids, "problems": problems, "failed": failed}


def run_fixtures(recap: Recap, fixtures: Iterable[Mapping[str, Any]]) -> FixtureReport:
    report = FixtureReport()
    packets: dict[str, str | None] = {}
    for fx in fixtures:
        kind = fx.get("type")
        if kind == "packet":
            result = _packet_fixture(recap, fx, report)
            packets[str(fx.get("id"))] = result["packet_id"]
        elif kind == "citation":
            packet_id = packets.get(str(fx.get("packet")))
            result = {"id": fx.get("id"), "type": "citation", "failed": []}
            if packet_id is None:
                report.expectation_failures += 1
                result["failed"].append("referenced packet fixture produced no packet")
            else:
                resolved = recap.resolve(packet_id, fx["ref"], at=fx["at"], mode="replay")
                valid = bool(resolved.get("current", {}).get("valid_at_time")) and not resolved.get("packet_revoked")
                result.update({"valid": valid, "reason": resolved.get("current", {}).get("reason")})
                if valid and not fx["expect_valid"]:
                    report.hard_failures["uncaught_invalid_citations"] += 1
                    result["failed"].append("invalid citation verified as valid")
                elif not valid and fx["expect_valid"]:
                    report.expectation_failures += 1
                    result["failed"].append("valid citation rejected")
        elif kind == "lookup":
            out = recap.lookup(entity=fx["entity"], predicate=fx.get("predicate"), scope=fx.get("scope"), as_of=fx["as_of"], mode="replay")
            expect = fx.get("expect") or {}
            ids = {r["fact_id"] for r in out["rows"]}
            failed = []
            if "covered" in expect and out["covered"] != expect["covered"]:
                failed.append(f"covered {out['covered']} != {expect['covered']}")
            failed += [f"missing {f}" for f in expect.get("includes", []) if f not in ids]
            failed += [f"unexpected {f}" for f in expect.get("excludes", []) if f in ids]
            if failed:
                report.expectation_failures += 1
            result = {"id": fx.get("id"), "type": "lookup", "covered": out["covered"], "failed": failed}
        else:
            report.expectation_failures += 1
            result = {"id": fx.get("id"), "type": kind, "failed": [f"unknown fixture type {kind!r}"]}
        report.results.append(result)
    return report
