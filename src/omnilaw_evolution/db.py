"""SQLite store for sources, knowledge, proposals, runs, evaluations, releases
and failures. Large artefacts (snapshots, patches, logs) live in files; rows
point at them."""
from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA_VERSION = 4

# Migrations keyed by the version they produce. "additive" migrations only add
# tables/columns, so releases recorded under an older schema stay compatible.
MIGRATIONS = {
    4: {"additive": True, "sql": [
        "ALTER TABLE proposals ADD COLUMN evaluator_version TEXT",
    ]},
    3: {"additive": True, "sql": [
        """CREATE TABLE IF NOT EXISTS journals (
            id INTEGER PRIMARY KEY, period TEXT NOT NULL, since TEXT NOT NULL, until TEXT NOT NULL,
            path TEXT NOT NULL, counts TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""",
    ]},
    2: {"additive": True, "sql": [
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL DEFAULT 50,
            origin TEXT NOT NULL DEFAULT 'user', dedupe_key TEXT, not_before TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 2,
            run_id INTEGER, result TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
            parent_id INTEGER, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            updated_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, priority, id)",
        """CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, cron TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
            last_fired_at TEXT, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS discoveries (
            id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, url TEXT NOT NULL,
            title TEXT, summary TEXT, topic TEXT, relevance REAL, status TEXT NOT NULL DEFAULT 'new',
            source_id INTEGER, note TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            times_seen INTEGER NOT NULL DEFAULT 1)""",
        """CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY, origin TEXT NOT NULL, user TEXT, text TEXT NOT NULL, run_id INTEGER,
            item_id TEXT, label TEXT, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS budget_ledger (
            day TEXT PRIMARY KEY, model_calls INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            download_bytes INTEGER NOT NULL DEFAULT 0, candidates INTEGER NOT NULL DEFAULT 0,
            sandbox_seconds REAL NOT NULL DEFAULT 0, tasks INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL)""",
        """CREATE TABLE IF NOT EXISTS eval_cache (
            key TEXT PRIMARY KEY, module_hash TEXT NOT NULL, evaluator_version TEXT NOT NULL,
            split TEXT NOT NULL, split_sha TEXT NOT NULL, result TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, channel TEXT NOT NULL, target TEXT, text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', error TEXT, created_at TEXT NOT NULL, sent_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS chat_confirmations (
            code TEXT PRIMARY KEY, user TEXT NOT NULL, command TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS chat_events (
            event_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)""",
        "ALTER TABLE knowledge ADD COLUMN review_status TEXT NOT NULL DEFAULT 'current'",
        "ALTER TABLE sources ADD COLUMN supersedes INTEGER",
        "ALTER TABLE sources ADD COLUMN reviewed_at TEXT",
        "ALTER TABLE sources ADD COLUMN review_note TEXT",
    ]},
}


def schema_compatible(old, new):
    """A release recorded under schema ``old`` can run against state at ``new``
    when every migration in between is additive."""
    if old == new:
        return True
    if old > new:
        return False
    return all(MIGRATIONS[v]["additive"] for v in range(old + 1, new + 1))

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL, title TEXT,
    fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE, revision TEXT,
    license TEXT, read_status TEXT NOT NULL, path TEXT NOT NULL, size INTEGER,
    origin TEXT, flags TEXT DEFAULT '[]', meta TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
    method TEXT NOT NULL, claim TEXT NOT NULL, steps TEXT NOT NULL, conditions TEXT NOT NULL,
    limitations TEXT NOT NULL, locator TEXT NOT NULL, excerpt TEXT NOT NULL,
    verification TEXT NOT NULL DEFAULT 'source_claim', applicable INTEGER NOT NULL DEFAULT 1,
    experiments TEXT NOT NULL DEFAULT '[]', extractor TEXT NOT NULL,
    created_at TEXT NOT NULL, reviewed_at TEXT, UNIQUE(source_id, method));
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY, goal TEXT NOT NULL, problem TEXT NOT NULL, module TEXT NOT NULL,
    hypothesis TEXT NOT NULL, methods TEXT NOT NULL, knowledge_ids TEXT NOT NULL,
    variants TEXT NOT NULL, criteria TEXT NOT NULL, planner TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    state TEXT NOT NULL, mode TEXT NOT NULL, baseline_sha TEXT, candidate_sha TEXT,
    patch_path TEXT, split_version TEXT, evaluator_version TEXT, model_config TEXT,
    executor TEXT, budget TEXT NOT NULL, usage TEXT NOT NULL DEFAULT '{}',
    checkpoint TEXT NOT NULL DEFAULT '{}', decision TEXT, error TEXT,
    stop_requested INTEGER NOT NULL DEFAULT 0, pid INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, run_id INTEGER, ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY, run_id INTEGER, variant TEXT NOT NULL, split TEXT NOT NULL,
    code_sha TEXT, metrics TEXT NOT NULL, per_item_path TEXT, runtime_s REAL,
    model_usage TEXT DEFAULT '{}', failures TEXT DEFAULT '[]', log_path TEXT,
    isolation TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY, sha TEXT NOT NULL, run_id INTEGER, policy TEXT NOT NULL,
    deps_hash TEXT NOT NULL, schema_version INTEGER NOT NULL, health TEXT,
    status TEXT NOT NULL, previous_release_id INTEGER, path TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY, run_id INTEGER, release_id INTEGER, split TEXT NOT NULL,
    item_id TEXT NOT NULL, kind TEXT NOT NULL, case_type TEXT, detail TEXT,
    status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
"""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')")
        self.migrate()

    def migrate(self):
        current = self.schema_version()
        for version in range(current + 1, SCHEMA_VERSION + 1):
            self.conn.execute("BEGIN IMMEDIATE")
            if self.schema_version() >= version:  # another process migrated first
                self.conn.execute("COMMIT")
                continue
            try:
                for stmt in MIGRATIONS[version]["sql"]:
                    self.conn.execute(stmt)
                self.conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(version),))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def close(self):
        self.conn.close()

    def execute(self, sql, args=()):
        return self.conn.execute(sql, args)

    def insert(self, table, row):
        cols = list(row)
        vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in row.values()]
        cur = self.conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(cols), ",".join("?" * len(cols))),
                                vals)
        return cur.lastrowid

    def update(self, table, rid, **fields):
        vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self.conn.execute("UPDATE %s SET %s WHERE id=?" % (table, ",".join("%s=?" % k for k in fields)),
                          vals + [rid])

    def get(self, table, rid):
        row = self.conn.execute("SELECT * FROM %s WHERE id=?" % table, (rid,)).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def event(self, run_id, kind, detail=None):
        self.insert("events", {"run_id": run_id, "ts": now(), "kind": kind,
                               "detail": detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)})

    def schema_version(self):
        return int(self.one("SELECT value FROM meta WHERE key='schema_version'")["value"])


def loads(value, default=None):
    if value is None or value == "":
        return default
    return json.loads(value)
