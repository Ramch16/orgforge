"""SQLite storage. One small file holds the whole company."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  reports_to TEXT NOT NULL CHECK (reports_to IN ('ceo','cto'))
);
CREATE TABLE IF NOT EXISTS roles (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  department TEXT NOT NULL REFERENCES departments(id),
  kind TEXT NOT NULL,
  tools TEXT NOT NULL,
  prompt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seat TEXT NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL REFERENCES roles(id),
  model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','probation','fired')),
  generation INTEGER NOT NULL DEFAULT 1,
  predecessor_id INTEGER,
  lessons TEXT NOT NULL DEFAULT '',
  score REAL,
  evals INTEGER NOT NULL DEFAULT 0,
  probation_at INTEGER,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  hired_at TEXT NOT NULL,
  fired_at TEXT,
  fire_reason TEXT
);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  brief TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'prd',
  workspace TEXT NOT NULL DEFAULT '',
  feedback TEXT NOT NULL DEFAULT '',
  purpose TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  budget REAL NOT NULL DEFAULT 0,
  budget_warned REAL NOT NULL DEFAULT 0,
  paused_stage TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'product',
  source TEXT NOT NULL DEFAULT '',
  base_branch TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '',
  checks TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  role TEXT NOT NULL,
  assignee_id INTEGER,
  status TEXT NOT NULL DEFAULT 'todo',
  depends_on TEXT NOT NULL DEFAULT '[]',
  attempts INTEGER NOT NULL DEFAULT 0,
  feedback TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT 'task',
  priority TEXT NOT NULL DEFAULT 'medium',
  origin TEXT NOT NULL DEFAULT 'plan',
  reporter TEXT NOT NULL DEFAULT 'Vittics Builder',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  author TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'comment',
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  human TEXT NOT NULL CHECK (human IN ('ceo','cto')),
  sender TEXT NOT NULL CHECK (sender IN ('human','agent')),
  body TEXT NOT NULL DEFAULT '',
  project_id INTEGER,
  status TEXT NOT NULL DEFAULT 'sent',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL,
  project_id INTEGER,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost REAL NOT NULL,
  priced INTEGER NOT NULL DEFAULT 1,
  purpose TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  trigger TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  author TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  requested_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  result TEXT NOT NULL DEFAULT '',
  tickets TEXT NOT NULL DEFAULT '[]',
  submitted_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER,
  author TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS swarm_members (
  project_id INTEGER NOT NULL,
  agent_id INTEGER NOT NULL,
  task_id INTEGER NOT NULL,
  assigned_at TEXT NOT NULL,
  PRIMARY KEY (project_id, task_id)
);
CREATE TABLE IF NOT EXISTS routing_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL,
  project_id INTEGER,
  task_id INTEGER,
  task_kind TEXT NOT NULL,
  model TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  success INTEGER,
  quality REAL,
  latency REAL NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  priced INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  environment TEXT NOT NULL,
  version INTEGER NOT NULL,
  commit_sha TEXT NOT NULL,
  status TEXT NOT NULL,
  log TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS node_pairings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  code TEXT NOT NULL,
  expires_at REAL NOT NULL,
  used_at TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  secret TEXT NOT NULL,
  status TEXT NOT NULL,
  info TEXT NOT NULL DEFAULT '{}',
  last_seen REAL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_assignments (
  agent_id INTEGER PRIMARY KEY,
  node_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS node_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL,
  archive TEXT,
  result TEXT,
  error TEXT,
  deadline REAL NOT NULL,
  created_at TEXT NOT NULL,
  claimed_at REAL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS machine_installs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tool TEXT NOT NULL,
  command TEXT NOT NULL,
  status TEXT NOT NULL,
  output TEXT NOT NULL DEFAULT '',
  requested_by TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS review_verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER,
  task_id INTEGER,
  reviewer_id INTEGER NOT NULL,
  route_id INTEGER,
  verdict TEXT NOT NULL,
  score REAL,
  correct INTEGER,
  settled_by TEXT,
  created_at TEXT NOT NULL,
  settled_at TEXT
);
CREATE TABLE IF NOT EXISTS learned_strategies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  task_kind TEXT NOT NULL,
  signature TEXT NOT NULL,
  prompt TEXT NOT NULL,
  why TEXT NOT NULL DEFAULT '',
  evidence INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'proposed',
  proposed_by TEXT NOT NULL,
  decided_by TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS engine_cooldowns (
  engine TEXT PRIMARY KEY,
  until REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL,
  project_id INTEGER,
  ticket_id INTEGER,
  purpose TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  started_at TEXT NOT NULL,
  ended_at TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL,
  run_id INTEGER,
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL REFERENCES agents(id),
  task_id INTEGER,
  project_id INTEGER,
  source TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  score REAL NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER,
  kind TEXT NOT NULL,
  required_role TEXT NOT NULL CHECK (required_role IN ('ceo','cto')),
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  feedback TEXT NOT NULL DEFAULT '',
  decided_by TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  project_id INTEGER,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        usage_columns = {r['name'] for r in self.conn.execute('PRAGMA table_info(usage)')}
        if 'run_id' not in usage_columns:
            self.conn.execute('ALTER TABLE usage ADD COLUMN run_id INTEGER')
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(memories)")}
        for column, default in (("category", "decision"), ("source", "")):
            if column not in have:
                self.conn.execute(f"ALTER TABLE memories ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
        had_memory_index = self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_search'").fetchone()
        self.conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(text, content='memories', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS memory_insert AFTER INSERT ON memories BEGIN
              INSERT INTO memory_search(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_delete AFTER DELETE ON memories BEGIN
              INSERT INTO memory_search(memory_search, rowid, text) VALUES ('delete', old.id, old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_update AFTER UPDATE OF text ON memories BEGIN
              INSERT INTO memory_search(memory_search, rowid, text) VALUES ('delete', old.id, old.text);
              INSERT INTO memory_search(rowid, text) VALUES (new.id, new.text);
            END;
        """)
        # Build the index once for companies created before searchable memory.
        if not had_memory_index:
            self.conn.execute("INSERT INTO memory_search(memory_search) VALUES ('rebuild')")
        # 0.4 turned tasks into tickets.
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        for column, default in (("type", "task"), ("priority", "medium"), ("origin", "plan"), ("reporter", "Vittics Builder")):
            if column not in have:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
        # 0.7 added the idea stage: what a product is for, and who proposed it.
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(projects)")}
        for column, decl in (("purpose", "TEXT NOT NULL DEFAULT ''"), ("author", "TEXT NOT NULL DEFAULT ''"),
                             ("budget", "REAL NOT NULL DEFAULT 0"), ("budget_warned", "REAL NOT NULL DEFAULT 0"),
                             ("paused_stage", "TEXT NOT NULL DEFAULT ''"), ("version", "INTEGER NOT NULL DEFAULT 0"),
                             ("kind", "TEXT NOT NULL DEFAULT 'product'"), ("source", "TEXT NOT NULL DEFAULT ''"),
                             ("base_branch", "TEXT NOT NULL DEFAULT ''"), ("branch", "TEXT NOT NULL DEFAULT ''"),
                             ("checks", "TEXT NOT NULL DEFAULT '[]'")):
            if column not in have:                # 0.7 ideas, 0.8 budgets and versions
                self.conn.execute(f"ALTER TABLE projects ADD COLUMN {column} {decl}")
        # Companies created before 0.2 limited role kinds in the table itself. Lift that.
        sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='roles'").fetchone()["sql"]
        if "CHECK (kind IN" not in sql:
            return
        self.conn.executescript("""
            PRAGMA legacy_alter_table=ON;
            BEGIN;
            ALTER TABLE roles RENAME TO roles_old;
            CREATE TABLE roles (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, department TEXT NOT NULL REFERENCES departments(id),
              kind TEXT NOT NULL, tools TEXT NOT NULL, prompt TEXT NOT NULL);
            INSERT INTO roles SELECT id, title, department, kind, tools, prompt FROM roles_old;
            DROP TABLE roles_old;
            COMMIT;
            PRAGMA legacy_alter_table=OFF;
        """)

    def run(self, sql: str, *args) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def all(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, *args) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def log(self, kind: str, message: str, project_id: int | None = None, actor: str = "system") -> None:
        self.run(
            "INSERT INTO events (ts, project_id, actor, kind, message) VALUES (?,?,?,?,?)",
            now(), project_id, actor, kind, message,
        )
