"""A small JSON-over-HTTP front end for hosts that cannot embed the library.

In-process use is preferred: it keeps assembly inside the latency budget and
keeps prompts in the tenant process. The server binds to localhost by default
and has no authentication; put it behind your own access control.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .citations import review_draft
from .runtime import AssembleRequest, Recap

MAX_BODY = 64 * 1024

LOOKUP_TOOL = {
    "name": "recap_lookup",
    "description": (
        "Look up verified facts about a known entity (software releases, deprecations, official status) "
        "from RECAP's index. Returns rows valid now, whether the entity is covered, and when its sources "
        "were last checked. An empty or uncovered result means: use the live search tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "Entity id, for example 'example:lumen'."},
            "predicate": {
                "type": "string",
                "enum": ["release", "release_highlights", "deprecation", "removal", "end_of_support", "status", "observation", "notice"],
            },
            "scope": {"type": "string", "description": "Jurisdiction or scope, for example 'global' or 'US'."},
        },
        "required": ["entity"],
        "additionalProperties": False,
    },
}


def make_handler(recap: Recap) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "recap/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep request bodies and queries out of logs
            return

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY:
                return None
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                return None
            return data if isinstance(data, dict) else None

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/health":
                self._send(200, {"ok": True, "watermark": recap.refresh()})
            elif self.path == "/v1/tools/lookup":
                self._send(200, LOOKUP_TOOL)
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            data = self._body()
            if data is None:
                self._send(400, {"error": "expected a JSON object body under 64 KiB"})
                return
            try:
                if self.path == "/v1/assemble":
                    self._send(200, recap.assemble(AssembleRequest.from_dict(data)).to_dict())
                elif self.path == "/v1/resolve":
                    self._send(200, recap.resolve(data["packet_id"], data["ref"], at=data.get("at"), mode=data.get("mode", "live")))
                elif self.path == "/v1/lookup":
                    allowed = {"key", "entity", "predicate", "scope", "as_of", "mode", "limit"}
                    self._send(200, recap.lookup(**{k: v for k, v in data.items() if k in allowed}))
                elif self.path == "/v1/session":
                    self._send(200, recap.session_status(data["packet_id"], at=data.get("at")))
                elif self.path == "/v1/review_draft":
                    decision = review_draft(
                        recap,
                        packet_id=data.get("packet_id"),
                        draft=str(data.get("draft", "")),
                        suppression_allowed=bool(data.get("suppression_allowed", False)),
                        at=data.get("at"),
                        scope=data.get("scope"),
                    )
                    self._send(200, decision.__dict__)
                else:
                    self._send(404, {"error": "not_found"})
            except (KeyError, TypeError, ValueError) as exc:
                self._send(400, {"error": f"bad request: {exc}"})

    return Handler


def serve(recap: Recap, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create the server; call ``serve_forever()`` on the result."""
    return ThreadingHTTPServer((host, port), make_handler(recap))
