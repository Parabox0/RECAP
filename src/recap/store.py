"""SQLite storage.

Snapshot-relevant history (fact versions, checks, attestations, source
events, editorial picks and packet revocations) is append-only and shares one
global sequence counter. A write batch commits atomically and advances the
index watermark, so a reader pinned to a watermark sees whole batches only.

Attestation intervals are the one exception to strict append-only storage:
their ``last_seq`` grows in place, which is safe because readers clamp it to
their pinned watermark.

Operational state (poll schedule, entry revisions, review queue, packets) is
mutable and is not part of a snapshot.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from .model import Evidence, FactVersion, State, canonical_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commits (seq_end INTEGER PRIMARY KEY, committed_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fact_versions (
  seq INTEGER PRIMARY KEY,
  fact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_label TEXT NOT NULL,
  predicate TEXT NOT NULL,
  scope TEXT NOT NULL,
  subject TEXT NOT NULL,
  value_json TEXT NOT NULL,
  qualifiers_json TEXT NOT NULL,
  event_at INTEGER,
  effective_at INTEGER,
  valid_to INTEGER,
  published_at INTEGER,
  observed_at INTEGER NOT NULL,
  explicit_expires_at INTEGER,
  state TEXT NOT NULL,
  supersedes_json TEXT NOT NULL,
  source_revision INTEGER,
  content_hash TEXT NOT NULL,
  origin TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  topics_json TEXT NOT NULL,
  authority TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fact_versions_fact ON fact_versions(fact_id, seq);
CREATE INDEX IF NOT EXISTS fact_versions_source ON fact_versions(source_id, seq);
CREATE TABLE IF NOT EXISTS checks (
  seq INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  at INTEGER NOT NULL,
  http_status INTEGER,
  content_sha TEXT,
  attests INTEGER NOT NULL,
  outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checks_source ON checks(source_id, seq);
CREATE TABLE IF NOT EXISTS attestations (
  fact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  first_seq INTEGER NOT NULL,
  last_seq INTEGER NOT NULL,
  PRIMARY KEY (fact_id, source_id, first_seq)
);
CREATE INDEX IF NOT EXISTS attestations_last ON attestations(last_seq);
CREATE INDEX IF NOT EXISTS attestations_source ON attestations(source_id, last_seq);
CREATE TABLE IF NOT EXISTS source_events (
  seq INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  at INTEGER NOT NULL,
  status TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS editorial (
  seq INTEGER PRIMARY KEY,
  profile_id TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  action TEXT NOT NULL,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS packet_revocations (
  seq INTEGER PRIMARY KEY,
  packet_id TEXT NOT NULL,
  at INTEGER NOT NULL,
  reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry_state (
  source_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  revision INTEGER,
  entry_hash TEXT NOT NULL,
  PRIMARY KEY (source_id, entry_id)
);
CREATE TABLE IF NOT EXISTS entry_history (
  source_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  entry_hash TEXT NOT NULL,
  first_seen INTEGER NOT NULL,
  PRIMARY KEY (source_id, entry_id, entry_hash)
);
CREATE TABLE IF NOT EXISTS source_runtime (
  source_id TEXT PRIMARY KEY,
  data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  target_fact_id TEXT,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  text_sha TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  status TEXT NOT NULL,
  decided_at INTEGER,
  decided_by TEXT,
  final_text TEXT,
  note TEXT,
  UNIQUE (source_id, entry_id, kind, text_sha)
);
CREATE TABLE IF NOT EXISTS packets (
  packet_id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  snapshot_id INTEGER NOT NULL,
  record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packet_facts (
  packet_id TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  PRIMARY KEY (packet_id, fact_id)
);
CREATE INDEX IF NOT EXISTS packet_facts_fact ON packet_facts(fact_id);
"""


