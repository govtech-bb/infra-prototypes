"""Session model + SQLite storage.

Adapted from deploy-agent/sessions.py. Different fields: no uploads,
no deployment record; instead carries `clone_path` (where the most
recently cloned repo lives) and `last_profile` (cached RepoProfile).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4


@dataclass
class Session:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    clone_path: str | None = None
    last_profile: dict | None = None


class SessionStore(Protocol):
    def create(self) -> Session: ...
    def get(self, session_id: str) -> Session | None: ...
    def save(self, session: Session) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  messages     TEXT NOT NULL DEFAULT '[]',
  clone_path   TEXT,
  last_profile TEXT,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class SqliteSessionStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self) -> Session:
        session = Session(session_id=str(uuid4()))
        with self._connect() as conn:
            conn.execute("INSERT INTO sessions (session_id) VALUES (?)", (session.session_id,))
        return session

    def get(self, session_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return Session(
            session_id=row["session_id"],
            messages=json.loads(row["messages"]),
            clone_path=row["clone_path"],
            last_profile=json.loads(row["last_profile"]) if row["last_profile"] else None,
        )

    def save(self, session: Session) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, messages, clone_path, last_profile, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    messages     = excluded.messages,
                    clone_path   = excluded.clone_path,
                    last_profile = excluded.last_profile,
                    updated_at   = CURRENT_TIMESTAMP
                """,
                (
                    session.session_id,
                    json.dumps(session.messages),
                    session.clone_path,
                    json.dumps(session.last_profile) if session.last_profile else None,
                ),
            )
