# RECAP

**Recency-aware Context Assembly Protocol.** A small service that gives an LLM a short, verified note about recent changes before a conversation starts.

Models don't know what happened after they were trained. Before a new conversation's first model call, RECAP hands the model at most eight lines about recent changes that matter for that assistant's job, such as a new software release, a deprecation or an official status change. Every line traces to an enrolled primary source and has an expiry. When the model relies on a line, the harness checks the line is still valid before the answer goes out. If the note is stale, uncertain or doesn't cover the question, the assistant searches the way it does today.

```text
<recap as_of="2026-11-18">
[f1] Lumen: legacy Python add-on API deprecated 2026-11-01.
[f2] Lumen 5.3.0 (stable) released 2026-11-10.
[f3] Quill 2.2.0rc1 (prerelease) released 2026-11-01.
</recap>
```

*(Lumen, Quill and Aurora-1 are fictional; the bundled example data uses them.)*

This repository is the reference implementation of the engineering spec in [`docs/SPEC.md`](docs/SPEC.md) (v0.3). Status: **alpha**. The design is complete and tested against fixtures; it has not yet run against production feeds or traffic.

## How it works

```text
OFF PATH   allowlisted sources -> safe fetch -> adapter -> validate -> append-only store
START      profile + model + snapshot -> filter -> rank -> exact selection -> token bound -> packet
ANSWER     cited [fN] tags -> resolve() -> release the draft, or rerun with lookup
ON MISS    lookup() on the index -> your existing live search
```

* **Ingestion is off the request path.** Sources are polled with conditional GETs, under a strict origin allowlist, with private-address blocking, size limits and jittered backoff. Deterministic adapters promote only typed fields. Free text such as release notes goes to a human review queue.
* **Every fact has two clocks and a verification window.** A fact is admitted at time T only if RECAP had observed it by T, it has taken effect (or is a labelled announcement), and an attesting check happened recently enough. Replays reproduce exactly what was known at T.
* **Selection is exact.** Candidates are scored with `u = .40R + .25I + .20A + .15F`, then a small dynamic program picks the subset with the most total utility under the fact cap (8), the per-entity cap (2) and the token budget (≤256).
* **Token counts are local.** Three modes are supported: a guaranteed byte bound, a calibrated bound fitted offline against your provider's counts, or an exact tokenizer you register.
* **Search suppression is verified.** The model may skip a search only by citing packet tags, and the harness checks every cited tag before releasing the answer. Mandatory lookups stay with your own policy layer.

## Install

Python 3.11+. The only runtime dependency is NumPy.

```bash
git clone https://github.com/Parabox0/RECAP.git && cd RECAP
pip install .
```

For development:

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

## Quickstart

The bundled example uses fictional sources on `example.org`, so it runs offline. Fixture files stand in for fetched feeds.

```bash
recap init demo && cd demo
```

```bash
for pair in "lumen-releases lumen-releases.atom" "quill-pypi quill-pypi.json" "aurora-status aurora-status.json" "lumen-policy lumen-policy.html"; do set -- $pair; recap ingest-file --now 2026-11-18T09:00:00Z "$1" "fixtures/$2"; done
```

```bash
recap review list
```

```bash
recap review approve 1 --now 2026-11-18T09:20:00Z --text "native Gaussian-splat import and a faster viewport"
```

```bash
recap assemble --now 2026-11-18T10:00:00Z --profile geo --model example-model --text
```

```text
<recap as_of="2026-11-18">
[f1] Aurora-1 radiometer status scheduled: degraded from 2026-11-25.
[f2] Aurora-1 imager status: safe mode since 2026-11-12.
</recap>
```

```bash
recap lookup --now 2026-11-18T10:00:00Z --entity example:quill --predicate release
```

[`examples/quickstart.py`](examples/quickstart.py) runs the full flow from Python: review approvals, placement, citation checks, and a recall that revokes a packet.

## Using RECAP from a harness