def _row_to_version(row: sqlite3.Row) -> FactVersion:
    return FactVersion(
        seq=row["seq"],
        fact_id=row["fact_id"],
        source_id=row["source_id"],
        entry_id=row["entry_id"],
        entity=row["entity"],
        entity_label=row["entity_label"],
        predicate=row["predicate"],
        scope=row["scope"],
        subject=row["subject"],
        value=json.loads(row["value_json"]),
        qualifiers=json.loads(row["qualifiers_json"]),
        event_at=row["event_at"],
        effective_at=row["effective_at"],
        valid_to=row["valid_to"],
        published_at=row["published_at"],
        observed_at=row["observed_at"],
        explicit_expires_at=row["explicit_expires_at"],
        state=State(row["state"]),
        supersedes=tuple(json.loads(row["supersedes_json"])),
        source_revision=row["source_revision"],
        content_hash=row["content_hash"],
        origin=row["origin"],
        evidence=Evidence.from_dict(json.loads(row["evidence_json"])),
        topics=tuple(json.loads(row["topics_json"])),
        authority=row["authority"],
    )


@dataclass
class SourceRuntime:
    """Mutable per-source poll state."""

    source_id: str
    etag: str | None = None
    last_modified: str | None = None
    next_poll_at: int | None = None
    failures: int = 0
    last_ok_check_seq: int | None = None
    newest_seen: int | None = None
    last_sequence: int | None = None
    oldest_backfilled: int | None = None

    def to_json(self) -> str:
        data = dict(self.__dict__)
        data.pop("source_id")
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, source_id: str, text: str | None) -> "SourceRuntime":
        rt = cls(source_id=source_id)
        if text:
            for k, v in json.loads(text).items():
                if hasattr(rt, k):
                    setattr(rt, k, v)
        return rt


@dataclass
class Delta:
    """Snapshot-relevant rows with ``since < seq <= upto``."""

    upto: int
    versions: list[FactVersion] = field(default_factory=list)
    checks: list[tuple[int, str, int, str]] = field(default_factory=list)
    attestations: list[tuple[str, str, int, int]] = field(default_factory=list)
    source_events: list[tuple[int, str, int, str, str | None]] = field(default_factory=list)
    editorial: list[tuple[int, str, str, str, int]] = field(default_factory=list)
    revocations: list[tuple[int, str, int, str]] = field(default_factory=list)


