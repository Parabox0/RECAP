# Contributing to RECAP

Thanks for helping. RECAP is a small codebase with strict invariants, so a few rules keep it trustworthy.

## Ground rules

* **The spec is the contract.** `docs/SPEC.md` describes the intended behaviour. If a change alters behaviour, update the spec in the same pull request and add a row to its changes table.
* **Never weaken an invariant to make a test pass.** The hard-failure invariants are: no expired or lapsed rows in a packet, no budget or cap overrun, no invalid citation accepted, nothing served from an unenrolled source, and no free text promoted without review.
* **Determinism matters.** For a given snapshot and time, `assemble` must return the same packet. Iterate sets only after sorting, and do not read the wall clock outside the injected `clock`.
* **Keep the request path local.** `assemble`, `resolve` and `lookup` must never make network calls.
* **Keep prompts out.** Do not add code that receives, logs or stores prompt text or end-user messages.

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

```bash
recap bench --facts 10000 --runs 300
```

Run the benchmark when you touch `index.py`, `ranking.py`, `select.py` or `runtime.py`, and include the numbers in the pull request.

## Adding an adapter

1. Subclass `recap.ingest.Adapter` and set `name` and `adapter_class`.
2. Implement `parse(source, body, *, content_type, url)` as a pure function. It returns `ParsedFact`s for validated typed fields and `ReviewCandidate`s for any free text.
3. Validate every field with `recap.sanitize`. Raise `ParseError` when the document's structure is wrong; the source then pauses for parser drift.
4. Add fixture-based tests covering a normal document, rejected entries, and a revision change.

## Reporting bugs

Open an issue with the RECAP version, a minimal configuration and fixture that reproduce the problem, and the expected and actual packet. Report security issues privately as described in [SECURITY.md](SECURITY.md).
