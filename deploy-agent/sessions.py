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
