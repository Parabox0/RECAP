# RECAP
> Recency-aware Context Assembly Protocol · Engineering spec v0.3 · 18 November 2026 · supersedes v0.2
>
> v0.3 records the decisions made while building the reference implementation in this repository. Where the spec and the code disagree, that is a bug in one of them.

RECAP gives an LLM harness a small, verified update before the first model call. It chooses facts based on the assistant's job and on what the model cannot know from training, and it keeps source polling off the request path. A packet can answer what changed in a known release; it cannot prove that nothing newer has shipped.

**In plain terms.** Models don't know what happened after they were trained. Before a new conversation starts, RECAP gives the model a short note of at most eight lines about recent changes that matter for its job, such as a new software release or a deprecation. Every line traces to an official source and has an expiry. If the note is stale, uncertain or doesn't cover the question, the assistant searches the way it does today.

| Default budget | Hard cap | Assembly p95 |
| --- | --- | --- |
| 192 input tokens | 256 input tokens | ≤20 ms |

[Product contract](#1-product-contract) · [Ingestion](#2-ingestion-and-temporal-validity) · [Relevance and packing](#3-relevance-and-packing) · [Integration](#4-runtime-integration) · [Launch gates](#5-evaluation-and-rollout) · [Changes from v0.2](#changes-from-v02)

## 1. Product contract

**Caller and entry point.** Before a new conversation's first model call, the host calls `assemble()` (§4) with the profile, the model, the budget, the time, and the size of the input it is about to send. It gets back either a packet with a citation sidecar or an empty result with a reason. No external request, generation call, or raw web text on this path. Existing sessions use the `lookup()` freshness tool (§4) when needed; never silently preserve an expired first-turn fact as current.

**Decision.** Build an asynchronously updated fact index with local, deterministic packing. A universal world digest spends tokens on events most assistants never discuss. Search on every turn fetches details the assistant may already have. A role-specific packet buys a cheap default only where we can measure repeated demand and verify the answer.

**V1 scope.** English; 10–20 explicitly enrolled primary sources; three profiles: software assistant, Earth-observation assistant, and general assistant. Cover version releases, deprecations, scheduled changes, and a small number of official agency status updates. Facts are promoted automatically only from sources with structured feeds or APIs; HTML-only sources go through review (§2) and are outside the freshness targets. General gets at most two editor-designated changes, and can get zero. Exclude live quotes, weather, availability, and advice based on high-stakes or strongly local facts. No full-world coverage, personal history inference, or claim that a packet makes the base model current.

### Request flow
```
OFF PATH   Allowlisted sources -> normalize -> validate -> versioned index
DEPLOY     Prompt version -> operator manifest -> versioned profile
           Model release  -> knowledge watermark + token calibration
START      Profile + model + index snapshot -> filter -> select -> bound tokens -> attach
ANSWER     Cited fact IDs -> resolve() -> release answer, or fall back to lookup
ON MISS    lookup() on the index -> existing live verification path
```

**Working budget.** Configurable per profile from 0 to 256 tokens (hard cap); 192 by default. At most eight facts. The budget covers the whole added model input as serialized, including wrapper and role overhead, measured against the target model (§3). A request may lower the profile's budget but never raise it, so a profile set to 0 is off. Budget is a ceiling, not a fill target; often the right packet is empty. Provenance served only through the sidecar does not count as injected context.

**Service targets.** Assembly p95 ≤20 ms across eligible requests; stop at 40 ms and return empty. For typed facts from deterministic adapters, aim for 95% of supported software changes queryable within 60 minutes and 95% of agency status changes within six hours of publication, measured only where source timestamps are reliable. Reviewed content (§2) has no freshness target; report its review latency separately. Report missing dates and missed feeds separately; do not use the assembly SLO to imply fresh source data.

---
## 2. Ingestion and temporal validity

**Source registry.** Per source: adapter ID, adapter class (`deterministic` for typed fields from a structured API, Atom/RSS or JSON; `review` for anything that needs extraction), exact origin and permitted paths, allowed fact types and jurisdictions, poll interval, maximum evidence age, parser version, and coverage start. Prefer structured APIs/Atom/RSS over page extraction. One conditional GET per interval (ETag/Last-Modified, respect 429/Retry-After, jitter/backoff); ordinary release sources poll every 30 min, slower agency feeds every 3 h. Record last successful check on 304, but only revalidate a claim when that endpoint attests to the claim's current validity. A static article returning 304 never extends a status TTL. [1, 2]

**Backfill.** At enrollment, backfill to 180 days before the present where the source exposes history (archive, paging or API). Record each source's coverage start; never imply coverage before it.

**Canonical event.** Normalize to one typed proposition per row. Releases use a semver/model identifier and channel, and their key subject is `version@channel`, so the same version on two channels is two events; policy changes use exact scope plus effective time; numeric observations keep units, geography, and reference period. Keep raw response and evidence range outside runtime context.
```
fact_id, key=(entity, predicate, scope), typed_value, qualifiers
event_at, effective_at, valid_to?, published_at, observed_at
checked_at, expires_at, state, supersedes[], source_id, source_revision
origin: deterministic | reviewed
evidence=(canonical_url, blob_sha256, byte_range, parser_version)
state: scheduled | active | retracted | disputed
```
| Clock | Meaning |
| --- | --- |
| `published_at` | Publisher made the evidence available |
| `observed_at` | RECAP first recorded the evidence |
| `effective_at` | Change starts to apply |
| `valid_to` / `expires_at` | Real-world validity ends / RECAP must reverify |
| `source_revision` | Publisher's revision marker (Atom `updated`, API revision) used for ordering |

**Admission at time T.** `effective_at` says when the claim applies; `observed_at` says when this service first knew. Three classes, applied identically in live serving and replay:

| Class | Admitted when |
| --- | --- |
| Active claim | `state = active`; `observed_at ≤ T`; `published_at ≤ T` where dated; `effective_at ≤ T`; T before `valid_to` (if set) and `expires_at` |
| Announcement | `state = scheduled`; `observed_at ≤ T`; `published_at ≤ T` where dated; verified `effective_at > T`; T before `expires_at`. Always rendered with an explicit "scheduled for <date>" label |
| Lapsed schedule | `state = scheduled` and `effective_at ≤ T`. Never admitted. It becomes active only when the source confirms the change, which writes a new active row |

A scheduled change never becomes an active claim just because the clock ticks. State history, including tombstones, is append-only with an atomic index watermark.

**Ordering, deduplication and correction.** Order deliveries by `source_revision`, not by content. A delivery whose revision is not newer than the stored revision for that entry ID is a replay and is ignored, whatever its hash; a newer revision is accepted even when it reverts to earlier content. Revisions come only from a real revision field (Atom `updated`, an API serial); RECAP never derives one from a publication date. Where a source has none, the first change to an entry is accepted as a correction; a return to earlier content, or any later change, goes to review, because without revisions the newer of two contents cannot be told apart. A corrected entry that no longer yields a fact it used to (a fixed version number, say) withdraws that fact. The key source ID + stable entry ID + content hash only dedupes identical deliveries. Resolve agreeing claims by canonical key and value, keeping every independent supporting citation. On explicit correction, supersede (transitively: a correction supersedes whether or not it is itself still current, unless it was retracted); on unresolved disagreement between comparably authoritative sources, mark disputed and omit. For an event key, any two admitted facts that disagree on value or date are a dispute. For a state key, the newest effective *active* value is current (announcements take effect after T and are never current; announcements for the same moment from different sources must agree); another source asserting a different value is a dispute only if it was published after the current value took effect, and is otherwise stale. Recheck release status every 7 days and official status within 48 h by default; an earlier explicit expiry or a source-specific limit wins.

**Gap detection and pauses.** A source pauses on parser drift, outage, a gap, or a recall. A gap is an API sequence break, or a feed whose oldest fetched entry is newer than the last entry seen, with no feed paging to recover the rest. [8] Parser drift is a document that fails to parse, a deterministic feed where no entry matches, or a fetch where at least three entries, and more than half, fail validation. While paused, a source promotes nothing and attests nothing, so its rows age out on their normal schedule rather than all at once. An outage pause (three consecutive failed polls) clears on the next good fetch. A gap pause clears after a backfill. Drift and recall pauses clear only on manual sign-off.

**Promotion and isolation.** Deterministic adapters may promote only validated typed fields: entity, predicate, version or identifier, channel, status, scope, dates, units and values. Free text, such as release highlights or notice summaries, is never promoted automatically. It is parsed in isolation, staged for review, and on approval attached to the existing typed fact as a separate `reviewed` row. A typed fact therefore goes live within the freshness target, and its reviewed highlight may follow later. No direct promotion from a summarizer. Reject private/link-local destinations and redirects outside permitted origins, and bound response bytes, decompression and parsing. Never let URLs in source documents initiate arbitrary requests. Strip markup and control characters; retain only bounded allowlisted values. On recall or source compromise, write tombstones and invalidate packets mentioning those fact IDs.

---
## 3. Relevance and packing

**Profiles are operator manifests.** Each v1 profile is an explicit, operator-owned manifest bound to the prompt versions it serves. A deploy with an unbound prompt version uses the general profile. An offline helper may read a trusted prompt's role statements and tool descriptions and propose manifest edits for an operator to accept. It never runs at request time and never reads user messages, credentials, safety-policy text or quoted examples. Persist `profile_id`, version, ontology version, weights, `min_score` and comparison rules. No exporting prompt text or storing raw prompts in telemetry.
```
{ id:"geo", version:3, prompt_versions:["geo-sys@14","geo-sys@15"],
  topics:{earth_observation:1, climate:0.6},
  entities:["nasa:terra","esa:sentinel-2"], scope:["global","US"],
  deny:["live_weather"], min_score:0.55 }
```

**Model registry.** Per `model_id`: `knowledge_through`, an operator-declared, conservative date taken from the provider's stated training cutoff; the token-counting mode; and the calibration version (below). A model without a supported counting mode gets no packet.

**Eligibility window.** The value lies in what the model does not know, not in what happened this week. Window start = max(as_of − 180 days, `knowledge_through` − 30 days); when `knowledge_through` is unknown, as_of − 30 days. Facts before a source's coverage start are never implied. If the model's cutoff is older than the window, the packet says nothing about the gap. This is a stated limitation, and the packet makes no claim about it.

**Candidate pool.** At a pinned snapshot, every filter runs before any truncation, so ineligible rows can never crowd out eligible ones: admission (§2), eligibility window, scope, deny list, trust, disputed, retracted or outdated state, and redundancy. Redundant rows are dominated releases and restatements. Within an (entity, scope, channel) group, only the highest-version active release (newest first among equal or unparseable versions) and the soonest announcement are worth a line, and a pre-release is also redundant once a stable release of the same or a later version exists. A restatement is a state key whose current value equals the newest earlier value effective by `knowledge_through`. Reviewed highlights follow their release: they stay valid while the release is valid, and selectable while it is selectable. From the survivors, union the up-to-30 most recently published facts for each of the top six topic IDs and the 20 most recent exact entity matches across all profile entities, then dedupe by claim (≤200).
```text
Pinned snapshot → all filters, newest first → ≤200 candidates → one row per claim
                → top 64 by u → exact selection (≤8 facts, ≤ budget) → token bound
```

**Utility.** `u = .40R + .25I + .20A + .15F`, with inputs in [0,1]:
- R = max matched topic weight, or 1 for an exact entity match.
- I = priority fixed per predicate: deprecation or removal = 1, release = .7, routine = .3.
- A = primary publisher (1) or manually corroborated (.75). In v1 every automatically promoted fact is primary, so A is a constant .20 offset; it stays for later source types.
- F = 2^(−age_hours / 168), where age_hours = max(0, T_bucket − `published_at`), using `observed_at` when `published_at` is missing. T_bucket is as_of rounded down to the hour, so results are stable within the hour.

Require R > 0 and u ≥ the profile's `min_score` (default .55; tune on development traces only). The general profile uses editorial selection instead of R and `min_score`: designations are an append-only log inside the snapshot, and the two most recent designations that pass the same filters as ranked rows (validity, window, scope, deny list, redundancy) are used.

**Selection is exact, not greedy.** Take the 64 highest-u candidates (ties: later `effective_at`, then `fact_id`). Choose the subset that maximizes Σu subject to at most eight facts, at most two per entity, and Σ line tokens ≤ effective budget − wrapper tokens. Solve with a dynamic program over (facts used, tokens used), with candidates grouped by entity; the worst case is a few million integer operations. Break ties by fewer tokens, then by a canonical order: arrange candidates entity by entity (entities ordered by their best-ranked candidate), then by rank, and prefer the subset that includes the earlier candidate at the first point of difference. Do not rank by utility per token: under a count cap, that lets short routine lines push out a longer, important one.

**Packing is typed, not abstractive.** Render one self-contained line per fact, with the qualifiers that prevent a wrong answer: product and channel, jurisdiction, unit, status, effective date, "scheduled for" on announcements, and the fact ID. Never shorten by dropping a qualifier or rounding a value; a fact that does not fit is not selected. No shared-date pooling in v1, because exact selection needs line costs to add up. URLs, expiry timestamps and trace data go to the sidecar. Try learned compression later only against a locked qualifier-preservation benchmark. [3]

**Count against the actual model, locally.** Packet cost is computed on the request path without an external call:
- *Byte bound:* for byte-level tokenizers every token covers at least one UTF-8 byte, so the byte count is a guaranteed upper bound. It is safe without calibration but overestimates roughly 3–4×, so packets come out smaller.
- *Exact mode:* the provider publishes a tokenizer that matches production for this model and serializer.
- *Calibrated bound:* otherwise, cost = ⌈α · local_count(line)⌉ + β per line + a wrapper constant. α and β are fitted offline per model and serializer version so that the bound is at least the provider's count on every packet in a calibration set of ≥10,000 rendered packets, then inflated by 5%.

Off the request path, recount 1% of served packets with the provider's counting endpoint (packets hold only public source facts). Disable that model's packets on any exceedance. The host supplies `context_window`, `base_input_tokens` (its own count or upper bound for the framed input without the packet) and `reserved_output_tokens`. Effective budget = min(budget, context_window − base_input_tokens − reserved_output_tokens − 64). If the effective budget is below the wrapper cost, return empty. Record aggregate counts and the selected IDs, never prompt strings.

---
## 4. Runtime integration
```
assemble({ profile_id, profile_version, model_id, serializer_version, budget, as_of,
           context_window, base_input_tokens, reserved_output_tokens,
           mode: "live"|"replay" })
 -> { status: "hit"|"empty"|"degraded", packet_id?, snapshot_id, rendered_data?,
      fact_ids[], added_input_tokens_max, suppression_allowed, cache_until?, reason? }

resolve(packet_id, fact_id, at)
 -> { as_packed: { canonical_url, effective_at, checked_at, expires_at, scope, evidence_hash },
      current:   { state, checked_at, expires_at, superseded_by?, valid_at_time } }

lookup({ key | entity_id + predicate }, as_of)
 -> { rows[], covered, source_status: "ok"|"paused"|"unenrolled", last_checked_at }

cache key:   profile_id+version / snapshot_id / model_id+serializer / effective budget / T_bucket
cache_until: min(end of T_bucket, earliest time any row examined during assembly can change state,
                 earliest time a profile source can turn stale)
replay:      bypass live cache and the deadline; `current` and lookup() answer from the pinned snapshot at `at`
packet_id:   derived from the cache key and the selected fact IDs, so identical inputs give the same packet
```

Lines carry tags `f1`…`f8` in rank order. The sidecar keeps, per tag, the fact ID, the rendered line, evidence URL and hash, the verification window and any supporting citations from agreeing sources.

**Status.**
- `hit`: packet attached; search suppression allowed.
- `degraded`: packet attached, but a source tagged to the profile's topics or entities is paused, never checked, or past its maximum evidence age. Its rows age out on their normal schedule. `suppression_allowed` is false, so the packet is background only.
- `empty`: no packet. The reason is one of cold index, timeout, empty profile, malformed row, unsupported model, low budget, or nothing selected.

**Runtime placement.** Put stable instructions and tool definitions first. After them, before the initial user message, attach the packet in the provider's supported untrusted data/context field. Where no separate field exists, use an explicitly marked quoted data section in a lower-authority message and enforce the same rules in a stable developer instruction. If placement and authority cannot be enforced, omit the packet. Do not splice source output into system instructions. Cache only until `cache_until`; on a recall, purge by fact ID and force any later claim about it through lookup. Track actual cached and uncached tokens; the position alone does not guarantee a cache hit. [4, 5]

**Worked example (illustrative values).** A graphics-support profile may include `[f7] Blender 5.3.0 (stable) released 2026-11-10.` If a reviewer has approved the release highlights, a second row may follow: `[f8] Blender 5.3.0 highlights (reviewed): native Gaussian-splat import.` The sidecar binds each ID to the Blender Foundation release and its verification window. A general inbox assistant gets no Blender row. [6]

| Question | Decision |
| --- | --- |
| What shipped in Blender 5.3? | Cite `f8` if present and valid; otherwise `lookup()`, then the live path. |
| When was Blender 5.3 released? | Cite `f7` after `resolve()` confirms it is still valid. |
| What's the latest Blender version today? | Check the current stable release channel. The release event alone cannot answer this. |

**Search suppression: the model proposes, the harness verifies.**
1. Mandatory lookups stay with the host's existing policy layer: explicit requests to browse, and high-stakes, live or volatile queries. RECAP never overrides that layer and adds no classifier on user messages.
2. Otherwise, the model may answer a current-state question from the packet only by citing fact IDs. Before the answer is released, the harness calls `resolve()` for each cited ID. If any is expired, retracted, disputed, superseded or out of scope, the draft is discarded and the turn reruns with lookup.
3. Each claim needs its own valid citation. Missing, ambiguous or wrong-scope coverage triggers `lookup()`, and then the existing live path. When lookup is unavailable, say what was last verified and do not assert current status.
4. If the host lacks the policy layer in (1) or the citation check in (2), or the status is `degraded`, the packet is background only. The profile should then usually stay off, since the savings come from skipped lookups (§5).

Uncited current-state claims cannot be caught at runtime. The unsupported-claims gate (§5) measures them.

**Long sessions.** After the earliest packet expiry, or on snapshot revocation, mark the initial packet context historical in an authoritative runtime signal. Citation checks use `resolve().current`, so a retraction after the first turn blocks any answer that cites the retracted fact. Model recollection alone never suppresses lookup. If the host cannot invalidate decision state reliably, disable packet-based suppression after the first turn. Do not inject a growing new digest each turn.

**Failure and privacy.** On cold index, timeout, empty profile, malformed row, unsupported model or low budget, pass no packet and keep ordinary tools available. Prompt and user data stay in the tenant process; feed polling is keyed by public sources, not user activity. Store normalized snapshots and fetched evidence for 90 days for replay where permitted. Snapshots and evidence pinned by a frozen evaluation set are kept for that set's life; they contain only public source material. Keep operational metrics for 30 days. Keep neither raw prompts nor raw end-user queries in shared logs. API credentials and source blobs are never in the packet.

---
## 5. Evaluation and rollout

**Replay design.** Build a shared 300-trace development set. Then, for each launch profile, freeze a chronological holdout of at least 600 traces, 100 each in six strata: static factual, slow-moving, fast-moving, false premise, conflicting/missing, and irrelevant. Size it with the rule below. Add 300 independent must-lookup challenge cases per profile, covering expiry, retraction, lapsed schedules, jurisdiction mismatch, explicit search and degraded status. Split by event/key and time, not paraphrase. Each trace includes time T, profile, model, gold claim set, admissible evidence as known at T, and the expected answer/refresh/abstain action. Rotate a prospective holdout monthly. No future evidence in index or tools. [7]

**Comparisons.** Hold model, decoding settings, output limits and replay corpus constant across: A incumbent on-demand lookup; B always-search reference; C packet with ordinary lookup, suppressible only through verified citations; D untargeted packet at the same budget. Blindly grade claim correctness, evidence and lookup compliance together; double-review disagreements. Cluster bootstrap by event with 95% intervals, and report per stratum. The unit for unsupported claims is the trace: a trace fails if its answer contains any current-state claim not supported by valid evidence at T. Sample live traffic only with appropriate consent; log bucketed relevance and lookup reason.

**Sizing the holdout.** On the development set, measure p_d, the share of traces where A and C disagree on correctness, and d, the design effect from clustering by event. Freeze n ≥ max(600, ⌈19,600 · p_d · d⌉) traces per profile. This gives about 80% power for the correctness gate when the true difference is zero (19,600 = (2.80 / 0.02)²). At n = 600, the claims gate allows at most one failing trace: the one-sided 95% upper bound is about 0.79% with one failure and about 1.05% with two.

### Ship gates

These are proposed thresholds, not measured results. Apply them per launch profile.

| Measure | Pass condition |
| --- | --- |
| Correctness vs. A | Lower bound of paired 95% interval ≥ −2 percentage points |
| Avoidable lookups vs. A | ≥20% point-estimate reduction; 95% lower bound >0 |
| Unsupported current claims | One-sided 95% upper bound on failing-trace rate ≤1% |
| Hard failures | Zero expired or lapsed rows injected, budget overruns (including token-bound exceedances), uncaught invalid citations, or missed mandatory lookups on dedicated fixtures |
| Latency | ≤50 ms added p95 latency on non-search first turns |
| Economics | Positive net savings per eligible conversation |

Zero failures in 300 independent cases still leaves an approximately 1% one-sided 95% upper bound. If a profile lacks enough data to clear a gate, leave it off.

**Economics, per conversation.** The packet and any search result both stay in context for the rest of the conversation, so compare them over the whole conversation rather than one turn. For a conversation of N turns with a search at turn k:
```
packet_cost = P·c_u + P·c_c·(N−1) + lost_cache_savings + amortized_pipeline
search_cost = fee + S·c_u + S·c_c·(N−k) + follow_up_calls
ship only if  E[avoided search_cost per eligible conversation] > packet_cost
```
P and S are packet and search-result tokens, and c_u and c_c are the uncached and cached input rates. For example, take P = 240 token-equivalents, N = 10 turns, and cached input at 10% of the uncached rate: packet_cost ≈ 240 + 216 = 456. A first-turn search returning S = 1,500 tokens, with fees worth 500, gives search_cost ≈ 2,000 + 1,350 = 3,350. The packet must then prevent such a search in more than about 14% of eligible conversations. Token-equivalents convert fees and cache effects at the uncached input rate; these figures are illustrative. A low-coverage profile should default to no packet.

**Build sequence.** Weeks 1–2: three profile manifests, model registry and token calibration for launch models, two typed sources with backfill, temporal replay harness, and cap and token-bound gates. Weeks 3–4: structured agency adapter, review queue, recall and retraction tests, host citation check, three-profile replay and shadow mode. Then a 5% canary with per-profile kill switches. Disable a profile on a hard invariant failure, a token-bound exceedance in the audit, inadequate sample size for the gates, or stale sources. Grow source coverage only after a profile shows enough eligible demand to justify it.

## Changes from v0.2

| Area | v0.2 | v0.3 |
| --- | --- | --- |
| Release key | (entity, predicate, scope, version) | Subject is `version@channel`; a beta and a stable build of one version are distinct |
| Pre-releases | Newest per channel | Also dominated by a stable release at least as new |
| Token counting | Exact or calibrated | Adds a byte bound, safe without calibration |
| Tie-break | Smallest sorted list of fact IDs | Canonical entity-grouped order, computed exactly by the DP |
| Sources without revisions | Any change after a correction goes to review | Only a return to previously seen content goes to review |
| State disputes | "Comparably authoritative sources disagree" | Precise rule based on publication after the current value took effect |
| Pauses | Paused sources' rows "already excluded" | Rows age out on schedule; pause kinds and how each clears are defined |
| Replay | Bypasses cache | Also bypasses the deadline, so replays are deterministic |
| Editorial picks | Unspecified storage | Append-only log in the snapshot; newest two picks that pass every filter |
| Release dominance | Newest by date | Highest version; cross-channel only for the same or a later version |
| Filter order | Some checks after truncation | Every check, including disputes and redundancy, before truncation |
| Revisions | Fallback to publication dates | Only real revision fields; corrected entries withdraw facts they no longer yield |
| Supersession | Direct | Transitive |
| State keys | Announcements could count as current | Only active rows are current |
| Response cache | Bounded by selected rows | Bounded by every row examined and by source health |
| Budget | Request could raise it | Request may only lower the profile's budget |

## Changes from v0.1 to v0.2

| Area | v0.1 | v0.2 |
| --- | --- | --- |
| Selection | Greedy by utility per token | Exact selection maximizing total utility under count, entity and token limits |
| Token counting | Count full input with and without packet, inputs not passed in | Host passes headroom inputs; local exact or calibrated upper bound; off-path audit |
| Freshness targets | All sources | Typed facts from deterministic adapters only; reviewed text has no target |
| Free text | Implicitly in packet lines | Only as separately reviewed rows |
| Admission | `effective ≤ T` contradicted announcements | Three classes: active, announcement, lapsed schedule |
| Candidates | Truncated, then filtered | Filtered in the index query, then truncated |
| Lookback | 30 days of wall-clock time | Anchored to the model's `knowledge_through`, at most 180 days |
| Threshold | Fixed u ≥ .45 alongside profile `min_score` | Profile `min_score` only, default .55 |
| Freshness age | Clock unspecified | `published_at`, rounded to the hour |
| Profiles | Prompt compiler at deploy | Operator manifests; compiler becomes an offline suggestion tool |
| `resolve()` | One view | As-packed and current views; new `lookup()` tool |
| Search suppression | Mechanism unspecified | Host policy for mandatory lookups; harness-verified citations |
| `degraded` | Undefined | Defined; never allows suppression |
| Cache | No time in key | Hour bucket in key; `cache_until` bound |
| Ship gates | Holdout scope and power unstated | Per-profile holdout, sizing rule, trace-level claims unit |
| Economics | Per turn | Per conversation |
| Replay ordering | Content hash | Source revision; hash only for identical deliveries |
| Feed gaps | "Missing sequence" | Gap rule for feeds without sequence numbers |
| Retention | 90 days | 90 days, plus an exemption for frozen evaluation sets |

## References

1. [HTTP conditional requests, RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#section-13)
2. [Atom feed identity and timestamps, RFC 4287](https://www.rfc-editor.org/rfc/rfc4287.html)
3. [LLMLingua-2, ACL 2024](https://aclanthology.org/2024.findings-acl.57/)
4. [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
5. [OpenAI Model Spec: untrusted data](https://model-spec.openai.com/2025-09-12.html)
6. [Blender 5.3 release](https://www.blender.org/press/blender-5-3-release/)
7. [FreshLLMs / FreshQA, ACL 2024](https://aclanthology.org/2024.findings-acl.813/)
8. [Feed paging and archiving, RFC 5005](https://www.rfc-editor.org/rfc/rfc5005.html)
