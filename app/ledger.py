"""
Hash-chained audit ledger.

Every decision the sentinel makes - auto-approved, judge-approved, rejected,
escalated, human-resolved, reversed - gets appended here. Each row's hash
depends on the previous row's hash, so mutating or deleting any historical
row breaks the chain and verify() will catch it. This is deliberately NOT a
blockchain (no consensus, no distributed nodes needed - we're the sole
writer) - it's the minimum mechanism that gives us tamper-evidence for
compliance/audit purposes.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass


GENESIS_HASH = "0" * 64


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash_row(prev_hash: str, timestamp: float, payload: str) -> str:
    h = hashlib.sha256()
    h.update(prev_hash.encode())
    h.update(str(timestamp).encode())
    h.update(payload.encode())
    return h.hexdigest()


@dataclass
class LedgerEntry:
    seq: int
    timestamp: float
    payload: dict
    prev_hash: str
    hash: str


class AuditLedger:
    def __init__(self, db_path: str = "data/ledger.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS ledger (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                payload TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def append(self, payload: dict) -> LedgerEntry:
        prev_hash = self._last_hash()
        ts = time.time()
        canon = _canonical(payload)
        row_hash = _hash_row(prev_hash, ts, canon)
        cur = self._conn.execute(
            "INSERT INTO ledger (timestamp, payload, prev_hash, hash) VALUES (?, ?, ?, ?)",
            (ts, canon, prev_hash, row_hash),
        )
        self._conn.commit()
        return LedgerEntry(seq=cur.lastrowid, timestamp=ts, payload=payload, prev_hash=prev_hash, hash=row_hash)

    def _last_hash(self) -> str:
        row = self._conn.execute("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS_HASH

    def all_entries(self) -> list[LedgerEntry]:
        rows = self._conn.execute(
            "SELECT seq, timestamp, payload, prev_hash, hash FROM ledger ORDER BY seq ASC"
        ).fetchall()
        return [
            LedgerEntry(seq=r[0], timestamp=r[1], payload=json.loads(r[2]), prev_hash=r[3], hash=r[4])
            for r in rows
        ]

    def verify(self) -> tuple[bool, str | None]:
        """Recompute the chain from scratch. Returns (is_valid, error_message)."""
        prev_hash = GENESIS_HASH
        for entry in self.all_entries():
            canon = _canonical(entry.payload)
            expected = _hash_row(prev_hash, entry.timestamp, canon)
            if entry.prev_hash != prev_hash:
                return False, f"seq {entry.seq}: prev_hash link broken"
            if entry.hash != expected:
                return False, f"seq {entry.seq}: hash mismatch - entry has been tampered with"
            prev_hash = entry.hash
        return True, None