```python
from recap import AssembleRequest, Recap, load_config
from recap.citations import review_draft
from recap.placement import POLICY_INSTRUCTION, prepend_to_first_user_message

recap = Recap(load_config("recap.toml"))   # in-process; keep one per worker

# 1. Before the first model call of a new conversation.
result = recap.assemble(AssembleRequest(
    profile_id="software", profile_version=1, model_id="example-model",
    context_window=200_000,
    base_input_tokens=count_without_packet,     # your own count or an upper bound
    reserved_output_tokens=4_096,
))

# 2. Stable instructions first; the packet goes before the first user message.
system = YOUR_SYSTEM_PROMPT + "\n\n" + POLICY_INSTRUCTION
messages = prepend_to_first_user_message(
    [{"role": "system", "content": system}, {"role": "user", "content": user_text}], result)

# 3. Mandatory lookups (explicit browse requests; high-stakes, live or volatile
#    questions) are decided by your policy layer before generation, as today.

# 4. If the model answered without searching, verify any packet citations.
decision = review_draft(recap, packet_id=result.packet_id, draft=draft_text,
                        suppression_allowed=result.suppression_allowed)
if decision.action == "lookup":
    ...  # discard the draft and rerun the turn with your search tool

# 5. Later turns: when the packet goes stale, tell the model it is historical.
if recap.session_status(result.packet_id)["historical"]:
    ...  # add recap.placement.HISTORICAL_NOTICE and stop treating the packet as current
```

`result.status` is `hit` (packet attached, suppression allowed), `degraded` (packet attached but a source is paused or stale, so it is background only) or `empty`, with a `reason` such as `cold_index`, `low_budget`, `unsupported_model`, `nothing_selected` or `timeout`. An empty result is normal and often correct.

`recap.lookup(entity=..., predicate=...)` is the targeted freshness tool. `recap.server.LOOKUP_TOOL` is a provider-neutral JSON Schema for exposing it to the model.

If you cannot embed the library, `recap serve` exposes the same calls as JSON over HTTP on localhost: `/v1/assemble`, `/v1/resolve`, `/v1/lookup`, `/v1/session` and `/v1/review_draft`. It has no authentication, so put it behind your own access control.

## Running ingestion

```bash
recap poll --loop
```

This polls each source on its own interval with ±10% jitter, ETag/Last-Modified, Retry-After and exponential backoff. Other commands:

| Command | Purpose |
| --- | --- |
| `recap backfill <source>` | Fetch history back to 180 days, following RFC 5005 archive links; clears a gap pause |
| `recap source status` | Health per source: ok, paused, stale or never checked |
| `recap source resume <source>` | Manual sign-off after parser drift or a recall |
| `recap review list / approve / reject` | The review queue for free text and review-class sources |
| `recap recall --fact ID` or `--source ID` | Tombstone facts and revoke every packet that cited them |
| `recap editorial add general <fact_id>` | Designate one of at most two changes for the general profile |

Built-in adapters:

| Adapter | Class | Source |
| --- | --- | --- |
| `atom` | deterministic | Atom or RSS release feeds; version and channel come from a title regex |
| `pypi` | deterministic | PyPI JSON API; yanked releases become retractions |
| `json_status` | deterministic | Structured status feeds with allowlisted status values |
| `review_page` | review | Pages with no structure; text goes to reviewers, who write typed facts |

To add a source type, subclass `recap.ingest.Adapter`, implement a pure `parse()`, and call `register_adapter()`.

## Configuration

`recap init` writes a commented example. The files are:

* `recap.toml`: paths and optional `[settings]` such as `deadline_ms` and `safety_margin_tokens`.
* `sources.toml`: one `[[source]]` per enrolled source. Each sets its exact origin and path prefixes, allowed fact types and jurisdictions, poll interval, maximum evidence age, authority, whether the endpoint attests current validity, and topics. A source removed from this file stops being served.
* `profiles/*.toml`: one manifest per profile version, with topic weights, exact entities, scope, deny list, `min_score`, budget and the prompt versions it is bound to. Unbound prompts get the `general` profile.
* `models.toml`: per model, `knowledge_through` (a conservative training cutoff that anchors the eligibility window), the counting mode and calibration.

Calibrate a model before serving real traffic:

```bash
recap calibrate --samples samples.jsonl --version 2026-11
```