class Batch:
    """An open write transaction. Created by :meth:`Store.batch`."""

    def __init__(self, store: "Store", cur: sqlite3.Cursor, now: int, start_seq: int):
        self.store = store
        self.cur = cur
        self.now = now
        self.start_seq = start_seq
        self.last_seq = start_seq

    def next_seq(self) -> int:
        self.last_seq += 1
        return self.last_seq

    # -- snapshot-relevant writes -------------------------------------------------

    def add_fact_version(self, row: Mapping[str, Any]) -> FactVersion:
        seq = self.next_seq()
        state = row["state"].value if isinstance(row["state"], State) else str(row["state"])
        evidence = row["evidence"]
        if isinstance(evidence, Evidence):
            evidence = evidence.to_dict()
        self.cur.execute(
            """INSERT INTO fact_versions (seq, fact_id, source_id, entry_id, entity, entity_label,
                 predicate, scope, subject, value_json, qualifiers_json, event_at, effective_at,
                 valid_to, published_at, observed_at, explicit_expires_at, state, supersedes_json,
                 source_revision, content_hash, origin, evidence_json, topics_json, authority)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                seq,
                row["fact_id"],
                row["source_id"],
                row["entry_id"],
                row["entity"],
                row["entity_label"],
                row["predicate"],
                row["scope"],
                row["subject"],
                canonical_json(row.get("value") or {}),
                canonical_json(row.get("qualifiers") or {}),
                row.get("event_at"),
                row.get("effective_at"),
                row.get("valid_to"),
                row.get("published_at"),
                row.get("observed_at", self.now),
                row.get("explicit_expires_at"),
                state,
                canonical_json(list(row.get("supersedes") or ())),
                row.get("source_revision"),
                row["content_hash"],
                row["origin"],
                canonical_json(evidence),
                canonical_json(list(row.get("topics") or ())),
                row["authority"],
            ),
        )
        found = self.cur.execute("SELECT * FROM fact_versions WHERE seq = ?", (seq,)).fetchone()
        return _row_to_version(found)

    def add_check(self, source_id: str, http_status: int | None, content_sha: str | None, attests: bool, outcome: str) -> int:
        seq = self.next_seq()
        self.cur.execute(
            "INSERT INTO checks (seq, source_id, at, http_status, content_sha, attests, outcome) VALUES (?,?,?,?,?,?,?)",
            (seq, source_id, self.now, http_status, content_sha, int(attests), outcome),
        )
        return seq

    def extend_attestations(self, source_id: str, fact_ids: list[str], prev_check_seq: int | None, check_seq: int) -> None:
        """Mark ``fact_ids`` as attested by check ``check_seq``.

        An interval continues when the fact was attested by the previous good
        check of the same source; otherwise a new interval starts.
        """
        for fact_id in sorted(set(fact_ids)):
            updated = 0
            if prev_check_seq is not None:
                updated = self.cur.execute(
                    "UPDATE attestations SET last_seq = ? WHERE fact_id = ? AND source_id = ? AND last_seq = ?",
                    (check_seq, fact_id, source_id, prev_check_seq),
                ).rowcount
            if not updated:
                self.cur.execute(
                    "INSERT OR IGNORE INTO attestations (fact_id, source_id, first_seq, last_seq) VALUES (?,?,?,?)",
                    (fact_id, source_id, check_seq, check_seq),
                )

    def extend_all_attestations(self, source_id: str, prev_check_seq: int, check_seq: int) -> int:
        """A 304 from an attesting endpoint re-attests everything the last good check did."""
        return self.cur.execute(
            "UPDATE attestations SET last_seq = ? WHERE source_id = ? AND last_seq = ?",
            (check_seq, source_id, prev_check_seq),
        ).rowcount

    def add_source_event(self, source_id: str, status: str, reason: str | None = None) -> int:
        seq = self.next_seq()
        self.cur.execute(
            "INSERT INTO source_events (seq, source_id, at, status, reason) VALUES (?,?,?,?,?)",
            (seq, source_id, self.now, status, reason),
        )
        return seq

    def add_editorial(self, profile_id: str, fact_id: str, action: str) -> int:
        if action not in ("add", "remove"):
            raise ValueError("editorial action must be 'add' or 'remove'")
        seq = self.next_seq()
        self.cur.execute(
            "INSERT INTO editorial (seq, profile_id, fact_id, action, at) VALUES (?,?,?,?,?)",
            (seq, profile_id, fact_id, action, self.now),
        )
        return seq

    def add_revocation(self, packet_id: str, reason: str) -> int:
        seq = self.next_seq()
        self.cur.execute(
            "INSERT INTO packet_revocations (seq, packet_id, at, reason) VALUES (?,?,?,?)",
            (seq, packet_id, self.now, reason),
        )
        return seq

    # -- operational writes inside the same transaction ---------------------------

    def get_entry_state(self, source_id: str, entry_id: str) -> tuple[int | None, str] | None:
        row = self.cur.execute(
            "SELECT revision, entry_hash FROM entry_state WHERE source_id = ? AND entry_id = ?",
            (source_id, entry_id),
        ).fetchone()
        return (row["revision"], row["entry_hash"]) if row else None

    def set_entry_state(self, source_id: str, entry_id: str, revision: int | None, entry_hash: str) -> None:
        self.cur.execute(
            "INSERT INTO entry_state (source_id, entry_id, revision, entry_hash) VALUES (?,?,?,?) "
            "ON CONFLICT(source_id, entry_id) DO UPDATE SET revision = excluded.revision, entry_hash = excluded.entry_hash",
            (source_id, entry_id, revision, entry_hash),
        )
        self.cur.execute(
            "INSERT OR IGNORE INTO entry_history (source_id, entry_id, entry_hash, first_seen) VALUES (?,?,?,?)",
            (source_id, entry_id, entry_hash, self.now),
        )

    def entry_hash_seen(self, source_id: str, entry_id: str, entry_hash: str) -> bool:
        row = self.cur.execute(
            "SELECT 1 FROM entry_history WHERE source_id = ? AND entry_id = ? AND entry_hash = ?",
            (source_id, entry_id, entry_hash),
        ).fetchone()
        return row is not None

    def latest_versions_for_entry(self, source_id: str, entry_id: str) -> list[FactVersion]:
        rows = self.cur.execute(
            """SELECT * FROM fact_versions fv WHERE source_id = ? AND entry_id = ? AND seq = (
                 SELECT MAX(seq) FROM fact_versions WHERE fact_id = fv.fact_id)""",
            (source_id, entry_id),
        ).fetchall()
        return [_row_to_version(r) for r in rows]

    def entry_hash_count(self, source_id: str, entry_id: str) -> int:
        row = self.cur.execute(
            "SELECT COUNT(*) AS n FROM entry_history WHERE source_id = ? AND entry_id = ?", (source_id, entry_id)
        ).fetchone()
        return int(row["n"])

    def latest_version(self, fact_id: str) -> FactVersion | None:
        row = self.cur.execute(
            "SELECT * FROM fact_versions WHERE fact_id = ? ORDER BY seq DESC LIMIT 1", (fact_id,)
        ).fetchone()
        return _row_to_version(row) if row else None

    def add_review(self, source_id: str, entry_id: str, target_fact_id: str | None, kind: str, text: str, text_sha: str) -> bool:
        cur = self.cur.execute(
            "INSERT OR IGNORE INTO review_queue (source_id, entry_id, target_fact_id, kind, text, text_sha, created_at, status) "
            "VALUES (?,?,?,?,?,?,?, 'pending')",
            (source_id, entry_id, target_fact_id, kind, text, text_sha, self.now),
        )
        return cur.rowcount > 0

    def set_runtime(self, rt: SourceRuntime) -> None:
        self.cur.execute(
            "INSERT INTO source_runtime (source_id, data_json) VALUES (?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET data_json = excluded.data_json",
            (rt.source_id, rt.to_json()),
        )

    def decide_review(self, review_id: int, status: str, decided_by: str, final_text: str | None, note: str | None) -> None:
        self.cur.execute(
            "UPDATE review_queue SET status = ?, decided_at = ?, decided_by = ?, final_text = ?, note = ? WHERE id = ?",
            (status, self.now, decided_by, final_text, note, review_id),
        )


class Store:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def batch(self, now: int) -> Iterator[Batch]:
        """Open an atomic write. The watermark advances only if rows were added."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute("SELECT value FROM meta WHERE key = 'last_seq'").fetchone()
                start = int(row["value"]) if row else 0
                batch = Batch(self, cur, int(now), start)
                yield batch
                if batch.last_seq > start:
                    cur.execute(
                        "INSERT INTO meta (key, value) VALUES ('last_seq', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (str(batch.last_seq),),
                    )
                    cur.execute(
                        "INSERT INTO commits (seq_end, committed_at) VALUES (?, ?)",
                        (batch.last_seq, int(now)),
                    )
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise

    # -- reads ---------------------------------------------------------------------

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def watermark(self) -> int:
        rows = self._query("SELECT value FROM meta WHERE key = 'last_seq'")
        return int(rows[0]["value"]) if rows else 0

    def snapshot_for_time(self, t: int) -> int:
        """The newest committed watermark whose commit time is at or before ``t``."""
        rows = self._query(
            "SELECT seq_end FROM commits WHERE committed_at <= ? ORDER BY seq_end DESC LIMIT 1", (int(t),)
        )
        return int(rows[0]["seq_end"]) if rows else 0

    def load_since(self, since: int, upto: int | None = None) -> Delta:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                return self._load_since(since, upto)
            finally:
                self._conn.execute("COMMIT")

    def _load_since(self, since: int, upto: int | None) -> Delta:
        if upto is None:
            upto = self.watermark()
        delta = Delta(upto=upto)
        c = self._conn
        delta.versions = [
            _row_to_version(r)
            for r in c.execute(
                "SELECT * FROM fact_versions WHERE seq > ? AND seq <= ? ORDER BY seq", (since, upto)
            )
        ]
        delta.checks = [
            (r["seq"], r["source_id"], r["at"], r["outcome"])
            for r in c.execute("SELECT seq, source_id, at, outcome FROM checks WHERE seq > ? AND seq <= ? ORDER BY seq", (since, upto))
        ]
        delta.attestations = [
            (r["fact_id"], r["source_id"], r["first_seq"], r["last_seq"])
            for r in c.execute(
                "SELECT fact_id, source_id, first_seq, last_seq FROM attestations WHERE last_seq > ? AND first_seq <= ?",
                (since, upto),
            )
        ]
        delta.source_events = [
            (r["seq"], r["source_id"], r["at"], r["status"], r["reason"])
            for r in c.execute("SELECT * FROM source_events WHERE seq > ? AND seq <= ? ORDER BY seq", (since, upto))
        ]
        delta.editorial = [
            (r["seq"], r["profile_id"], r["fact_id"], r["action"], r["at"])
            for r in c.execute("SELECT * FROM editorial WHERE seq > ? AND seq <= ? ORDER BY seq", (since, upto))
        ]
        delta.revocations = [
            (r["seq"], r["packet_id"], r["at"], r["reason"])
            for r in c.execute("SELECT * FROM packet_revocations WHERE seq > ? AND seq <= ? ORDER BY seq", (since, upto))
        ]
        return delta

    def latest_version(self, fact_id: str) -> FactVersion | None:
        rows = self._query("SELECT * FROM fact_versions WHERE fact_id = ? ORDER BY seq DESC LIMIT 1", (fact_id,))
        return _row_to_version(rows[0]) if rows else None

    def latest_versions_for_source(self, source_id: str) -> list[FactVersion]:
        rows = self._query(
            """SELECT * FROM fact_versions fv WHERE source_id = ? AND seq = (
                 SELECT MAX(seq) FROM fact_versions WHERE fact_id = fv.fact_id) ORDER BY seq""",
            (source_id,),
        )
        return [_row_to_version(r) for r in rows]

    def list_latest_versions(self, entity: str | None = None, limit: int = 200) -> list[FactVersion]:
        sql = """SELECT * FROM fact_versions fv WHERE seq = (
                   SELECT MAX(seq) FROM fact_versions WHERE fact_id = fv.fact_id)"""
        params: tuple = ()
        if entity:
            sql += " AND entity = ?"
            params = (entity,)
        sql += " ORDER BY seq DESC LIMIT ?"
        return [_row_to_version(r) for r in self._query(sql, params + (limit,))]

    def latest_source_event(self, source_id: str) -> tuple[str, str | None] | None:
        rows = self._query(
            "SELECT status, reason FROM source_events WHERE source_id = ? ORDER BY seq DESC LIMIT 1", (source_id,)
        )
        return (rows[0]["status"], rows[0]["reason"]) if rows else None

    def get_runtime(self, source_id: str) -> SourceRuntime:
        rows = self._query("SELECT data_json FROM source_runtime WHERE source_id = ?", (source_id,))
        return SourceRuntime.from_json(source_id, rows[0]["data_json"] if rows else None)

    def set_runtime(self, rt: SourceRuntime) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_runtime (source_id, data_json) VALUES (?, ?) "
                "ON CONFLICT(source_id) DO UPDATE SET data_json = excluded.data_json",
                (rt.source_id, rt.to_json()),
            )

    # -- review queue ----------------------------------------------------------------

    def list_reviews(self, status: str | None = "pending") -> list[dict[str, Any]]:
        if status:
            rows = self._query("SELECT * FROM review_queue WHERE status = ? ORDER BY id", (status,))
        else:
            rows = self._query("SELECT * FROM review_queue ORDER BY id")
        return [dict(r) for r in rows]

    def get_review(self, review_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM review_queue WHERE id = ?", (review_id,))
        return dict(rows[0]) if rows else None

    # -- packets -----------------------------------------------------------------------

    def put_packet(self, packet_id: str, created_at: int, snapshot_id: int, record: Mapping[str, Any], fact_ids: list[str]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO packets (packet_id, created_at, snapshot_id, record_json) VALUES (?,?,?,?)",
                    (packet_id, created_at, snapshot_id, json.dumps(record, sort_keys=True)),
                )
                self._conn.executemany(
                    "INSERT OR IGNORE INTO packet_facts (packet_id, fact_id) VALUES (?, ?)",
                    [(packet_id, f) for f in fact_ids],
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def get_packet(self, packet_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT record_json FROM packets WHERE packet_id = ?", (packet_id,))
        return json.loads(rows[0]["record_json"]) if rows else None

    def packets_with_facts(self, fact_ids: list[str]) -> list[str]:
        if not fact_ids:
            return []
        out: set[str] = set()
        ids = list(fact_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in self._query(f"SELECT DISTINCT packet_id FROM packet_facts WHERE fact_id IN ({marks})", tuple(chunk)):
                out.add(r["packet_id"])
        return sorted(out)
