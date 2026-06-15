"""Deployment record + SQLite persistence.

Sibling of sessions.py — same defensive ALTER-on-init idempotent
migration pattern. Status transitions are encoded as DeploymentStatus
enum values stored as strings.
"""

from __future__ import annotations

import enum
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


class DeploymentStatus(str, enum.Enum):
    QUEUED = "queued"
    CLONING = "cloning"
    APPLYING = "applying"
    SYNCING = "syncing"
    LIVE = "live"
    MODIFYING = "modifying"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    EXPIRED = "expired"
    FAILED = "failed"


_IN_FLIGHT = {
    DeploymentStatus.QUEUED.value,
    DeploymentStatus.CLONING.value,
    DeploymentStatus.APPLYING.value,
    DeploymentStatus.SYNCING.value,
    DeploymentStatus.MODIFYING.value,
    DeploymentStatus.DESTROYING.value,
}

_ACTIVE = {
    DeploymentStatus.QUEUED.value,
    DeploymentStatus.CLONING.value,
    DeploymentStatus.APPLYING.value,
    DeploymentStatus.SYNCING.value,
    DeploymentStatus.LIVE.value,
    DeploymentStatus.MODIFYING.value,
}


@dataclass
class Deployment:
    deployment_id: str
    session_id: str
    repo_url: str
    pattern: str
    project_name: str
    env: str
    status: DeploymentStatus = DeploymentStatus.QUEUED
    outputs: dict = field(default_factory=dict)
    knobs: dict = field(default_factory=dict)
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(days=14)
    )
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
  deployment_id TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,
  repo_url      TEXT NOT NULL,
  pattern       TEXT NOT NULL,
  project_name  TEXT NOT NULL,
  env           TEXT NOT NULL,
  status        TEXT NOT NULL,
  outputs       TEXT NOT NULL DEFAULT '{}',
  knobs         TEXT NOT NULL DEFAULT '{}',
  expires_at    TIMESTAMP NOT NULL,
  last_error    TEXT,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_deployments_session ON deployments(session_id);
CREATE INDEX IF NOT EXISTS idx_deployments_status  ON deployments(status);
CREATE INDEX IF NOT EXISTS idx_deployments_expires ON deployments(expires_at);
"""


class SqliteDeploymentStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create(
        self,
        session_id: str,
        repo_url: str,
        pattern: str,
        project_name: str,
        env: str,
        ttl_days: int,
    ) -> Deployment:
        d = Deployment(
            deployment_id=str(uuid4()),
            session_id=session_id,
            repo_url=repo_url,
            pattern=pattern,
            project_name=project_name,
            env=env,
            expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days),
        )
        self.save(d)
        return d

    def get(self, deployment_id: str) -> Deployment | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE deployment_id = ?", (deployment_id,)
            ).fetchone()
        return self._row_to_deployment(row) if row else None

    def save(self, d: Deployment) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deployments
                  (deployment_id, session_id, repo_url, pattern, project_name, env,
                   status, outputs, knobs, expires_at, last_error, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)
                ON CONFLICT(deployment_id) DO UPDATE SET
                  status     = excluded.status,
                  outputs    = excluded.outputs,
                  knobs      = excluded.knobs,
                  expires_at = excluded.expires_at,
                  last_error = excluded.last_error,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    d.deployment_id,
                    d.session_id,
                    d.repo_url,
                    d.pattern,
                    d.project_name,
                    d.env,
                    d.status.value,
                    json.dumps(d.outputs),
                    json.dumps(d.knobs),
                    d.expires_at.isoformat(),
                    d.last_error,
                ),
            )

    def list_active(self) -> list[Deployment]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deployments WHERE status IN ({}) ORDER BY created_at DESC".format(
                    ",".join("?" * len(_ACTIVE))
                ),
                tuple(_ACTIVE),
            ).fetchall()
        return [self._row_to_deployment(r) for r in rows]

    def list_for_session(self, session_id: str) -> list[Deployment]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deployments WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        return [self._row_to_deployment(r) for r in rows]

    def list_expired(self) -> list[Deployment]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deployments WHERE status = ? AND expires_at < ?",
                (DeploymentStatus.LIVE.value, now),
            ).fetchall()
        return [self._row_to_deployment(r) for r in rows]

    def count_today_for_session(self, session_id: str) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM deployments "
                "WHERE session_id = ? AND date(created_at) = date('now')",
                (session_id,),
            ).fetchone()[0]

    def count_today_global(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM deployments WHERE date(created_at) = date('now')"
            ).fetchone()[0]

    def extend(self, deployment_id: str, days: int) -> Deployment | None:
        d = self.get(deployment_id)
        if d is None:
            return None
        d.expires_at = datetime.now(timezone.utc) + timedelta(days=days)
        self.save(d)
        return d

    def recover_in_flight(self) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE deployments SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE status IN ({})".format(",".join("?" * len(_IN_FLIGHT))),
                (
                    DeploymentStatus.FAILED.value,
                    "Interrupted by task restart — safe to retry.",
                    *_IN_FLIGHT,
                ),
            )
            return cur.rowcount

    @staticmethod
    def _row_to_deployment(row: sqlite3.Row) -> Deployment:
        def _ensure_utc(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt

        return Deployment(
            deployment_id=row["deployment_id"],
            session_id=row["session_id"],
            repo_url=row["repo_url"],
            pattern=row["pattern"],
            project_name=row["project_name"],
            env=row["env"],
            status=DeploymentStatus(row["status"]),
            outputs=json.loads(row["outputs"] or "{}"),
            knobs=json.loads(row["knobs"] or "{}"),
            expires_at=_ensure_utc(datetime.fromisoformat(row["expires_at"])),
            last_error=row["last_error"],
            created_at=_ensure_utc(datetime.fromisoformat(row["created_at"])),
            updated_at=_ensure_utc(datetime.fromisoformat(row["updated_at"])),
        )
