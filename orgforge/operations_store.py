"""Additive storage for company operations; safe to open existing companies."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS trajectories (
 route_id INTEGER PRIMARY KEY, strategy TEXT NOT NULL, task_kind TEXT NOT NULL,
 signature TEXT NOT NULL, success INTEGER NOT NULL, quality REAL, lessons TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_service (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS worker_jobs (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, project_id INTEGER NOT NULL, kind TEXT NOT NULL,
 config TEXT NOT NULL, interval_seconds INTEGER NOT NULL, next_run REAL NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, lease_until REAL NOT NULL DEFAULT 0, lease_owner TEXT,
 failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS worker_runs (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
 started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, source TEXT NOT NULL, event_key TEXT NOT NULL,
 severity TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
 ticket_id INTEGER, created_at TEXT NOT NULL, last_seen TEXT NOT NULL, UNIQUE(project_id, source, event_key)
);
CREATE TABLE IF NOT EXISTS arena_runs (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, status TEXT NOT NULL, goal TEXT NOT NULL,
 benchmark TEXT NOT NULL, winner TEXT, results TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_assessments (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
 revision TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS marketplace (
 name TEXT PRIMARY KEY, version TEXT NOT NULL, digest TEXT NOT NULL, manifest TEXT NOT NULL,
 assets TEXT NOT NULL, installed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS federation_inbox (
 peer TEXT NOT NULL, message_id TEXT NOT NULL, receipt TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(peer, message_id)
);
'''


def initialize(db):
    with db.lock:
        db.conn.executescript(SCHEMA)
        columns = {r['name'] for r in db.conn.execute('PRAGMA table_info(routing_decisions)')}
        for column, default in [('strategy','baseline'),('signature','general')]:
            if column not in columns:
                db.conn.execute(f"ALTER TABLE routing_decisions ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")


from contextlib import contextmanager
import uuid


@contextmanager
def atomic(db):
    with db.lock:
        nested=db.conn.in_transaction
        token='op_'+uuid.uuid4().hex
        db.conn.execute('SAVEPOINT '+token if nested else 'BEGIN IMMEDIATE')
        try:
            yield
            db.conn.execute('RELEASE SAVEPOINT '+token if nested else 'COMMIT')
        except BaseException:
            if nested:
                db.conn.execute('ROLLBACK TO SAVEPOINT '+token)
                db.conn.execute('RELEASE SAVEPOINT '+token)
            else:
                db.conn.execute('ROLLBACK')
            raise
