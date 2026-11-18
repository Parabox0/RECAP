"""End-to-end RECAP walkthrough on the bundled fictional example data.

Run from the repository root:

    python examples/quickstart.py

It copies the example configuration to a temporary directory, ingests the
fixture feeds as if they had been fetched, approves two review items,
assembles packets, places one in a chat request, checks a draft's citations,
and then recalls a fact to show revocation. Nothing touches the network.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import recap.cli  # noqa: E402
from recap import AssembleRequest, Recap, load_config  # noqa: E402
from recap.admin import approve_review, recall  # noqa: E402
from recap.citations import review_draft  # noqa: E402
from recap.ingest import Poller  # noqa: E402
from recap.placement import POLICY_INSTRUCTION, prepend_to_first_user_message  # noqa: E402
from recap.store import Store  # noqa: E402
from recap.timeutil import parse_time  # noqa: E402

EXAMPLE = Path(recap.cli.__file__).resolve().parent / "_example"


class Clock:
    """A settable clock, so the walkthrough runs on the example's dates."""

    def __init__(self, when: str):
        self.t = parse_time(when)

    def __call__(self) -> int:
        return self.t


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="recap-quickstart-"))
    shutil.copytree(EXAMPLE, root, dirs_exist_ok=True)
    cfg = load_config(root / "recap.toml")
    store = Store(cfg.db_path)
    clock = Clock("2026-11-18T09:00:00Z")

    # 1. Ingestion (normally `recap poll` on a schedule).
    poller = Poller(store, cfg, clock=clock)
    for source_id, name in [
        ("lumen-releases", "lumen-releases.atom"),
        ("quill-pypi", "quill-pypi.json"),
        ("aurora-status", "aurora-status.json"),
        ("lumen-policy", "lumen-policy.html"),
    ]:
        out = poller.ingest_bytes(source_id, (root / "fixtures" / name).read_bytes())
        print(f"ingested {source_id}: {out.stats.new_versions} new rows, {out.stats.reviews_staged} staged for review")

    # 2. Review: free text never reaches a packet without a person approving it.
    for item in store.list_reviews():
        if item["kind"] == "highlights":
            approve_review(store, cfg, item["id"], decided_by="reviewer", now=clock.t,
                           text="native Gaussian-splat import and a faster viewport")
        elif item["kind"] == "page":
            approve_review(store, cfg, item["id"], decided_by="reviewer", now=clock.t, fact={
                "predicate": "deprecation", "entity": "example:lumen", "entity_label": "Lumen",
                "subject": "python-addon-api-legacy", "value": {"item": "legacy Python add-on API"},
                "effective_at": "2026-11-01", "published_at": "2026-11-01",
            })

    # 3. Assembly, on the request path.
    clock.t = parse_time("2026-11-18T10:00:00Z")
    rc = Recap(cfg, store, clock=clock)
    request = AssembleRequest(
        profile_id="software", profile_version=1, model_id="example-model-calibrated",
        context_window=200_000, base_input_tokens=3_000, reserved_output_tokens=4_096,
    )
    result = rc.assemble(request)
    print(f"\nstatus={result.status} tokens<={result.added_input_tokens_max} packet={result.packet_id}")
    print(result.rendered_data)

    # 4. Placement: stable instructions first, the packet before the first user message.
    messages = prepend_to_first_user_message(
        [{"role": "system", "content": "You are a support assistant.\n\n" + POLICY_INSTRUCTION},
         {"role": "user", "content": "Does Lumen still support the old Python add-on API?"}],
        result,
    )
    print(f"\nfirst user message now has {len(messages[1]['content'])} parts")

    # 5. The model answered without searching and cited a line; verify before release.
    draft = "It was deprecated on 2026-11-01 [f1]."
    decision = review_draft(rc, packet_id=result.packet_id, draft=draft, suppression_allowed=result.suppression_allowed)
    print(f"draft decision: {decision.action} ({decision.reason})")
    print("provenance:", rc.resolve(result.packet_id, "f1")["as_packed"]["canonical_url"])

    # 6. Lookup: the targeted freshness tool for questions the packet does not cover.
    found = rc.lookup(entity="example:quill", predicate="release")
    print("\nlookup example:quill:", found["covered"], [row["text"] for row in found["rows"][:2]])

    # 7. Recall: tombstone the cited fact; the packet is revoked and the draft is rejected.
    recall(store, now=clock.t + 60, reason="publisher withdrew the notice", fact_ids=[result.refs["f1"]])
    clock.t += 120
    rc.refresh(force=True)
    decision = review_draft(rc, packet_id=result.packet_id, draft=draft, suppression_allowed=True)
    print(f"after recall: {decision.action} ({decision.reason}); session: {rc.session_status(result.packet_id)['reason']}")
    print(f"\nexample files are in {root}")


if __name__ == "__main__":
    main()
