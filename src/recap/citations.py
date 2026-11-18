"""Harness-side checks for packet-based search suppression (spec section 4).

The model proposes: it may answer a current-state question from the packet
only by citing ``[fN]`` tags. The harness verifies: before a draft that cites
packet lines is released, every cited line must still be valid. Mandatory
lookups (explicit browse requests, high-stakes, live or volatile questions)
stay with the host's own policy layer and are decided before generation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .timeutil import TimeLike

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Recap

REF_RE = re.compile(r"\[(f[1-9][0-9]?)\]")

RELEASE = "release"
LOOKUP = "lookup"


def extract_refs(text: str) -> list[str]:
    """Distinct ``fN`` citation tags in order of first appearance."""
    seen: dict[str, None] = {}
    for m in REF_RE.finditer(text):
        seen.setdefault(m.group(1), None)
    return list(seen)


@dataclass
class CitationCheck:
    ok: bool
    results: dict[str, str] = field(default_factory=dict)


def check_citations(recap: "Recap", packet_id: str, refs: list[str], *, at: TimeLike = None, scope: str | None = None) -> CitationCheck:
    """Validate each cited tag. ``scope``, when given, must match the line's scope."""
    results: dict[str, str] = {}
    for ref in refs:
        resolved = recap.resolve(packet_id, ref, at=at)
        if "error" in resolved:
            results[ref] = resolved["error"]
            continue
        current = resolved["current"]
        if resolved.get("packet_revoked"):
            results[ref] = "packet_revoked"
        elif not current["valid_at_time"]:
            results[ref] = current["reason"] or "invalid"
        elif scope is not None and current["scope"] not in (scope, "global"):
            results[ref] = "out_of_scope"
        else:
            results[ref] = "ok"
    return CitationCheck(ok=bool(refs) and all(r == "ok" for r in results.values()), results=results)


@dataclass
class DraftDecision:
    action: str
    reason: str
    refs: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)


def review_draft(
    recap: "Recap",
    *,
    packet_id: str | None,
    draft: str,
    suppression_allowed: bool,
    at: TimeLike = None,
    scope: str | None = None,
) -> DraftDecision:
    """Decide whether a draft written without a search may be released.

    * No packet citations: release; the draft does not rely on the packet.
      Uncited current-state claims are measured offline by the evaluation gate.
    * Citations while suppression is not allowed (degraded packet, or a host
      without the policy layer): rerun with lookup.
    * Citations that all verify: release.
    * Any invalid citation: discard the draft and rerun with lookup.
    """
    refs = extract_refs(draft)
    if not refs:
        return DraftDecision(RELEASE, "no_packet_citations")
    if packet_id is None:
        return DraftDecision(LOOKUP, "citations_without_packet", refs)
    if not suppression_allowed:
        return DraftDecision(LOOKUP, "suppression_not_allowed", refs)
    check = check_citations(recap, packet_id, refs, at=at, scope=scope)
    if check.ok:
        return DraftDecision(RELEASE, "citations_valid", refs, check.results)
    return DraftDecision(LOOKUP, "invalid_citations", refs, check.results)