Each line of `samples.jsonl` is `{"packet": "<rendered packet>", "actual": <provider token count>}`. The spec asks for at least 10,000 samples. The fit is reported with zero violations, or the command fails.

## Evaluation and launch gates

RECAP ships the parts of the evaluation that need no model:

* `recap replay --fixtures fixtures.jsonl` runs deterministic fixtures for the hard-failure gate: no expired or lapsed rows injected, no budget or cap overruns, no invalid citation accepted.
* `recap gates --graded graded.jsonl --fixtures-report report.json --net-savings N` computes every ship gate from your blindly graded A/C replay results. It uses a paired cluster bootstrap for correctness and lookups and an exact Clopper–Pearson bound for unsupported claims. The input format is documented in `recap/evaluation/gates.py`.
* `recap holdout-size --p-d 0.05 --design-effect 1.2` sizes a holdout.
* `recap economics ...` gives per-conversation break-even.

Running the A/B/C/D systems and grading their answers is yours to do; the spec's section 5 describes the design.

## Security model

* **Fetching:** exact origin and path allowlists, applied to every redirect hop. All resolved addresses must be public, and the connection is pinned to a checked address. Body, decompression and time limits apply. No proxy support: deployments that need one must enforce the same rules there.
* **Parsing:** XML with a DTD is rejected. Adapters are pure functions over bytes, and URLs inside documents never trigger requests.
* **Packet text:** values are typed and validated. Markup, control and bidi characters are stripped, as are brackets, braces, angle brackets, backticks, pipes and backslashes, so source text cannot close the wrapper or forge a `[fN]` tag. Over-long values are rejected, never truncated.
* **Authority:** the packet is placed as quoted data in a lower-authority position, and `POLICY_INSTRUCTION` tells the model it is data, not instructions.
* **Privacy:** prompts and user messages never enter RECAP. Polling is keyed by public sources. The metrics hook receives counts and fact IDs only.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Performance

`recap bench` builds a synthetic index across 20 sources and 400 entities. Every fact is attested and admissible, and release versions rise over time. On a 4-core container:

| Facts in index | Cold assembly p50 | Cold p95 | Cached p95 |
| --- | --- | --- | --- |
| 10,000 | 5.8 ms | 8.3 ms | 0.01 ms |
| 50,000 | 8.0 ms | 10.2 ms | 0.01 ms |

"Cold" clears the packet cache before every call. A cached packet is reused until the hour bucket ends or any row examined during assembly can change state, whichever comes first.

The spec target is p95 ≤ 20 ms with a 40 ms hard stop.

## Limitations

* Not yet run against real feeds or production traffic. Adapters are tested against fixtures only.
* Each worker process keeps its own in-memory index and refreshes it from SQLite about once a second. One poller process should write to a given database.
* Evidence byte ranges are not recorded yet; evidence is the URL plus a hash of the fetched body.
* No automatic retention purge; schedule your own according to the spec's retention rules.
* Placement helpers are provider-neutral. If your provider has a dedicated untrusted-data field, put `rendered_data` there instead.
* The example calibration values are illustrative. The byte bound is safe but leaves room for only about three lines in the default budget.

## Project layout

```text
src/recap/
  config.py      source registry, profiles, models, settings
  store.py       append-only SQLite store with snapshot watermarks
  index.py       point-in-time views and admission
  ranking.py     candidate pool, disputes, dominance, utility
  select.py      exact selection
  render.py      typed packet lines        sanitize.py  value cleaning
  tokens.py      byte, calibrated and exact counting
  runtime.py     assemble / resolve / lookup / session_status
  citations.py   harness-side citation checks    placement.py  request placement
  admin.py       review, recall, editorial, source sign-off
  ingest/        fetch.py, feeds.py, adapters.py, writer.py, poller.py
  evaluation/    stats.py, gates.py, economics.py, replay.py
  server.py, cli.py, bench.py
docs/SPEC.md     the engineering spec
examples/        runnable walkthrough
tests/           pytest suite
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes that affect behaviour should update `docs/SPEC.md` in the same pull request.

## License

MIT License. See [LICENSE](LICENSE).
