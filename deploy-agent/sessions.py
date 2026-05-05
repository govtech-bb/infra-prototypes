"""Session model + storage interface.

Task 2 ships an in-memory implementation. Task 3 replaces it with a SQLite
implementation behind the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4


@dataclass
class Session:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    upload_dir: str | None = None
    deployment: dict | None = None


class SessionStore(Protocol):
    def create(self) -> Session: ...
    def get(self, session_id: str) -> Session | None: ...
    def save(self, session: Session) -> None: ...


class InMemorySessionStore:
    """Drop-in replacement for the original `sessions: dict` in app.py."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        session = Session(session_id=str(uuid4()))
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def save(self, session: Session) -> None:
        self._sessions[session.session_id] = session


import json
import sqlite3
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  messages     TEXT NOT NULL DEFAULT '[]',
  files        TEXT NOT NULL DEFAULT '[]',
  upload_dir   TEXT,
  deployment   TEXT,
  project_name TEXT,
  env          TEXT,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class SqliteSessionStore:
    """SessionStore backed by a single SQLite file. Survives process restart."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self) -> Session:
        session = Session(session_id=str(uuid4()))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id) VALUES (?)",
                (session.session_id,),
            )
            conn.commit()
        return session

    def get(self, session_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        deployment = json.loads(row["deployment"]) if row["deployment"] else None
        return Session(
            session_id=row["session_id"],
            messages=json.loads(row["messages"]),
            files=json.loads(row["files"]),
            upload_dir=row["upload_dir"],
            deployment=deployment,
        )

    def save(self, session: Session) -> None:
        deployment_json = json.dumps(session.deployment) if session.deployment else None
        project_name = session.deployment.get("project_name") if session.deployment else None
        env = session.deployment.get("env") if session.deployment else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, messages, files, upload_dir,
                    deployment, project_name, env, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    messages    = excluded.messages,
                    files       = excluded.files,
                    upload_dir  = excluded.upload_dir,
                    deployment  = excluded.deployment,
                    project_name= excluded.project_name,
                    env         = excluded.env,
                    updated_at  = CURRENT_TIMESTAMP
                """,
                (
                    session.session_id,
                    json.dumps(session.messages),
                    json.dumps(session.files),
                    session.upload_dir,
                    deployment_json,
                    project_name,
                    env,
                ),
            )
            conn.commit()
