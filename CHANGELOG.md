# Changelog

## 0.1.0 (unreleased)

First public version: reference implementation of spec v0.3.

* Append-only SQLite store with snapshot watermarks and point-in-time replay.
* Admission classes (active, announcement, lapsed schedule), attestation-based verification windows, supersession, disputes, dominance and restatement filters.
* Exact packet selection under fact, per-entity and token caps.
* Byte-bound, calibrated and exact token counting, plus an offline calibration fitter.
* `assemble`, `resolve`, `lookup` and `session_status`; harness-side citation checks; provider-neutral placement helpers.
* Safe fetcher, polling with backoff, and gap and drift detection; Atom/RSS, PyPI, JSON status and review-page adapters; review queue and recall.
* Evaluation helpers: deterministic fixtures, gate statistics, holdout sizing and per-conversation economics.
* CLI, localhost JSON server and a synthetic latency benchmark.
* Hardening from an independent review, each covered by a regression test in `tests/test_regressions.py`:
  * state keys never treat announcements as current;
  * all filters run before truncation;
  * releases dominate by version;
  * supersession is transitive;
  * revisions are never invented from publication dates;
  * corrected entries withdraw stale facts;
  * a backfill must close the gap before a source resumes;
  * the cache is bounded by every row examined;
  * the sanitizer strips invisible and lookalike characters;
  * DTDs are rejected in any encoding;
  * IPv6 forms that embed IPv4 are blocked;
  * slow bodies are bounded by the total time limit.
