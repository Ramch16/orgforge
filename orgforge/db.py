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
  reporter TEXT NOT NULL DEFAULT 'OrgForge',
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
        # 0.4 turned tasks into tickets.
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        for column, default in (("type", "task"), ("priority", "medium"), ("origin", "plan"), ("reporter", "OrgForge")):
            if column not in have:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
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
