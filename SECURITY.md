# Security policy

RECAP fetches untrusted documents and places derived text in front of a language model, so security reports are welcome and taken seriously.

## Reporting a vulnerability

Please do not open a public issue. Use the repository host's private vulnerability reporting, or contact the maintainers listed in the repository, with:

* what an attacker controls (a source document, a redirect target, DNS, a request to the HTTP server, ...);
* what they can achieve (SSRF, text injected into a packet, a forged citation, a stale fact served as current, denial of service, ...);
* a minimal reproduction, ideally a failing test.

We aim to acknowledge reports within five working days.

## In scope

* Bypassing the fetch allowlist, private-address blocking or size limits in `recap/ingest/fetch.py`.
* Source-controlled text that survives sanitisation and alters the packet wrapper, citation tags or model instructions.
* XML, JSON or decompression attacks against the adapters.
* A packet that includes a fact that should not be admitted at that time (expired, retracted, lapsed, superseded, from an unenrolled source).
* A citation check that accepts an invalid citation.

## Out of scope

* The bundled HTTP server has no authentication by design and binds to localhost; deploying it on a public interface is a configuration error.
* Trusted operator inputs: configuration files, review decisions, editorial picks.
