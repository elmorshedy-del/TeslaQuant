"""Short SQLite transactions; immutable research versions; fenced job leases."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
 checksum TEXT NOT NULL, rows INTEGER NOT NULL, metadata TEXT NOT NULL,
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0, worker TEXT, token TEXT, lease_until REAL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 result TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS reviews (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS forecasts (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reference_weeks (
 id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_objects (
 id TEXT PRIMARY KEY, path TEXT NOT NULL, metadata TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Ledger:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "ledger.sqlite3"
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError("Ledger schema is newer than this application")
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        try:
            with c:
                yield c
        finally:
            c.close()

    def enqueue(self, kind: str, payload: dict) -> str:
        fingerprint = identity({"kind": kind, "payload": payload})
        now = time.time()
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO jobs(id,fingerprint,kind,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                      (fingerprint, fingerprint, kind, canonical(payload), now, now))
        return fingerprint

    def claim(self, worker: str, lease_seconds=180):
        now = time.time()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE jobs SET status='queued',token=NULL,worker=NULL WHERE status='running' AND lease_until<? AND attempts<3", (now,))
            c.execute("UPDATE jobs SET status='failed',error='Worker lease expired after 3 attempts',updated_at=? WHERE status='running' AND lease_until<? AND attempts>=3", (now, now))
            row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            c.execute("UPDATE jobs SET status='running',worker=?,token=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?",
                      (worker, token, now + lease_seconds, now, row["id"]))
            return self._job(c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def heartbeat(self, jid, token, lease_seconds=180):
        now = time.time()
        with self.connect() as c:
            changed = c.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE id=? AND token=? AND status='running' AND lease_until>=?",
                                (now + lease_seconds, now, jid, token, now)).rowcount
        if not changed:
            raise ValueError("Job lease lost")

    def finish(self, jid, token, status, result=None, error=None):
        if status not in {"succeeded", "failed", "blocked"}:
            raise ValueError("Invalid final status")
        now = time.time()
        with self.connect() as c:
            changed = c.execute("UPDATE jobs SET status=?,result=?,error=?,updated_at=?,lease_until=NULL WHERE id=? AND token=? AND status='running' AND lease_until>=?",
                                (status, canonical(result) if result is not None else None, error, now, jid, token, now)).rowcount
        if not changed:
            raise ValueError("Job lease lost")

    def retry(self, jid):
        with self.connect() as c:
            if not c.execute("UPDATE jobs SET status='queued',attempts=0,token=NULL,error=NULL,result=NULL,updated_at=? WHERE id=? AND status IN ('failed','blocked')", (time.time(), jid)).rowcount:
                raise ValueError("Only failed or blocked jobs can be retried")

    @staticmethod
    def _job(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("payload", "result"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def job(self, jid):
        with self.connect() as c:
            return self._job(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def jobs(self):
        with self.connect() as c:
            return [self._job(r) for r in c.execute("SELECT * FROM jobs ORDER BY created_at DESC")]

    def datasets(self, kind=None):
        with self.connect() as c:
            rows = c.execute("SELECT * FROM datasets WHERE kind=? ORDER BY created_at DESC", (kind,)) if kind else c.execute("SELECT * FROM datasets ORDER BY created_at DESC")
            return [dict(r) | {"metadata": json.loads(r["metadata"])} for r in rows]

    def append_forecast(self, body):
        fid = identity(body)
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO forecasts VALUES(?,?,?)", (fid, canonical(body), time.time()))
        return fid
