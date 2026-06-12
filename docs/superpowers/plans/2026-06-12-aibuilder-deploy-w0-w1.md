# aibuilder deploy + modify — W0 + W1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the foundation (async job runner, S3-backed tofu state, deploy caps, TTL reaper, private-repo cloning, pluggable stack registry) and the first deployable pattern (static sites) so chat can deploy → modify → redeploy → destroy an analyzed repo end-to-end.

**Architecture:** A new `deploy_stacks/` package exposes pluggable `StackSpec` entries keyed by catalog pattern. Deploys run as in-process asyncio jobs against per-deployment tofu state in S3, tracked by a `deployments` SQLite table. New agent tools (`deploy_repo`, `get_deployment_status`, `list_deployments`, `redeploy`, `modify_deployment`, `destroy_deployment`, `extend_deployment`) wrap the registry. An hourly reaper enforces a 14-day TTL. A bearer-authed `POST /api/deployments/{id}/redeploy` endpoint lets teammates trigger a redeploy from their own Claude Code without opening the aibuilder UI.

**Tech Stack:** Python 3.13 (FastAPI, anthropic.AnthropicBedrock, asyncio, sqlite3, boto3), OpenTofu 1.8.x, AWS (S3, CloudFront, DynamoDB, ECS Fargate, SSM, IAM), pytest.

**Spec:** `docs/superpowers/specs/2026-06-11-aibuilder-deploy-design.md`

---

## File structure

**New (aibuilder/):**
- `errors.py` — `_classify_error(stderr) -> {summary, details}` ported from deploy-agent.
- `deployments.py` — `Deployment` dataclass + `SqliteDeploymentStore` + status enum.
- `limits.py` — daily cap counters.
- `jobs.py` — asyncio job queue + worker + startup recovery sweep.
- `gh_clone.py` — git clone helper with private-repo retry + token scrub.
- `reaper.py` — TTL background task.
- `deploy_stacks/__init__.py` — `StackSpec` dataclass + `STACK_REGISTRY`.
- `deploy_stacks/static_website/` — self-contained tofu copy (stack + modules + S3 backend block).

**Modified (aibuilder/):**
- `tools.py` — seven new deploy tools + `TOOL_DEFINITIONS` entries + `execute_tool` dispatch.
- `agent.py` — extend `SYSTEM_PROMPT` for deploy/update/destroy workflows.
- `app.py` — FastAPI lifespan starts worker + reaper + runs recovery sweep; add `POST /api/deployments/{id}/redeploy` and `GET /api/deployments/{id}`.
- `Dockerfile` — install `tofu` 1.8.x.
- `docker-compose.yml` — pass `AIBUILDER_GITHUB_TOKEN`, `AIBUILDER_DEPLOY_STATE_BUCKET`, `AIBUILDER_DEPLOY_LOCK_TABLE`.

**Modified (aibuilder/infra/stacks/aibuilder-hosting/):**
- `state.tf` (new) — S3 state bucket + DynamoDB lock table.
- `ssm.tf` — add `/aibuilder/github-token`.
- `ecs.tf` — add env vars + secret to task definition.
- `iam.tf` — add `task_deploy_state` (state backend) + `task_deploy_w1` (provisioning for `aibd-*` S3 + CloudFront).

**Resource naming convention:** Deployed resources use prefix `aibd-<project>-<env>-` so the W1 IAM policy can grant on `arn:aws:s3:::aibd-*` and tag `Stack = "aibuilder-deploy"`. Deploy state key: `deployments/<project>-<env>.tfstate`. Per-job workdir: `/aibuilder/data/deploys/<deployment_id>/` with `TF_DATA_DIR` to isolate `.terraform` state per job.

---

## Task 0: Branch prep + plan checkpoint

**Files:**
- Verify: branch `aibuilder-deploy-spec` already has the design spec committed.

- [ ] **Step 1:** Confirm branch state.

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git branch --show-current
git log --oneline -3
```

Expected: branch is `aibuilder-deploy-spec`, top commit is the design spec.

- [ ] **Step 2:** No code change. Move on.

---

## Task 1: Install tofu in the Docker image

**Files:**
- Modify: `aibuilder/Dockerfile`

- [ ] **Step 1:** Append tofu install to the Dockerfile right after the existing `apt-get install` block.

```dockerfile
# OpenTofu 1.8.x — pinned to match CI; deploys run from inside this image.
ARG TOFU_VERSION=1.8.5
RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip wget \
    && wget -q "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" -O /tmp/tofu.zip \
    && unzip /tmp/tofu.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/tofu \
    && rm /tmp/tofu.zip \
    && apt-get purge -y unzip wget \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2:** Build the image and verify tofu is on PATH.

```bash
cd "/Users/christophercorbin/INFRA prototypes/aibuilder"
docker build -t aibuilder:test .
docker run --rm aibuilder:test tofu version
```

Expected: prints `OpenTofu v1.8.5` (or whatever was pinned).

- [ ] **Step 3:** Commit.

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add aibuilder/Dockerfile
git commit -m "feat(aibuilder): install OpenTofu in image for deploy jobs"
```

---

## Task 2: Port `_classify_error`

**Files:**
- Create: `aibuilder/errors.py`
- Test: `aibuilder/tests/test_errors.py`

- [ ] **Step 1:** Write the failing test.

```python
# aibuilder/tests/test_errors.py
from errors import classify_error


def test_classifies_missing_credentials():
    out = classify_error("Error: NoCredentialProviders chain found")
    assert "credentials" in out["summary"].lower()


def test_classifies_bucket_collision():
    out = classify_error("Error: BucketAlreadyOwnedByYou: bucket 'x' already exists")
    assert "bucket" in out["summary"].lower()


def test_classifies_access_denied():
    out = classify_error("is not authorized to perform: s3:CreateBucket")
    assert "permission" in out["summary"].lower()


def test_falls_through_to_generic():
    out = classify_error("weird unparseable thing")
    assert out["summary"] == "Deployment failed — see details."
    assert "weird" in out["details"]


def test_details_truncated_to_last_2000_chars():
    long = "x" * 5000 + "TAIL"
    out = classify_error(long)
    assert out["details"].endswith("TAIL")
    assert len(out["details"]) == 2000
```

- [ ] **Step 2:** Run test.

```bash
cd "/Users/christophercorbin/INFRA prototypes/aibuilder"
.venv/bin/python -m pytest tests/test_errors.py -v
```

Expected: FAIL — `errors` module not found.

- [ ] **Step 3:** Implement.

```python
# aibuilder/errors.py
"""Map tofu/AWS stderr to friendly {summary, details} error dicts.

Ported verbatim from deploy-agent/tools.py:_classify_error. The pattern
order matters — bucket-collision is checked before AccessDenied so the
more specific message wins. Add patterns in order of specificity.
"""

from __future__ import annotations

import re

_ERROR_PATTERNS: list[tuple[str, str]] = [
    (
        r"NoCredentialProviders|Unable to locate credentials",
        "No AWS credentials found in the deploy task — check the task role.",
    ),
    (
        r"BucketAlreadyOwnedByYou|BucketAlreadyExists",
        "A bucket with this name already exists. Pick a different project name.",
    ),
    (
        r"AccessDenied|UnauthorizedOperation|is not authorized to",
        "The deploy task role lacks permission for this operation. Check IAM.",
    ),
    (
        r"NoSuchBucket",
        "S3 bucket not found — it may have been deleted out-of-band.",
    ),
    (
        r"Error: error configuring",
        "AWS configuration error — check the region and credentials.",
    ),
]


def classify_error(stderr: str) -> dict:
    details = stderr[-2000:]
    for pattern, summary in _ERROR_PATTERNS:
        if re.search(pattern, stderr, re.IGNORECASE):
            return {"summary": summary, "details": details}
    return {"summary": "Deployment failed — see details.", "details": details}
```

- [ ] **Step 4:** Run test.

```bash
.venv/bin/python -m pytest tests/test_errors.py -v
```

Expected: all 5 PASS.

- [ ] **Step 5:** Commit.

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add aibuilder/errors.py aibuilder/tests/test_errors.py
git commit -m "feat(aibuilder): port classify_error from deploy-agent"
```

---

## Task 3: Deployments store

**Files:**
- Create: `aibuilder/deployments.py`
- Test: `aibuilder/tests/test_deployments.py`

- [ ] **Step 1:** Write failing tests.

```python
# aibuilder/tests/test_deployments.py
from datetime import datetime, timedelta, timezone

import pytest

from deployments import Deployment, DeploymentStatus, SqliteDeploymentStore


@pytest.fixture
def store(tmp_path):
    return SqliteDeploymentStore(tmp_path / "deploys.db")


def test_create_returns_deployment_with_id_and_queued_status(store):
    d = store.create(
        session_id="s1",
        repo_url="https://github.com/foo/bar",
        pattern="static_site",
        project_name="bar",
        env="proto",
        ttl_days=14,
    )
    assert d.deployment_id
    assert d.status == DeploymentStatus.QUEUED
    assert d.session_id == "s1"
    assert d.expires_at > datetime.now(timezone.utc)


def test_get_returns_none_for_missing(store):
    assert store.get("nonexistent") is None


def test_save_then_get_roundtrips(store):
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    d.outputs = {"site_url": "https://example.com"}
    store.save(d)
    loaded = store.get(d.deployment_id)
    assert loaded.status == DeploymentStatus.LIVE
    assert loaded.outputs == {"site_url": "https://example.com"}


def test_list_active_excludes_destroyed(store):
    a = store.create("s", "u", "static_site", "a", "e", ttl_days=14)
    b = store.create("s", "u", "static_site", "b", "e", ttl_days=14)
    b.status = DeploymentStatus.DESTROYED
    store.save(b)
    active = store.list_active()
    ids = [d.deployment_id for d in active]
    assert a.deployment_id in ids
    assert b.deployment_id not in ids


def test_list_for_session_filters(store):
    a = store.create("s1", "u", "static_site", "a", "e", ttl_days=14)
    store.create("s2", "u", "static_site", "b", "e", ttl_days=14)
    out = store.list_for_session("s1")
    assert len(out) == 1
    assert out[0].deployment_id == a.deployment_id


def test_count_today_for_session_counts_only_today(store):
    store.create("s", "u", "static_site", "a", "e", ttl_days=14)
    store.create("s", "u", "static_site", "b", "e", ttl_days=14)
    store.create("other", "u", "static_site", "c", "e", ttl_days=14)
    assert store.count_today_for_session("s") == 2
    assert store.count_today_global() == 3


def test_list_expired_returns_past_ttl(store):
    d = store.create("s", "u", "static_site", "a", "e", ttl_days=14)
    d.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    d.status = DeploymentStatus.LIVE
    store.save(d)
    expired = store.list_expired()
    assert len(expired) == 1


def test_extend_resets_clock(store):
    d = store.create("s", "u", "static_site", "a", "e", ttl_days=14)
    original = d.expires_at
    d.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    store.save(d)
    d = store.extend(d.deployment_id, days=14)
    assert d.expires_at > original


def test_recover_in_flight_marks_them_failed(store):
    d1 = store.create("s", "u", "static_site", "a", "e", ttl_days=14)
    d2 = store.create("s", "u", "static_site", "b", "e", ttl_days=14)
    d2.status = DeploymentStatus.APPLYING
    store.save(d2)
    n = store.recover_in_flight()
    # both queued + applying were in-flight
    assert n == 2
    loaded = store.get(d1.deployment_id)
    assert loaded.status == DeploymentStatus.FAILED
    assert "interrupted" in loaded.last_error.lower()


def test_schema_migration_is_idempotent(tmp_path):
    SqliteDeploymentStore(tmp_path / "x.db")
    SqliteDeploymentStore(tmp_path / "x.db")  # second open mustn't crash
```

- [ ] **Step 2:** Run tests — expect ImportError.

```bash
.venv/bin/python -m pytest tests/test_deployments.py -v
```

- [ ] **Step 3:** Implement.

```python
# aibuilder/deployments.py
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
        """Mark any in-flight deployment as failed — called at startup.

        Returns the number of rows updated. Tofu state in S3 is the
        source of truth, so a retry is always safe.
        """
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
            expires_at=datetime.fromisoformat(row["expires_at"]),
            last_error=row["last_error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
```

- [ ] **Step 4:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_deployments.py -v
```

Expected: all PASS.

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/deployments.py aibuilder/tests/test_deployments.py
git commit -m "feat(aibuilder): deployments SQLite store with status + TTL"
```

---

## Task 4: Stack registry

**Files:**
- Create: `aibuilder/deploy_stacks/__init__.py`
- Test: `aibuilder/tests/test_deploy_stacks.py`

- [ ] **Step 1:** Write failing tests.

```python
# aibuilder/tests/test_deploy_stacks.py
import pytest

from deploy_stacks import StackSpec, get_spec, list_supported_patterns, register


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    """Each test gets an empty registry."""
    from deploy_stacks import _registry  # noqa: PLC0415

    monkeypatch.setattr(_registry, "STACK_REGISTRY", {})


def test_get_spec_returns_registered():
    spec = StackSpec(
        pattern="static_site",
        stack_dir="x",
        build_vars=lambda d: {},
        allowed_knobs=["is_spa"],
    )
    register(spec)
    assert get_spec("static_site") is spec


def test_get_spec_returns_none_for_unknown():
    assert get_spec("nope") is None


def test_list_supported_patterns_is_generated():
    register(StackSpec("a", "x", lambda d: {}, []))
    register(StackSpec("b", "x", lambda d: {}, []))
    assert sorted(list_supported_patterns()) == ["a", "b"]


def test_not_deployable_message_lists_supported():
    register(StackSpec("static_site", "x", lambda d: {}, []))
    from deploy_stacks import not_deployable_message

    msg = not_deployable_message("worker")
    assert "worker" in msg
    assert "static_site" in msg
```

- [ ] **Step 2:** Run tests — expect ImportError.

```bash
.venv/bin/python -m pytest tests/test_deploy_stacks.py -v
```

- [ ] **Step 3:** Implement.

```python
# aibuilder/deploy_stacks/__init__.py
"""Pattern → tofu stack registry.

Each catalog pattern that can be deployed registers a StackSpec here.
The registry is the source of truth for "what can aibuilder deploy
today" — `not_deployable_message` enumerates supported patterns from
the registry so the message can't drift from reality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from deploy_stacks import _registry


@dataclass(frozen=True)
class StackSpec:
    pattern: str
    stack_dir: str  # absolute path inside the image
    build_vars: Callable[[dict], dict]  # deployment-row dict -> tofu vars dict
    allowed_knobs: list[str]


def register(spec: StackSpec) -> None:
    _registry.STACK_REGISTRY[spec.pattern] = spec


def get_spec(pattern: str) -> StackSpec | None:
    return _registry.STACK_REGISTRY.get(pattern)


def list_supported_patterns() -> list[str]:
    return list(_registry.STACK_REGISTRY.keys())


def not_deployable_message(pattern: str) -> str:
    supported = sorted(list_supported_patterns())
    if not supported:
        return (
            f"`{pattern}` is not yet deployable — no patterns are wired up yet. "
            "This is a setup error; ask the maintainer."
        )
    return (
        f"`{pattern}` is not yet deployable by aibuilder. "
        f"Currently supported: {', '.join(f'`{p}`' for p in supported)}. "
        "Other patterns are coming in later waves."
    )
```

```python
# aibuilder/deploy_stacks/_registry.py
"""Mutable global. Split from __init__.py so tests can monkeypatch it."""

from __future__ import annotations

STACK_REGISTRY: dict = {}
```

- [ ] **Step 4:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_deploy_stacks.py -v
```

Expected: all PASS.

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/deploy_stacks/ aibuilder/tests/test_deploy_stacks.py
git commit -m "feat(aibuilder): pluggable StackSpec registry"
```

---

## Task 5: Limits / deploy caps

**Files:**
- Create: `aibuilder/limits.py`
- Test: `aibuilder/tests/test_limits.py`

- [ ] **Step 1:** Write failing tests.

```python
# aibuilder/tests/test_limits.py
import pytest

from deployments import SqliteDeploymentStore
from limits import check_caps


@pytest.fixture
def store(tmp_path):
    return SqliteDeploymentStore(tmp_path / "deploys.db")


def test_passes_when_under_caps(monkeypatch, store):
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", "5")
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY", "10")
    assert check_caps(store, session_id="s1") is None


def test_blocks_when_session_cap_reached(monkeypatch, store):
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", "2")
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY", "100")
    for i in range(2):
        store.create("s1", "u", "static_site", f"p{i}", "e", ttl_days=14)
    err = check_caps(store, session_id="s1")
    assert err is not None
    assert "session" in err["summary"].lower()
    assert "details" in err


def test_blocks_when_global_cap_reached(monkeypatch, store):
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", "100")
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY", "2")
    for i in range(2):
        store.create("s1", "u", "static_site", f"p{i}", "e", ttl_days=14)
    err = check_caps(store, session_id="other")
    assert err is not None
    assert "daily" in err["summary"].lower() or "global" in err["summary"].lower()


def test_uses_default_caps_when_env_unset(monkeypatch, store):
    monkeypatch.delenv("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", raising=False)
    monkeypatch.delenv("AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY", raising=False)
    # Defaults are 10 / 50; nothing in store, so we pass.
    assert check_caps(store, session_id="s1") is None
```

- [ ] **Step 2:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_limits.py -v
```

- [ ] **Step 3:** Implement.

```python
# aibuilder/limits.py
"""Deploy caps to keep sandbox spend bounded.

Per-session and global daily counters read from the deployments table.
Caps are env-configurable — defaults sized for a small team
prototyping. Failures use the standard {summary, details} shape so the
agent surfaces a friendly message via its existing convention.
"""

from __future__ import annotations

import os

from deployments import SqliteDeploymentStore

_DEFAULT_PER_SESSION = 10
_DEFAULT_GLOBAL = 50


def _session_cap() -> int:
    return int(os.environ.get("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", _DEFAULT_PER_SESSION))


def _global_cap() -> int:
    return int(os.environ.get("AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY", _DEFAULT_GLOBAL))


def check_caps(store: SqliteDeploymentStore, *, session_id: str) -> dict | None:
    """Return None if a new deploy may proceed, else {summary, details}."""
    session_cap = _session_cap()
    global_cap = _global_cap()
    session_count = store.count_today_for_session(session_id)
    global_count = store.count_today_global()
    if session_count >= session_cap:
        return {
            "summary": (
                f"This session has used its daily deploy budget "
                f"({session_count}/{session_cap}). Try again tomorrow or destroy "
                "an existing deployment to free a slot."
            ),
            "details": f"session={session_id} count={session_count} cap={session_cap}",
        }
    if global_count >= global_cap:
        return {
            "summary": (
                f"Global daily deploy cap reached ({global_count}/{global_cap}). "
                "Wait until tomorrow."
            ),
            "details": f"global_count={global_count} cap={global_cap}",
        }
    return None
```

- [ ] **Step 4:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_limits.py -v
```

Expected: all PASS.

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/limits.py aibuilder/tests/test_limits.py
git commit -m "feat(aibuilder): deploy caps (per-session + global daily)"
```

---

## Task 6: Job runner

**Files:**
- Create: `aibuilder/jobs.py`
- Test: `aibuilder/tests/test_jobs.py`

- [ ] **Step 1:** Write failing tests.

```python
# aibuilder/tests/test_jobs.py
import asyncio

import pytest

from jobs import JobQueue


@pytest.mark.asyncio
async def test_enqueue_runs_in_order():
    ran = []

    async def make_job(label):
        async def job():
            ran.append(label)
        return job

    q = JobQueue()
    await q.start()
    await q.enqueue(await make_job("a"))
    await q.enqueue(await make_job("b"))
    await q.drain()
    await q.stop()
    assert ran == ["a", "b"]


@pytest.mark.asyncio
async def test_failing_job_does_not_stop_queue():
    ran = []

    async def boom():
        raise RuntimeError("boom")

    async def ok():
        ran.append("ok")

    q = JobQueue()
    await q.start()
    await q.enqueue(boom)
    await q.enqueue(ok)
    await q.drain()
    await q.stop()
    assert ran == ["ok"]


@pytest.mark.asyncio
async def test_stop_idempotent():
    q = JobQueue()
    await q.start()
    await q.stop()
    await q.stop()  # second stop must not raise
```

- [ ] **Step 2:** Add `pytest-asyncio` to `requirements-dev.txt` if not present, then run tests.

```bash
cd "/Users/christophercorbin/INFRA prototypes/aibuilder"
grep -q '^pytest-asyncio' requirements-dev.txt || echo 'pytest-asyncio>=0.23' >> requirements-dev.txt
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/test_jobs.py -v
```

Expected: ImportError on `jobs`.

- [ ] **Step 3:** Implement.

```python
# aibuilder/jobs.py
"""In-process async job queue.

A single worker drains an asyncio.Queue of coroutine factories. Deploys
serialize — fine for a small team prototyping. Failures are logged but
never crash the worker. `drain()` is for tests; production lifespan
calls start()/stop() only.

This deliberately stays a module (not a class hierarchy) — chat and the
TTL reaper share the singleton via app.py state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

log = logging.getLogger("aibuilder.jobs")


class JobQueue:
    def __init__(self) -> None:
        self._q: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._worker is not None:
            return
        self._stopped.clear()
        self._worker = asyncio.create_task(self._run(), name="aibuilder-job-worker")

    async def enqueue(self, job: Callable[[], Awaitable[None]]) -> None:
        await self._q.put(job)

    async def drain(self) -> None:
        """Block until the queue is empty. For tests only."""
        await self._q.join()

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._stopped.set()
        # Send a sentinel so the worker wakes from .get() if it was waiting
        await self._q.put(None)
        await self._worker
        self._worker = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            job = await self._q.get()
            try:
                if job is None:
                    return
                await job()
            except Exception:
                log.exception("Job failed")
            finally:
                self._q.task_done()
```

- [ ] **Step 4:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_jobs.py -v
```

Expected: all PASS.

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/jobs.py aibuilder/tests/test_jobs.py aibuilder/requirements-dev.txt
git commit -m "feat(aibuilder): async job queue with single worker"
```

---

## Task 7: GitHub clone helper

**Files:**
- Create: `aibuilder/gh_clone.py`
- Test: `aibuilder/tests/test_gh_clone.py`

- [ ] **Step 1:** Write failing tests.

```python
# aibuilder/tests/test_gh_clone.py
from unittest.mock import patch

import pytest

from gh_clone import clone, _scrub_token


def test_scrub_strips_token_from_url():
    s = "fatal: clone of https://x-access-token:ghp_secret@github.com/foo/bar failed"
    assert "ghp_secret" not in _scrub_token(s, "ghp_secret")
    assert "<token>" in _scrub_token(s, "ghp_secret")


def test_scrub_handles_no_token_set():
    assert _scrub_token("plain text", None) == "plain text"


def test_clone_public_succeeds_first_try(tmp_path, monkeypatch):
    monkeypatch.delenv("AIBUILDER_GITHUB_TOKEN", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        from subprocess import CompletedProcess

        target = cmd[-1]
        import os

        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "README.md"), "w") as f:
            f.write("x")
        return CompletedProcess(cmd, 0, "", "")

    with patch("gh_clone.subprocess.run", side_effect=fake_run):
        path, err = clone("https://github.com/public/repo", tmp_path)
    assert err is None
    assert path is not None
    assert len(calls) == 1


def test_clone_private_retries_with_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_GITHUB_TOKEN", "ghp_xyz")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        from subprocess import CompletedProcess

        target = cmd[-1]
        if len(calls) == 1:
            return CompletedProcess(cmd, 128, "", "Repository not found")
        import os

        os.makedirs(target, exist_ok=True)
        return CompletedProcess(cmd, 0, "", "")

    with patch("gh_clone.subprocess.run", side_effect=fake_run):
        path, err = clone("https://github.com/govtech-bb/private", tmp_path)
    assert err is None
    assert len(calls) == 2
    # First call uses bare URL; second injects token
    assert "x-access-token" in calls[1][-2]


def test_clone_failure_scrubs_token_in_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_GITHUB_TOKEN", "ghp_xyz")

    def fake_run(cmd, **kw):
        from subprocess import CompletedProcess

        return CompletedProcess(cmd, 128, "", "fatal: ghp_xyz invalid")

    with patch("gh_clone.subprocess.run", side_effect=fake_run):
        _, err = clone("https://github.com/foo/bar", tmp_path)
    assert err is not None
    assert "ghp_xyz" not in err["details"]
```

- [ ] **Step 2:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_gh_clone.py -v
```

Expected: ImportError.

- [ ] **Step 3:** Implement.

```python
# aibuilder/gh_clone.py
"""Clone GitHub repos with optional private-repo retry.

First attempt is bare https. On failure (non-zero exit), if
AIBUILDER_GITHUB_TOKEN is set in the env, retry with the token
injected as `x-access-token:<token>@`. The token is scrubbed from any
error message before it leaves this module.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_GITHUB_PREFIX = re.compile(r"^https://github\.com/")


def _scrub_token(s: str, token: str | None) -> str:
    if not token:
        return s
    return s.replace(token, "<token>")


def _inject_token(url: str, token: str) -> str:
    return _GITHUB_PREFIX.sub(f"https://x-access-token:{token}@github.com/", url, count=1)


def clone(github_url: str, dest_dir: Path) -> tuple[Path | None, dict | None]:
    """Clone `github_url` into `dest_dir/<repo>`. Returns (path, error).

    Path is None and error is the {summary, details} dict on failure.
    """
    token = os.environ.get("AIBUILDER_GITHUB_TOKEN")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    repo_name = github_url.rstrip("/").split("/")[-1].removesuffix(".git")
    target = dest_dir / repo_name

    def _try(url: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "clone", "--depth=1", url, str(target)],
            capture_output=True,
            text=True,
            timeout=120,
        )

    r = _try(github_url)
    if r.returncode == 0:
        return target, None

    # Retry with token for github.com URLs
    if token and _GITHUB_PREFIX.match(github_url):
        if target.exists():
            import shutil

            shutil.rmtree(target)
        r = _try(_inject_token(github_url, token))
        if r.returncode == 0:
            return target, None

    return None, {
        "summary": "Could not clone the repository. Is the URL correct and accessible?",
        "details": _scrub_token(r.stderr.strip(), token)[-1000:],
    }
```

- [ ] **Step 4:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_gh_clone.py -v
```

Expected: all PASS.

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/gh_clone.py aibuilder/tests/test_gh_clone.py
git commit -m "feat(aibuilder): clone helper with private-repo token retry"
```

---

## Task 8: State backend infra

**Files:**
- Create: `aibuilder/infra/stacks/aibuilder-hosting/state.tf`
- Modify: `aibuilder/infra/stacks/aibuilder-hosting/iam.tf` (append a policy)

- [ ] **Step 1:** Create the state backend resources.

```hcl
# aibuilder/infra/stacks/aibuilder-hosting/state.tf
# Tofu state for deployed prototypes lives here. The hosting stack
# itself still uses local state — this is for the deploy engine's
# per-deployment state files (key = deployments/<project>-<env>.tfstate).

resource "aws_s3_bucket" "deploy_state" {
  bucket = "aibuilder-deploy-state-${data.aws_caller_identity.current.account_id}"
  tags   = { Name = "aibuilder-deploy-state" }
}

resource "aws_s3_bucket_versioning" "deploy_state" {
  bucket = aws_s3_bucket.deploy_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "deploy_state" {
  bucket = aws_s3_bucket.deploy_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "deploy_state" {
  bucket                  = aws_s3_bucket.deploy_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "deploy_lock" {
  name         = "aibuilder-deploy-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = { Name = "aibuilder-deploy-lock" }
}

output "deploy_state_bucket" {
  value = aws_s3_bucket.deploy_state.id
}

output "deploy_lock_table" {
  value = aws_dynamodb_table.deploy_lock.name
}
```

- [ ] **Step 2:** Append a state-access policy to the task role in `iam.tf`.

```hcl
# Append to aibuilder/infra/stacks/aibuilder-hosting/iam.tf
resource "aws_iam_role_policy" "task_deploy_state" {
  name = "${local.name}-task-deploy-state"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:ListBucket"]
        Resource = aws_s3_bucket.deploy_state.arn
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.deploy_state.arn}/*"
      },
      {
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.deploy_lock.arn
      },
    ]
  })
}
```

- [ ] **Step 3:** Plan + apply.

```bash
cd "/Users/christophercorbin/INFRA prototypes/aibuilder/infra/stacks/aibuilder-hosting"
AWS_PROFILE=govtech-sandbox tofu plan -var='github_oidc_provider_arn=arn:aws:iam::672203047922:oidc-provider/token.actions.githubusercontent.com' -no-color | tail -25
# Review: expect ~5 to add (bucket, versioning, sse, BPA, DDB) + 1 IAM policy add. No destroys.
AWS_PROFILE=govtech-sandbox tofu apply -auto-approve -var='github_oidc_provider_arn=arn:aws:iam::672203047922:oidc-provider/token.actions.githubusercontent.com' -no-color | tail -15
```

Expected: outputs include `deploy_state_bucket` and `deploy_lock_table`.

- [ ] **Step 4:** Commit.

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add aibuilder/infra/stacks/aibuilder-hosting/state.tf aibuilder/infra/stacks/aibuilder-hosting/iam.tf
git commit -m "feat(aibuilder/infra): S3+DDB tofu state backend for deploy jobs"
```

---

## Task 9: SSM github-token + ECS task wiring

**Files:**
- Modify: `aibuilder/infra/stacks/aibuilder-hosting/ssm.tf`
- Modify: `aibuilder/infra/stacks/aibuilder-hosting/ecs.tf`
- Modify: `aibuilder/infra/stacks/aibuilder-hosting/iam.tf` (task execution role gets read access to the new param)

- [ ] **Step 1:** Append the SSM parameter.

```hcl
# Append to aibuilder/infra/stacks/aibuilder-hosting/ssm.tf
resource "aws_ssm_parameter" "github_token" {
  name        = "/aibuilder/github-token"
  description = "Fine-grained GitHub PAT (read-only, govtech-bb org) for cloning private repos"
  type        = "SecureString"
  value       = "PLACEHOLDER_OVERWRITE_VIA_CLI"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = "${local.name}-github-token" }
}
```

- [ ] **Step 2:** Extend the task execution role's SSM read policy.

```hcl
# In aibuilder/infra/stacks/aibuilder-hosting/iam.tf — replace the existing
# task_execution_ssm policy block with this version (adds github_token ARN):
resource "aws_iam_role_policy" "task_execution_ssm" {
  name = "${local.name}-task-execution-ssm"
  role = aws_iam_role.task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["ssm:GetParameters"]
      Resource = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${aws_ssm_parameter.auth_token.name}",
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${aws_ssm_parameter.github_token.name}",
      ]
    }]
  })
}
```

- [ ] **Step 3:** Add the env var + secret + state-bucket env to the container in `ecs.tf` — edit the `container_definitions` block.

```hcl
# In ecs.tf container_definitions, extend `environment` and `secrets`:
environment = [
  { name = "AWS_REGION",                value = var.aws_region },
  { name = "AIBUILDER_BEDROCK_MODEL",   value = var.bedrock_model_id },
  { name = "AIBUILDER_DB",              value = "/aibuilder/data/sessions.db" },
  { name = "AIBUILDER_DEPLOYMENTS_DB",  value = "/aibuilder/data/deployments.db" },
  { name = "AIBUILDER_DEPLOY_STATE_BUCKET", value = aws_s3_bucket.deploy_state.id },
  { name = "AIBUILDER_DEPLOY_LOCK_TABLE",   value = aws_dynamodb_table.deploy_lock.name },
]

secrets = [
  { name = "AIBUILDER_TOKEN",         valueFrom = aws_ssm_parameter.auth_token.arn },
  { name = "AIBUILDER_GITHUB_TOKEN",  valueFrom = aws_ssm_parameter.github_token.arn },
]
```

- [ ] **Step 4:** Apply + put the real token.

```bash
cd "/Users/christophercorbin/INFRA prototypes/aibuilder/infra/stacks/aibuilder-hosting"
AWS_PROFILE=govtech-sandbox tofu plan -var='github_oidc_provider_arn=arn:aws:iam::672203047922:oidc-provider/token.actions.githubusercontent.com' -no-color | tail -15
AWS_PROFILE=govtech-sandbox tofu apply -auto-approve -var='github_oidc_provider_arn=arn:aws:iam::672203047922:oidc-provider/token.actions.githubusercontent.com' -no-color | tail -10
# User creates a fine-grained PAT in GitHub (read-only, govtech-bb org metadata + contents) and exports it:
read -s GH_TOKEN
aws ssm put-parameter --name /aibuilder/github-token --type SecureString --value "$GH_TOKEN" --overwrite --profile govtech-sandbox --region us-east-1
unset GH_TOKEN
```

Expected: parameter Version goes from 1 (placeholder) to 2 (real). Task definition will pick it up on next deploy.

- [ ] **Step 5:** Commit.

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add aibuilder/infra/stacks/aibuilder-hosting/{ssm.tf,ecs.tf,iam.tf}
git commit -m "feat(aibuilder/infra): GitHub token secret + state-backend env wiring"
```

---

## Task 10: Copy static-website tofu into deploy_stacks

**Files:**
- Create: `aibuilder/deploy_stacks/static_website/main.tf`
- Create: `aibuilder/deploy_stacks/static_website/variables.tf`
- Create: `aibuilder/deploy_stacks/static_website/outputs.tf`
- Create: `aibuilder/deploy_stacks/static_website/backend.tf`
- Create: `aibuilder/deploy_stacks/static_website/modules/s3-static-site/*`
- Create: `aibuilder/deploy_stacks/static_website/modules/cloudfront/*`

- [ ] **Step 1:** Copy the existing stack tree.

```bash
cd "/Users/christophercorbin/INFRA prototypes"
mkdir -p aibuilder/deploy_stacks/static_website/modules
cp infra/stacks/static-website/{main.tf,variables.tf,outputs.tf} aibuilder/deploy_stacks/static_website/
cp -R infra/modules/s3-static-site aibuilder/deploy_stacks/static_website/modules/
cp -R infra/modules/cloudfront aibuilder/deploy_stacks/static_website/modules/
```

- [ ] **Step 2:** Adjust module source paths in `aibuilder/deploy_stacks/static_website/main.tf`.

```
# Change in main.tf:
-  source = "../../modules/s3-static-site"
+  source = "./modules/s3-static-site"
-  source = "../../modules/cloudfront"
+  source = "./modules/cloudfront"
```

- [ ] **Step 3:** Add a partial S3 backend block.

```hcl
# aibuilder/deploy_stacks/static_website/backend.tf
# Bucket, key, region, dynamodb_table are passed via -backend-config at init time
# so the same stack tree serves every deployment with a distinct state key.
terraform {
  backend "s3" {}
}
```

- [ ] **Step 4:** Validate the copy is self-contained.

```bash
cd "/Users/christophercorbin/INFRA prototypes/aibuilder/deploy_stacks/static_website"
AWS_PROFILE=govtech-sandbox tofu init \
  -backend-config="bucket=aibuilder-deploy-state-672203047922" \
  -backend-config="key=deployments/_init-probe.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="dynamodb_table=aibuilder-deploy-lock" \
  -input=false
AWS_PROFILE=govtech-sandbox tofu validate
# Clean up the probe state — don't leave a stray empty state in the bucket
rm -rf .terraform .terraform.lock.hcl
aws s3 rm s3://aibuilder-deploy-state-672203047922/deployments/_init-probe.tfstate --profile govtech-sandbox 2>/dev/null || true
```

Expected: validate prints `Success!`.

- [ ] **Step 5:** Commit.

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add aibuilder/deploy_stacks/static_website/
git commit -m "feat(aibuilder): vendor static-website tofu stack with S3 backend"
```

---

## Task 11: Register the static_website pattern

**Files:**
- Create: `aibuilder/deploy_stacks/static_website.py` (Python wrapper, not the tofu)
- Modify: `aibuilder/deploy_stacks/__init__.py` (auto-import the wrapper at import time)
- Test: `aibuilder/tests/test_static_website_spec.py`

- [ ] **Step 1:** Write failing tests.

```python
# aibuilder/tests/test_static_website_spec.py
from deployments import Deployment, DeploymentStatus
from deploy_stacks import get_spec
import deploy_stacks.static_website  # noqa: F401  (registers on import)


def _fixture_deployment(**overrides):
    base = dict(
        deployment_id="d1",
        session_id="s",
        repo_url="https://github.com/foo/bar",
        pattern="static_site",
        project_name="bar",
        env="proto",
        status=DeploymentStatus.QUEUED,
        knobs={"is_spa": True, "price_class": "PriceClass_100"},
    )
    base.update(overrides)
    return Deployment(**base)


def test_static_site_pattern_registered():
    assert get_spec("static_site") is not None


def test_build_vars_includes_aibd_prefix_and_knobs():
    spec = get_spec("static_site")
    vars_ = spec.build_vars(_fixture_deployment())
    assert vars_["project_name"].startswith("aibd-")
    assert vars_["env"] == "proto"
    assert vars_["is_spa"] is True
    assert vars_["price_class"] == "PriceClass_100"


def test_allowed_knobs_includes_is_spa_and_price_class():
    spec = get_spec("static_site")
    assert "is_spa" in spec.allowed_knobs
    assert "price_class" in spec.allowed_knobs
```

- [ ] **Step 2:** Run tests.

```bash
cd "/Users/christophercorbin/INFRA prototypes/aibuilder"
.venv/bin/python -m pytest tests/test_static_website_spec.py -v
```

Expected: ImportError on `deploy_stacks.static_website`.

- [ ] **Step 3:** Implement the wrapper.

```python
# aibuilder/deploy_stacks/static_website.py
"""Register the static_site catalog pattern → vendored tofu stack.

The deploy task role's W1 IAM policy is scoped to `aibd-*` resource
names, so build_vars prepends that prefix to the project_name. Knobs
the agent is allowed to flip via modify_deployment are explicit.
"""

from __future__ import annotations

from pathlib import Path

from deploy_stacks import StackSpec, register
from deployments import Deployment

_STACK_DIR = str(Path(__file__).parent / "static_website")
_AIBD_PREFIX = "aibd-"


def _build_vars(d: Deployment) -> dict:
    return {
        "project_name": f"{_AIBD_PREFIX}{d.project_name}",
        "env": d.env,
        "is_spa": bool(d.knobs.get("is_spa", False)),
        "price_class": d.knobs.get("price_class", "PriceClass_100"),
        "site_title": d.knobs.get("site_title", ""),
        "owner_name": d.knobs.get("owner_name", ""),
        "owner_email": d.knobs.get("owner_email", ""),
    }


register(
    StackSpec(
        pattern="static_site",
        stack_dir=_STACK_DIR,
        build_vars=_build_vars,
        allowed_knobs=["is_spa", "price_class", "site_title", "owner_name", "owner_email"],
    )
)
```

- [ ] **Step 4:** Ensure the wrapper is auto-imported. Edit `aibuilder/deploy_stacks/__init__.py` and append at the bottom:

```python
# Register built-in patterns. New patterns add a sibling module here.
from deploy_stacks import static_website  # noqa: E402,F401
```

- [ ] **Step 5:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_static_website_spec.py -v
```

Expected: all PASS.

- [ ] **Step 6:** Commit.

```bash
git add aibuilder/deploy_stacks/static_website.py aibuilder/deploy_stacks/__init__.py aibuilder/tests/test_static_website_spec.py
git commit -m "feat(aibuilder): register static_site pattern with aibd- prefix"
```

---

## Task 12: deploy_repo tool + execute_tool wiring (no job yet)

**Files:**
- Modify: `aibuilder/tools.py`
- Test: `aibuilder/tests/test_tools_deploy.py`

- [ ] **Step 1:** Write failing test.

```python
# aibuilder/tests/test_tools_deploy.py
from unittest.mock import MagicMock

import pytest

from deployments import DeploymentStatus, SqliteDeploymentStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_DEPLOYMENTS_DB", str(tmp_path / "deploys.db"))
    return SqliteDeploymentStore(tmp_path / "deploys.db")


def test_deploy_repo_creates_queued_row_and_enqueues(store, monkeypatch):
    from tools import deploy_repo

    job_queue = MagicMock()
    enqueued = []

    async def fake_enqueue(fn):
        enqueued.append(fn)

    job_queue.enqueue = fake_enqueue
    monkeypatch.setattr("tools._JOB_QUEUE", job_queue)
    monkeypatch.setattr("tools._STORE", store)

    out = deploy_repo(
        github_url="https://github.com/foo/bar",
        pattern="static_site",
        project_name="bar",
        env="proto",
        knobs={"is_spa": True},
        session_id="s1",
        session=MagicMock(),
    )
    assert out["deployment_id"]
    assert out["status"] == "queued"
    listed = store.list_active()
    assert len(listed) == 1
    assert listed[0].status == DeploymentStatus.QUEUED


def test_deploy_repo_rejects_unknown_pattern(store, monkeypatch):
    from tools import deploy_repo

    monkeypatch.setattr("tools._STORE", store)
    out = deploy_repo(
        github_url="https://github.com/foo/bar",
        pattern="worker",  # not in registry
        project_name="bar",
        env="proto",
        knobs={},
        session_id="s1",
        session=MagicMock(),
    )
    assert "summary" in out
    assert "not yet deployable" in out["summary"].lower()


def test_deploy_repo_respects_cap(store, monkeypatch):
    from tools import deploy_repo

    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", "1")
    monkeypatch.setattr("tools._STORE", store)
    monkeypatch.setattr("tools._JOB_QUEUE", MagicMock(enqueue=lambda fn: None))
    # First deploy fills the cap
    store.create("s1", "u", "static_site", "p", "e", ttl_days=14)
    out = deploy_repo(
        github_url="https://github.com/foo/bar",
        pattern="static_site",
        project_name="bar",
        env="proto",
        knobs={},
        session_id="s1",
        session=MagicMock(),
    )
    assert "session" in out["summary"].lower()
```

- [ ] **Step 2:** Run test — expect AttributeError on `tools.deploy_repo`.

```bash
.venv/bin/python -m pytest tests/test_tools_deploy.py -v
```

- [ ] **Step 3:** Implement at the bottom of `aibuilder/tools.py`, before `TOOL_DEFINITIONS`.

```python
# Append to aibuilder/tools.py — after the existing tools, before TOOL_DEFINITIONS

import asyncio  # noqa: E402

from deploy_stacks import get_spec, not_deployable_message  # noqa: E402
from deployments import DeploymentStatus, SqliteDeploymentStore  # noqa: E402
from limits import check_caps  # noqa: E402

# Wired by app.py at startup so tools can reach the global store + queue.
_STORE: SqliteDeploymentStore | None = None
_JOB_QUEUE = None
_TTL_DAYS_DEFAULT = 14


def configure(store: SqliteDeploymentStore, job_queue) -> None:
    """Called from app.py's lifespan to wire singletons into the tools module."""
    global _STORE, _JOB_QUEUE
    _STORE = store
    _JOB_QUEUE = job_queue


def deploy_repo(
    github_url: str,
    pattern: str,
    project_name: str,
    env: str = "proto",
    knobs: dict | None = None,
    *,
    session_id: str,
    session=None,
    **_: Any,
) -> dict:
    if _STORE is None:
        return {"summary": "Deploy engine not initialized.", "details": "store unset"}
    if get_spec(pattern) is None:
        return {"summary": not_deployable_message(pattern), "details": f"pattern={pattern}"}
    cap_err = check_caps(_STORE, session_id=session_id)
    if cap_err:
        return cap_err

    d = _STORE.create(
        session_id=session_id,
        repo_url=github_url,
        pattern=pattern,
        project_name=project_name,
        env=env,
        ttl_days=_TTL_DAYS_DEFAULT,
    )
    d.knobs = knobs or {}
    _STORE.save(d)

    # Enqueue the deploy job. The actual job function is added in Task 13;
    # for now we register a placeholder so the chain is verifiable. Task 13
    # replaces this with the real run_deploy_job.
    from jobs_runtime import run_deploy_job  # late import to avoid cycle

    async def _job():
        await run_deploy_job(d.deployment_id)

    asyncio.get_event_loop().create_task(_JOB_QUEUE.enqueue(_job))

    return {
        "deployment_id": d.deployment_id,
        "status": d.status.value,
        "message": (
            f"Deployment {d.deployment_id} queued. Ask me 'how is the deploy going?' "
            "or check `get_deployment_status` for live updates."
        ),
    }
```

Stub the runtime module to make imports resolve cleanly until Task 13:

```python
# aibuilder/jobs_runtime.py
"""Job bodies — populated in Task 13. Stubbed here so imports resolve."""

from __future__ import annotations


async def run_deploy_job(deployment_id: str) -> None:
    raise NotImplementedError("run_deploy_job is implemented in Task 13")
```

- [ ] **Step 4:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_tools_deploy.py -v
```

Expected: PASS — the stubbed `run_deploy_job` is never called because the test mocks `_JOB_QUEUE.enqueue`.

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/tools.py aibuilder/jobs_runtime.py aibuilder/tests/test_tools_deploy.py
git commit -m "feat(aibuilder): deploy_repo tool (queues, caps, registry guard)"
```

---

## Task 13: Real deploy job body

**Files:**
- Modify: `aibuilder/jobs_runtime.py` (replace stub)
- Test: `aibuilder/tests/test_jobs_runtime.py`

- [ ] **Step 1:** Write failing test (mocks subprocess + boto3).

```python
# aibuilder/tests/test_jobs_runtime.py
import json
from unittest.mock import MagicMock, patch

import pytest

from deployments import DeploymentStatus, SqliteDeploymentStore
import deploy_stacks.static_website  # noqa: F401


@pytest.fixture
def store(tmp_path):
    s = SqliteDeploymentStore(tmp_path / "deploys.db")
    return s


@pytest.mark.asyncio
async def test_run_deploy_job_happy_path(store, tmp_path, monkeypatch):
    from jobs_runtime import run_deploy_job
    import jobs_runtime

    monkeypatch.setattr("jobs_runtime._STORE", store)
    monkeypatch.setenv("AIBUILDER_DEPLOY_WORKDIR", str(tmp_path / "deploys"))
    monkeypatch.setenv("AIBUILDER_DEPLOY_STATE_BUCKET", "test-bucket")
    monkeypatch.setenv("AIBUILDER_DEPLOY_LOCK_TABLE", "test-lock")

    d = store.create("s", "https://github.com/foo/bar", "static_site", "bar", "proto", ttl_days=14)

    async def fake_clone(url, dest):
        path = tmp_path / "clones" / "bar"
        path.mkdir(parents=True, exist_ok=True)
        (path / "index.html").write_text("<html></html>")
        return path, None

    def fake_subprocess(cmd, **kw):
        from subprocess import CompletedProcess

        if "output" in cmd:
            return CompletedProcess(
                cmd,
                0,
                json.dumps(
                    {
                        "bucket_name": {"value": "aibd-bar-proto-static"},
                        "site_url": {"value": "https://d123.cloudfront.net"},
                        "cloudfront_distribution_id": {"value": "E123"},
                    }
                ),
                "",
            )
        return CompletedProcess(cmd, 0, "", "")

    async def fake_sync(*a, **kw):
        return None

    with (
        patch("jobs_runtime.gh_clone.clone", side_effect=lambda url, dest: (
            __import__("asyncio").get_event_loop().run_until_complete(fake_clone(url, dest))
        )),
        patch("jobs_runtime.subprocess.run", side_effect=fake_subprocess),
        patch("jobs_runtime.sync_content", side_effect=fake_sync),
    ):
        await run_deploy_job(d.deployment_id)

    loaded = store.get(d.deployment_id)
    assert loaded.status == DeploymentStatus.LIVE
    assert "site_url" in loaded.outputs


@pytest.mark.asyncio
async def test_run_deploy_job_records_failure(store, tmp_path, monkeypatch):
    from jobs_runtime import run_deploy_job

    monkeypatch.setattr("jobs_runtime._STORE", store)
    monkeypatch.setenv("AIBUILDER_DEPLOY_WORKDIR", str(tmp_path / "deploys"))

    d = store.create("s", "https://github.com/foo/bar", "static_site", "bar", "proto", ttl_days=14)

    with patch(
        "jobs_runtime.gh_clone.clone",
        return_value=(None, {"summary": "clone failed", "details": "nope"}),
    ):
        await run_deploy_job(d.deployment_id)

    loaded = store.get(d.deployment_id)
    assert loaded.status == DeploymentStatus.FAILED
    assert "clone failed" in loaded.last_error
```

- [ ] **Step 2:** Run.

```bash
.venv/bin/python -m pytest tests/test_jobs_runtime.py -v
```

Expected: FAIL — stub raises NotImplementedError.

- [ ] **Step 3:** Implement.

```python
# aibuilder/jobs_runtime.py
"""Job bodies. Imports are kept local to avoid circular deps with tools.py."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

import boto3
import mimetypes

import gh_clone
from deploy_stacks import get_spec
from deployments import Deployment, DeploymentStatus, SqliteDeploymentStore
from errors import classify_error

log = logging.getLogger("aibuilder.jobs")
_STORE: SqliteDeploymentStore | None = None


def configure(store: SqliteDeploymentStore) -> None:
    global _STORE
    _STORE = store


def _workdir(deployment_id: str) -> Path:
    root = Path(os.environ.get("AIBUILDER_DEPLOY_WORKDIR", "/aibuilder/data/deploys"))
    p = root / deployment_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _update(d: Deployment, status: DeploymentStatus, error: str | None = None) -> None:
    d.status = status
    if error is not None:
        d.last_error = error
    _STORE.save(d)


async def run_deploy_job(deployment_id: str) -> None:
    d = _STORE.get(deployment_id)
    if d is None:
        log.error("deployment %s vanished before job ran", deployment_id)
        return

    spec = get_spec(d.pattern)
    if spec is None:
        _update(d, DeploymentStatus.FAILED, f"pattern not registered: {d.pattern}")
        return

    work = _workdir(deployment_id)
    state_bucket = os.environ.get("AIBUILDER_DEPLOY_STATE_BUCKET", "")
    lock_table = os.environ.get("AIBUILDER_DEPLOY_LOCK_TABLE", "")

    # 1. Clone
    _update(d, DeploymentStatus.CLONING)
    repo_path, err = gh_clone.clone(d.repo_url, work / "src")
    if err:
        _update(d, DeploymentStatus.FAILED, err["summary"] + " :: " + err["details"])
        return

    # 2. Apply
    _update(d, DeploymentStatus.APPLYING)
    state_key = f"deployments/{d.project_name}-{d.env}.tfstate"
    env = {
        **os.environ,
        "TF_DATA_DIR": str(work / "tf"),
    }
    init = subprocess.run(
        [
            "tofu", "init", "-input=false",
            f"-backend-config=bucket={state_bucket}",
            f"-backend-config=key={state_key}",
            f"-backend-config=region={os.environ.get('AWS_REGION', 'us-east-1')}",
            f"-backend-config=dynamodb_table={lock_table}",
        ],
        cwd=spec.stack_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    if init.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(init.stderr)["details"])
        return

    var_args: list[str] = []
    for k, v in spec.build_vars(d).items():
        if isinstance(v, bool):
            var_args += [f"-var={k}={'true' if v else 'false'}"]
        else:
            var_args += [f"-var={k}={v}"]

    apply_res = subprocess.run(
        ["tofu", "apply", "-auto-approve", "-input=false", *var_args],
        cwd=spec.stack_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=900,
    )
    if apply_res.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(apply_res.stderr)["details"])
        return

    out_res = subprocess.run(
        ["tofu", "output", "-json"],
        cwd=spec.stack_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    outputs_raw = json.loads(out_res.stdout) if out_res.stdout else {}
    d.outputs = {k: v.get("value") for k, v in outputs_raw.items()}

    # 3. Sync content
    _update(d, DeploymentStatus.SYNCING)
    sync_err = await sync_content(d, repo_path)
    if sync_err:
        _update(d, DeploymentStatus.FAILED, sync_err["summary"] + " :: " + sync_err["details"])
        return

    _update(d, DeploymentStatus.LIVE)


async def sync_content(d: Deployment, repo_path: Path) -> dict | None:
    """W1 only: boto3 sync the cloned repo to the deployment's bucket + invalidate CF.

    Returns None on success or {summary, details} on error. Runs in a thread
    pool because boto3 is sync.
    """
    bucket = d.outputs.get("bucket_name")
    distribution = d.outputs.get("cloudfront_distribution_id")
    if not bucket:
        return {"summary": "tofu output missing bucket_name.", "details": str(d.outputs)}

    def _sync() -> dict | None:
        try:
            s3 = boto3.client("s3")
            cf = boto3.client("cloudfront")
            for p in sorted(Path(repo_path).rglob("*")):
                if not p.is_file() or any(part.startswith(".git") for part in p.parts):
                    continue
                key = str(p.relative_to(repo_path))
                content_type, _ = mimetypes.guess_type(str(p))
                s3.upload_file(
                    str(p), bucket, key,
                    ExtraArgs={"ContentType": content_type or "application/octet-stream"},
                )
            if distribution:
                cf.create_invalidation(
                    DistributionId=distribution,
                    InvalidationBatch={
                        "Paths": {"Quantity": 1, "Items": ["/*"]},
                        "CallerReference": f"aibuilder-{d.deployment_id}",
                    },
                )
            return None
        except Exception as e:
            return {"summary": "Content sync failed.", "details": str(e)}

    return await asyncio.to_thread(_sync)
```

- [ ] **Step 4:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_jobs_runtime.py -v
```

Expected: both PASS. If the happy-path test trips on the async event-loop trick, simplify the mock — call `clone` synchronously and patch `jobs_runtime.gh_clone.clone` to return `(tmp_path/'clones'/'bar', None)` directly.

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/jobs_runtime.py aibuilder/tests/test_jobs_runtime.py
git commit -m "feat(aibuilder): deploy job (clone → tofu apply → sync → live)"
```

---

## Task 14: status + list tools

**Files:**
- Modify: `aibuilder/tools.py`
- Test: extend `aibuilder/tests/test_tools_deploy.py`

- [ ] **Step 1:** Append tests.

```python
# Append to tests/test_tools_deploy.py
from datetime import datetime, timedelta, timezone


def test_get_deployment_status_returns_row(store, monkeypatch):
    from tools import get_deployment_status

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    out = get_deployment_status(deployment_id=d.deployment_id, session_id="s", session=None)
    assert out["deployment_id"] == d.deployment_id
    assert out["status"] == "queued"


def test_get_deployment_status_404(store, monkeypatch):
    from tools import get_deployment_status

    monkeypatch.setattr("tools._STORE", store)
    out = get_deployment_status(deployment_id="nope", session_id="s", session=None)
    assert "summary" in out


def test_list_deployments_includes_ttl_remaining(store, monkeypatch):
    from tools import list_deployments

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.expires_at = datetime.now(timezone.utc) + timedelta(days=2)
    store.save(d)
    out = list_deployments(session_id="s", session=None)
    rows = out["deployments"]
    assert any(r["deployment_id"] == d.deployment_id for r in rows)
    row = next(r for r in rows if r["deployment_id"] == d.deployment_id)
    assert row["ttl_hours_remaining"] > 0
    assert row["ttl_hours_remaining"] < 100  # ~48h
```

- [ ] **Step 2:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_tools_deploy.py -v
```

Expected: 3 new fails on AttributeError.

- [ ] **Step 3:** Implement.

```python
# Append to aibuilder/tools.py
from datetime import datetime, timezone  # noqa: E402


def get_deployment_status(
    deployment_id: str, *, session_id: str, session=None, **_: Any
) -> dict:
    if _STORE is None:
        return {"summary": "Deploy engine not initialized.", "details": ""}
    d = _STORE.get(deployment_id)
    if d is None:
        return {"summary": f"No deployment `{deployment_id}` found.", "details": ""}
    return _deployment_row(d)


def list_deployments(*, session_id: str, session=None, **_: Any) -> dict:
    if _STORE is None:
        return {"summary": "Deploy engine not initialized.", "details": ""}
    rows = [_deployment_row(d) for d in _STORE.list_active()]
    return {"deployments": rows}


def _deployment_row(d) -> dict:
    now = datetime.now(timezone.utc)
    remaining_hours = max(0, int((d.expires_at - now).total_seconds() // 3600))
    return {
        "deployment_id": d.deployment_id,
        "session_id": d.session_id,
        "repo_url": d.repo_url,
        "pattern": d.pattern,
        "project_name": d.project_name,
        "env": d.env,
        "status": d.status.value,
        "outputs": d.outputs,
        "knobs": d.knobs,
        "ttl_hours_remaining": remaining_hours,
        "warn_expiring_soon": remaining_hours < 48,
        "last_error": d.last_error,
    }
```

- [ ] **Step 4:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_tools_deploy.py -v
```

Expected: all PASS.

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/tools.py aibuilder/tests/test_tools_deploy.py
git commit -m "feat(aibuilder): status + list tools with TTL countdown"
```

---

## Task 15: redeploy / modify / destroy / extend tools

**Files:**
- Modify: `aibuilder/tools.py`
- Modify: `aibuilder/jobs_runtime.py`
- Test: extend `aibuilder/tests/test_tools_deploy.py`, `aibuilder/tests/test_jobs_runtime.py`

- [ ] **Step 1:** Append tool-level tests.

```python
# Append to tests/test_tools_deploy.py
@pytest.fixture
def mock_queue(monkeypatch):
    q = MagicMock()
    q.enqueue = MagicMock(return_value=None)
    monkeypatch.setattr("tools._JOB_QUEUE", q)
    return q


def test_redeploy_enqueues_for_live_only(store, monkeypatch, mock_queue):
    from tools import redeploy

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    out = redeploy(deployment_id=d.deployment_id, session_id="s", session=None)
    assert "summary" in out  # not live yet — refuse
    d.status = DeploymentStatus.LIVE
    store.save(d)
    out = redeploy(deployment_id=d.deployment_id, session_id="s", session=None)
    assert out["status"] in ("queued", "syncing")


def test_modify_rejects_unknown_knob(store, monkeypatch, mock_queue):
    from tools import modify_deployment

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    store.save(d)
    out = modify_deployment(
        deployment_id=d.deployment_id,
        changes={"haha_not_a_knob": True},
        session_id="s",
        session=None,
    )
    assert "knob" in out["summary"].lower()


def test_modify_accepts_allowed_knob(store, monkeypatch, mock_queue):
    from tools import modify_deployment

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    store.save(d)
    out = modify_deployment(
        deployment_id=d.deployment_id,
        changes={"is_spa": True},
        session_id="s",
        session=None,
    )
    assert out["status"] in ("queued", "modifying")
    loaded = store.get(d.deployment_id)
    assert loaded.knobs["is_spa"] is True


def test_destroy_two_phase(store, monkeypatch, mock_queue):
    from tools import destroy_deployment

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    store.save(d)
    preview = destroy_deployment(deployment_id=d.deployment_id, session_id="s", session=None)
    assert preview["preview"] is True
    confirmed = destroy_deployment(
        deployment_id=d.deployment_id, confirm=True, session_id="s", session=None
    )
    assert confirmed["status"] in ("queued", "destroying")


def test_extend_resets_clock(store, monkeypatch):
    from tools import extend_deployment

    monkeypatch.setattr("tools._STORE", store)
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    d.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    store.save(d)
    out = extend_deployment(deployment_id=d.deployment_id, session_id="s", session=None)
    assert out["ttl_hours_remaining"] > 200  # 14 days = 336h
```

- [ ] **Step 2:** Run — expect 5 fails.

```bash
.venv/bin/python -m pytest tests/test_tools_deploy.py -v
```

- [ ] **Step 3:** Implement tools + matching job runtimes.

Append to `aibuilder/tools.py`:

```python
from deploy_stacks import get_spec  # already imported above; re-stated for clarity
from datetime import timedelta  # noqa: E402


def redeploy(deployment_id: str, *, session_id: str, session=None, **_: Any) -> dict:
    if _STORE is None:
        return {"summary": "Deploy engine not initialized.", "details": ""}
    d = _STORE.get(deployment_id)
    if d is None:
        return {"summary": f"No deployment `{deployment_id}`.", "details": ""}
    if d.status != DeploymentStatus.LIVE:
        return {
            "summary": (
                f"Deployment is in status `{d.status.value}` — redeploy is only valid "
                "from `live`. Try `get_deployment_status` first."
            ),
            "details": "",
        }
    from jobs_runtime import run_redeploy_job

    asyncio.get_event_loop().create_task(_JOB_QUEUE.enqueue(lambda: run_redeploy_job(d.deployment_id)))
    return {"deployment_id": d.deployment_id, "status": "queued"}


def modify_deployment(
    deployment_id: str, changes: dict, *, session_id: str, session=None, **_: Any
) -> dict:
    if _STORE is None:
        return {"summary": "Deploy engine not initialized.", "details": ""}
    d = _STORE.get(deployment_id)
    if d is None:
        return {"summary": f"No deployment `{deployment_id}`.", "details": ""}
    spec = get_spec(d.pattern)
    if spec is None:
        return {"summary": f"Pattern `{d.pattern}` has no spec.", "details": ""}
    bad = [k for k in changes if k not in spec.allowed_knobs]
    if bad:
        return {
            "summary": (
                f"These knob(s) aren't modifiable for `{d.pattern}`: "
                f"{', '.join('`' + k + '`' for k in bad)}. Allowed: "
                f"{', '.join('`' + k + '`' for k in spec.allowed_knobs)}."
            ),
            "details": "",
        }
    d.knobs.update(changes)
    _STORE.save(d)
    from jobs_runtime import run_modify_job

    asyncio.get_event_loop().create_task(_JOB_QUEUE.enqueue(lambda: run_modify_job(d.deployment_id)))
    return {"deployment_id": d.deployment_id, "status": "queued"}


def destroy_deployment(
    deployment_id: str, confirm: bool = False, *, session_id: str, session=None, **_: Any
) -> dict:
    if _STORE is None:
        return {"summary": "Deploy engine not initialized.", "details": ""}
    d = _STORE.get(deployment_id)
    if d is None:
        return {"summary": f"No deployment `{deployment_id}`.", "details": ""}
    if not confirm:
        site = d.outputs.get("site_url", "—")
        return {
            "preview": True,
            "message": (
                f"Will destroy `{d.project_name}-{d.env}` (pattern: {d.pattern}, "
                f"site: {site}). Reply `confirm destroy {deployment_id[:8]}` to proceed."
            ),
            "deployment_id": deployment_id,
        }
    from jobs_runtime import run_destroy_job

    asyncio.get_event_loop().create_task(_JOB_QUEUE.enqueue(lambda: run_destroy_job(d.deployment_id)))
    return {"deployment_id": d.deployment_id, "status": "queued"}


def extend_deployment(deployment_id: str, *, session_id: str, session=None, **_: Any) -> dict:
    if _STORE is None:
        return {"summary": "Deploy engine not initialized.", "details": ""}
    d = _STORE.extend(deployment_id, days=_TTL_DAYS_DEFAULT)
    if d is None:
        return {"summary": f"No deployment `{deployment_id}`.", "details": ""}
    return _deployment_row(d)
```

Append to `aibuilder/jobs_runtime.py`:

```python
async def run_redeploy_job(deployment_id: str) -> None:
    d = _STORE.get(deployment_id)
    if d is None:
        return
    _update(d, DeploymentStatus.CLONING)
    work = _workdir(deployment_id)
    if (work / "src").exists():
        import shutil
        shutil.rmtree(work / "src")
    repo_path, err = gh_clone.clone(d.repo_url, work / "src")
    if err:
        _update(d, DeploymentStatus.FAILED, err["summary"] + " :: " + err["details"])
        return
    _update(d, DeploymentStatus.SYNCING)
    sync_err = await sync_content(d, repo_path)
    if sync_err:
        _update(d, DeploymentStatus.FAILED, sync_err["summary"])
        return
    _update(d, DeploymentStatus.LIVE)


async def run_modify_job(deployment_id: str) -> None:
    d = _STORE.get(deployment_id)
    if d is None:
        return
    spec = get_spec(d.pattern)
    if spec is None:
        _update(d, DeploymentStatus.FAILED, f"no spec for {d.pattern}")
        return
    _update(d, DeploymentStatus.MODIFYING)
    work = _workdir(deployment_id)
    env = {**os.environ, "TF_DATA_DIR": str(work / "tf")}
    state_key = f"deployments/{d.project_name}-{d.env}.tfstate"
    state_bucket = os.environ.get("AIBUILDER_DEPLOY_STATE_BUCKET", "")
    lock_table = os.environ.get("AIBUILDER_DEPLOY_LOCK_TABLE", "")
    init = subprocess.run(
        ["tofu", "init", "-input=false", "-reconfigure",
         f"-backend-config=bucket={state_bucket}",
         f"-backend-config=key={state_key}",
         f"-backend-config=region={os.environ.get('AWS_REGION', 'us-east-1')}",
         f"-backend-config=dynamodb_table={lock_table}"],
        cwd=spec.stack_dir, capture_output=True, text=True, env=env, timeout=180,
    )
    if init.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(init.stderr)["details"])
        return
    var_args = [f"-var={k}={'true' if v is True else 'false' if v is False else v}"
                for k, v in spec.build_vars(d).items()]
    apply_res = subprocess.run(
        ["tofu", "apply", "-auto-approve", "-input=false", *var_args],
        cwd=spec.stack_dir, capture_output=True, text=True, env=env, timeout=900,
    )
    if apply_res.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(apply_res.stderr)["details"])
        return
    _update(d, DeploymentStatus.LIVE)


async def run_destroy_job(deployment_id: str) -> None:
    d = _STORE.get(deployment_id)
    if d is None:
        return
    spec = get_spec(d.pattern)
    if spec is None:
        _update(d, DeploymentStatus.FAILED, f"no spec for {d.pattern}")
        return
    _update(d, DeploymentStatus.DESTROYING)

    # Empty bucket first if known
    bucket = d.outputs.get("bucket_name")
    if bucket:
        err = await asyncio.to_thread(_empty_bucket_sync, bucket)
        if err:
            _update(d, DeploymentStatus.FAILED, err["summary"])
            return

    work = _workdir(deployment_id)
    env = {**os.environ, "TF_DATA_DIR": str(work / "tf")}
    state_key = f"deployments/{d.project_name}-{d.env}.tfstate"
    state_bucket = os.environ.get("AIBUILDER_DEPLOY_STATE_BUCKET", "")
    lock_table = os.environ.get("AIBUILDER_DEPLOY_LOCK_TABLE", "")
    init = subprocess.run(
        ["tofu", "init", "-input=false", "-reconfigure",
         f"-backend-config=bucket={state_bucket}",
         f"-backend-config=key={state_key}",
         f"-backend-config=region={os.environ.get('AWS_REGION', 'us-east-1')}",
         f"-backend-config=dynamodb_table={lock_table}"],
        cwd=spec.stack_dir, capture_output=True, text=True, env=env, timeout=180,
    )
    if init.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(init.stderr)["details"])
        return
    var_args = [f"-var={k}={'true' if v is True else 'false' if v is False else v}"
                for k, v in spec.build_vars(d).items()]
    destroy_res = subprocess.run(
        ["tofu", "destroy", "-auto-approve", "-input=false", *var_args],
        cwd=spec.stack_dir, capture_output=True, text=True, env=env, timeout=600,
    )
    if destroy_res.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(destroy_res.stderr)["details"])
        return
    _update(d, DeploymentStatus.DESTROYED)


def _empty_bucket_sync(bucket: str) -> dict | None:
    import botocore.exceptions

    try:
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        return None
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "NoSuchBucket":
            return None
        return {"summary": f"Could not empty bucket {bucket}.", "details": str(e)}
```

- [ ] **Step 4:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_tools_deploy.py -v
```

Expected: all PASS. (Note: `asyncio.get_event_loop().create_task(...)` is fine in tests because the mock `_JOB_QUEUE.enqueue` returns `None`, so the coroutine completes immediately.)

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/tools.py aibuilder/jobs_runtime.py aibuilder/tests/test_tools_deploy.py
git commit -m "feat(aibuilder): redeploy, modify, destroy, extend tools"
```

---

## Task 16: TTL reaper

**Files:**
- Create: `aibuilder/reaper.py`
- Test: `aibuilder/tests/test_reaper.py`

- [ ] **Step 1:** Write failing tests.

```python
# aibuilder/tests/test_reaper.py
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from deployments import DeploymentStatus, SqliteDeploymentStore
from reaper import sweep_once


@pytest.fixture
def store(tmp_path):
    return SqliteDeploymentStore(tmp_path / "deploys.db")


@pytest.mark.asyncio
async def test_sweep_enqueues_destroy_for_expired(store):
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    d.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    store.save(d)
    enqueued = []

    async def fake_enqueue(fn):
        enqueued.append(fn)

    class Q:
        enqueue = staticmethod(fake_enqueue)

    n = await sweep_once(store, Q())
    assert n == 1
    assert len(enqueued) == 1
    loaded = store.get(d.deployment_id)
    assert loaded.status == DeploymentStatus.EXPIRED


@pytest.mark.asyncio
async def test_sweep_skips_non_expired(store):
    d = store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    store.save(d)
    n = await sweep_once(store, type("Q", (), {"enqueue": staticmethod(lambda fn: asyncio.sleep(0))})())
    assert n == 0
```

- [ ] **Step 2:** Run.

```bash
.venv/bin/python -m pytest tests/test_reaper.py -v
```

- [ ] **Step 3:** Implement.

```python
# aibuilder/reaper.py
"""Hourly TTL sweep. Marks expired deployments EXPIRED and enqueues destroy jobs."""

from __future__ import annotations

import asyncio
import logging

from deployments import DeploymentStatus, SqliteDeploymentStore

log = logging.getLogger("aibuilder.reaper")
_INTERVAL_SECONDS = 3600


async def sweep_once(store: SqliteDeploymentStore, queue) -> int:
    expired = store.list_expired()
    for d in expired:
        from jobs_runtime import run_destroy_job  # local import: same-cycle avoidance

        async def _job(did=d.deployment_id):
            await run_destroy_job(did)

        await queue.enqueue(_job)
        d.status = DeploymentStatus.EXPIRED
        store.save(d)
    return len(expired)


async def run_loop(store: SqliteDeploymentStore, queue, *, interval: int = _INTERVAL_SECONDS) -> None:
    while True:
        try:
            n = await sweep_once(store, queue)
            if n:
                log.info("reaper: enqueued destroy for %d expired deployments", n)
        except Exception:
            log.exception("reaper sweep failed")
        await asyncio.sleep(interval)
```

- [ ] **Step 4:** Run.

```bash
.venv/bin/python -m pytest tests/test_reaper.py -v
```

Expected: PASS.

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/reaper.py aibuilder/tests/test_reaper.py
git commit -m "feat(aibuilder): TTL reaper background sweep"
```

---

## Task 17: HTTP endpoints + lifespan wiring

**Files:**
- Modify: `aibuilder/app.py`
- Test: `aibuilder/tests/test_endpoints_deploy.py`

- [ ] **Step 1:** Write failing tests.

```python
# aibuilder/tests/test_endpoints_deploy.py
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("AIBUILDER_DEPLOYMENTS_DB", str(tmp_path / "d.db"))
    monkeypatch.delenv("AIBUILDER_TOKEN", raising=False)
    # Reset module-level singletons by reloading
    import importlib

    import app

    importlib.reload(app)
    return TestClient(app.app), app


def test_get_deployment_404(client):
    c, _ = client
    r = c.get("/api/deployments/nope")
    assert r.status_code == 404


def test_get_deployment_returns_row(client):
    c, app_mod = client
    d = app_mod.deployment_store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    r = c.get(f"/api/deployments/{d.deployment_id}")
    assert r.status_code == 200
    assert r.json()["deployment_id"] == d.deployment_id


def test_redeploy_endpoint_202_when_live(client):
    c, app_mod = client
    from deployments import DeploymentStatus

    d = app_mod.deployment_store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    d.status = DeploymentStatus.LIVE
    app_mod.deployment_store.save(d)
    with patch("app.tools.redeploy", return_value={"deployment_id": d.deployment_id, "status": "queued"}):
        r = c.post(f"/api/deployments/{d.deployment_id}/redeploy")
    assert r.status_code == 202
    assert r.json()["status"] == "queued"


def test_redeploy_endpoint_4xx_when_not_live(client):
    c, app_mod = client
    d = app_mod.deployment_store.create("s", "u", "static_site", "p", "e", ttl_days=14)
    with patch("app.tools.redeploy", return_value={"summary": "not live", "details": ""}):
        r = c.post(f"/api/deployments/{d.deployment_id}/redeploy")
    assert r.status_code == 409
```

- [ ] **Step 2:** Run — expect import + attribute errors.

```bash
.venv/bin/python -m pytest tests/test_endpoints_deploy.py -v
```

- [ ] **Step 3:** Modify `aibuilder/app.py`.

Add at the top (after existing imports):

```python
from contextlib import asynccontextmanager

import tools
from deployments import SqliteDeploymentStore
from jobs import JobQueue
import jobs_runtime
import reaper as reaper_module
```

Add new module-level state right after `store = SqliteSessionStore(_DB_PATH)`:

```python
_DEPLOY_DB_PATH = Path(
    os.environ.get("AIBUILDER_DEPLOYMENTS_DB", Path(__file__).parent / "data" / "deployments.db")
)
deployment_store = SqliteDeploymentStore(_DEPLOY_DB_PATH)
job_queue = JobQueue()
```

Replace the `app = FastAPI(title="aibuilder")` line with a lifespan-wired construction:

```python
@asynccontextmanager
async def lifespan(_app):
    deployment_store.recover_in_flight()
    jobs_runtime.configure(deployment_store)
    tools.configure(deployment_store, job_queue)
    await job_queue.start()
    reaper_task = asyncio.create_task(reaper_module.run_loop(deployment_store, job_queue))
    try:
        yield
    finally:
        reaper_task.cancel()
        await job_queue.stop()


app = FastAPI(title="aibuilder", lifespan=lifespan)
```

Add the two new endpoints before the static-mount line:

```python
@app.get("/api/deployments/{deployment_id}")
def deployment_status(deployment_id: str) -> dict:
    d = deployment_store.get(deployment_id)
    if d is None:
        raise HTTPException(404, f"No deployment {deployment_id}")
    return tools._deployment_row(d)


@app.post("/api/deployments/{deployment_id}/redeploy")
def trigger_redeploy(deployment_id: str) -> JSONResponse:
    d = deployment_store.get(deployment_id)
    if d is None:
        raise HTTPException(404, f"No deployment {deployment_id}")
    out = tools.redeploy(deployment_id=deployment_id, session_id=d.session_id)
    if "summary" in out:
        return JSONResponse(status_code=409, content=out)
    return JSONResponse(status_code=202, content=out)
```

Add an `import asyncio` at the top of the file if not already present.

- [ ] **Step 4:** Run tests.

```bash
.venv/bin/python -m pytest tests/test_endpoints_deploy.py -v
```

Expected: PASS.

- [ ] **Step 5:** Commit.

```bash
git add aibuilder/app.py aibuilder/tests/test_endpoints_deploy.py
git commit -m "feat(aibuilder): lifespan-wired job queue + reaper + deploy endpoints"
```

---

## Task 18: Agent tool definitions + system prompt

**Files:**
- Modify: `aibuilder/tools.py` (extend `TOOL_DEFINITIONS` and `execute_tool`)
- Modify: `aibuilder/agent.py` (extend `SYSTEM_PROMPT`)
- Test: extend existing tests if needed; no new test file.

- [ ] **Step 1:** Append to `TOOL_DEFINITIONS` in `aibuilder/tools.py`.

```python
TOOL_DEFINITIONS.extend([
    {
        "name": "deploy_repo",
        "description": (
            "Deploy a previously analyzed repo to AWS using the catalog pattern. "
            "Returns a deployment_id immediately; the actual apply runs in the "
            "background. Use get_deployment_status to check progress."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "github_url": {"type": "string"},
                "pattern": {"type": "string", "description": "Catalog pattern key, e.g. static_site"},
                "project_name": {"type": "string", "description": "URL-safe slug derived from the site name"},
                "env": {"type": "string", "default": "proto"},
                "knobs": {"type": "object", "description": "Pattern-specific options (e.g. {is_spa: true})"},
            },
            "required": ["github_url", "pattern", "project_name"],
        },
    },
    {
        "name": "get_deployment_status",
        "description": "Look up one deployment by its deployment_id.",
        "input_schema": {
            "type": "object",
            "properties": {"deployment_id": {"type": "string"}},
            "required": ["deployment_id"],
        },
    },
    {
        "name": "list_deployments",
        "description": "List active (non-destroyed) deployments with TTL remaining.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "redeploy",
        "description": "Re-clone and re-sync content for a live deployment without re-running tofu.",
        "input_schema": {
            "type": "object",
            "properties": {"deployment_id": {"type": "string"}},
            "required": ["deployment_id"],
        },
    },
    {
        "name": "modify_deployment",
        "description": "Apply chat-driven infra knob changes (e.g. {is_spa: true}) to a live deployment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment_id": {"type": "string"},
                "changes": {"type": "object"},
            },
            "required": ["deployment_id", "changes"],
        },
    },
    {
        "name": "destroy_deployment",
        "description": (
            "Two-phase destroy. confirm=false returns a preview; confirm=true tears down "
            "the deployment. Always preview first and surface the message to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "deployment_id": {"type": "string"},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["deployment_id"],
        },
    },
    {
        "name": "extend_deployment",
        "description": "Reset the TTL clock on a deployment to 14 days from now.",
        "input_schema": {
            "type": "object",
            "properties": {"deployment_id": {"type": "string"}},
            "required": ["deployment_id"],
        },
    },
])
```

Append to `execute_tool` dispatch in `aibuilder/tools.py`:

```python
def execute_tool(name: str, inputs: dict, *, session_id: str, session) -> dict:
    # ... existing branches ...
    if name == "deploy_repo":
        return deploy_repo(session=session, session_id=session_id, **inputs)
    if name == "get_deployment_status":
        return get_deployment_status(session=session, session_id=session_id, **inputs)
    if name == "list_deployments":
        return list_deployments(session=session, session_id=session_id, **inputs)
    if name == "redeploy":
        return redeploy(session=session, session_id=session_id, **inputs)
    if name == "modify_deployment":
        return modify_deployment(session=session, session_id=session_id, **inputs)
    if name == "destroy_deployment":
        return destroy_deployment(session=session, session_id=session_id, **inputs)
    if name == "extend_deployment":
        return extend_deployment(session=session, session_id=session_id, **inputs)
    return {"summary": f"Unknown tool: {name}", "details": ""}
```

- [ ] **Step 2:** Extend `SYSTEM_PROMPT` in `aibuilder/agent.py`. Add a new section after the existing 4-stage workflow:

```python
# Append inside SYSTEM_PROMPT (read the file and find the closing """ — add this
# block right before that triple-quote)

5. **Deploy stage (when the user says "yes deploy it" or similar):**
   - Confirm pattern + project name with the user once.
   - Call deploy_repo. It returns immediately with a deployment_id.
   - Tell the user the deploy is queued and they can ask "how's the deploy
     going?" any time. Don't pretend it's done.

6. **Status / list:**
   - Use get_deployment_status when the user asks about a specific deploy.
   - Use list_deployments when they ask "what's live?" or "list everything."

7. **Update (code changed):**
   - Confirm they pushed to GitHub. Then call redeploy.
   - If the deployment isn't `live` yet, tell them to wait — don't redeploy
     a half-applied stack.

8. **Update (config changed):**
   - Use modify_deployment with allowed knobs only. The tool will reject
     unknown knobs — surface that rejection verbatim if it happens.

9. **Destroy:**
   - Always call destroy_deployment(confirm=false) first.
   - Relay the preview message verbatim. Wait for the user to confirm.
   - Then call with confirm=true.

10. **Extend:**
    - Use extend_deployment when the user wants more time on a deployment
      that's about to expire.

CRITICAL: do NOT invent deployment IDs. Use list_deployments to find them.
Do NOT call modify_deployment with knobs you didn't see in the tool's
allowed_knobs error message or the pattern's recommended config.
```

- [ ] **Step 3:** Run the full test suite to confirm nothing regressed.

```bash
cd "/Users/christophercorbin/INFRA prototypes/aibuilder"
.venv/bin/python -m pytest -q
```

Expected: all PASS, test count grew.

- [ ] **Step 4:** Commit.

```bash
git add aibuilder/tools.py aibuilder/agent.py
git commit -m "feat(aibuilder): wire deploy tools into TOOL_DEFINITIONS + system prompt"
```

---

## Task 19: Provisioning IAM for W1 (S3 + CloudFront, scoped to aibd-*)

**Files:**
- Modify: `aibuilder/infra/stacks/aibuilder-hosting/iam.tf`

- [ ] **Step 1:** Append the W1 provisioning policy.

```hcl
# Append to aibuilder/infra/stacks/aibuilder-hosting/iam.tf

resource "aws_iam_role_policy" "task_deploy_w1" {
  name = "${local.name}-task-deploy-w1"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 buckets named aibd-*
      {
        Effect   = "Allow"
        Action   = [
          "s3:CreateBucket", "s3:DeleteBucket", "s3:GetBucket*", "s3:PutBucket*",
          "s3:ListBucket", "s3:ListBucketVersions",
          "s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:DeleteObjectVersion",
          "s3:GetEncryptionConfiguration", "s3:PutEncryptionConfiguration",
          "s3:GetBucketPolicy", "s3:PutBucketPolicy", "s3:DeleteBucketPolicy",
          "s3:GetBucketPublicAccessBlock", "s3:PutBucketPublicAccessBlock",
          "s3:GetBucketTagging", "s3:PutBucketTagging",
          "s3:GetBucketVersioning", "s3:PutBucketVersioning",
        ]
        Resource = [
          "arn:aws:s3:::aibd-*",
          "arn:aws:s3:::aibd-*/*",
        ]
      },
      # CloudFront distributions (CF doesn't support resource-level ARNs for create —
      # so * here is required, but the SG of "what aibuilder creates" is implicit by
      # being the only thing it deploys.)
      {
        Effect   = "Allow"
        Action   = [
          "cloudfront:CreateDistribution", "cloudfront:UpdateDistribution",
          "cloudfront:DeleteDistribution", "cloudfront:GetDistribution",
          "cloudfront:GetDistributionConfig",
          "cloudfront:CreateInvalidation", "cloudfront:GetInvalidation",
          "cloudfront:ListDistributions",
          "cloudfront:CreateOriginAccessControl", "cloudfront:GetOriginAccessControl",
          "cloudfront:UpdateOriginAccessControl", "cloudfront:DeleteOriginAccessControl",
          "cloudfront:ListOriginAccessControls",
          "cloudfront:TagResource", "cloudfront:UntagResource", "cloudfront:ListTagsForResource",
        ]
        Resource = "*"
      },
      # CloudFront needs to assume a service-linked role for some operations; allow PassRole there.
      {
        Effect = "Allow"
        Action = ["iam:CreateServiceLinkedRole"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "iam:AWSServiceName" = "cloudfront.amazonaws.com"
          }
        }
      },
    ]
  })
}
```

- [ ] **Step 2:** Plan + apply.

```bash
cd "/Users/christophercorbin/INFRA prototypes/aibuilder/infra/stacks/aibuilder-hosting"
AWS_PROFILE=govtech-sandbox tofu plan -var='github_oidc_provider_arn=arn:aws:iam::672203047922:oidc-provider/token.actions.githubusercontent.com' -no-color | tail -10
AWS_PROFILE=govtech-sandbox tofu apply -auto-approve -var='github_oidc_provider_arn=arn:aws:iam::672203047922:oidc-provider/token.actions.githubusercontent.com' -no-color | tail -5
```

Expected: 1 IAM policy added.

- [ ] **Step 3:** Commit.

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add aibuilder/infra/stacks/aibuilder-hosting/iam.tf
git commit -m "feat(aibuilder/infra): W1 provisioning IAM (S3 aibd-*, CloudFront)"
```

---

## Task 20: Local smoke

**Files:** none (manual verification).

- [ ] **Step 1:** Compose up.

```bash
cd "/Users/christophercorbin/INFRA prototypes/aibuilder"
# Make sure local env doesn't shadow the cloud SSM token
export AIBUILDER_TOKEN=
export AIBUILDER_DEPLOY_STATE_BUCKET=aibuilder-deploy-state-672203047922
export AIBUILDER_DEPLOY_LOCK_TABLE=aibuilder-deploy-lock
docker compose down
docker compose up -d --build
until curl -fsS http://localhost:8001/api/health 2>/dev/null; do sleep 1; done
```

Expected: `{"status":"ok"}` returns.

- [ ] **Step 2:** Pick a tiny public static-site repo (e.g. `https://github.com/govtech-bb/storm-ready-checklist` or any `index.html`-only fixture). Open http://localhost:8001 in a browser, paste the bearer token, and chat:

```
Deploy https://github.com/<owner>/<repo>. Project name: smoketest. It's a static site.
```

Expected: agent calls deploy_repo, returns a deployment_id, and tells you to ask for status.

- [ ] **Step 3:** Check status until live.

```
What's the status of the smoketest deploy?
```

Expected: agent calls get_deployment_status, eventually returns `status: live` with a `site_url`. Visit the URL in a browser — page renders.

- [ ] **Step 4:** Modify.

```
Make smoketest an SPA.
```

Expected: agent calls modify_deployment with `{"is_spa": true}`, status goes through modifying → live.

- [ ] **Step 5:** Redeploy via the endpoint.

```bash
curl -X POST -H "Authorization: Bearer $AIBUILDER_TOKEN_VALUE" \
  http://localhost:8001/api/deployments/<deployment_id>/redeploy
```

Expected: 202 + status JSON. Subsequent GET shows syncing → live.

- [ ] **Step 6:** Destroy.

```
Destroy smoketest.
```

Expected: agent calls destroy_deployment(confirm=false), surfaces the preview, you confirm, agent calls confirm=true. After ~5 min, status is destroyed. Bucket and CF distribution are gone.

- [ ] **Step 7:** Compose down + cleanup any orphans.

```bash
docker compose down
aws s3 ls --profile govtech-sandbox | grep aibd-smoketest || echo "no aibd-smoketest buckets — clean"
aws cloudfront list-distributions --profile govtech-sandbox --query "DistributionList.Items[?Comment=='aibd-smoketest-proto'].Id" --output text
```

Expected: no orphan resources.

---

## Task 21: Ship to cloud

**Files:** none (orchestration).

- [ ] **Step 1:** Push branch.

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git push -u origin aibuilder-deploy-spec
```

- [ ] **Step 2:** Open PR via gh.

```bash
gh pr create --base main --head aibuilder-deploy-spec \
  --title "aibuilder: deploy + modify (W0 + W1)" \
  --body "Implements the W0 foundation (S3 state, deployments store, job runner, registry, caps, private clone, TTL reaper) and W1 static-site pattern. Spec: docs/superpowers/specs/2026-06-11-aibuilder-deploy-design.md."
```

- [ ] **Step 3:** Wait for CI green, merge.

```bash
gh pr checks --watch
gh pr merge --rebase --delete-branch
```

Expected: `aibuilder-deploy.yml` deploy job fires on main, rolls the ECS service to the new image.

- [ ] **Step 4:** Cloud smoke. Hit the live URL, paste the bearer token, repeat the same deploy → modify → redeploy → destroy loop against a public repo. Verify the live image has `tofu` (`docker exec` into the task via SSM or check via the chat: "what's the deploy stack dir?").

- [ ] **Step 5:** Update memory.

```
project_deploy_agent_state.md → mark W0+W1 live, add the new endpoints,
private repo token now in SSM /aibuilder/github-token.
```

---

## Self-review notes

- **Spec coverage:** every numbered component in `docs/superpowers/specs/2026-06-11-aibuilder-deploy-design.md` is implemented somewhere in Tasks 1–19. Smoke + ship is Tasks 20–21.
- **No placeholders:** every code block is complete; every `commit` step has a real message.
- **Type consistency:** `DeploymentStatus` enum is shared from `deployments.py`; tools and runtime use the same values; `Deployment.knobs` is a dict throughout; `StackSpec.build_vars` always takes a `Deployment` and returns `dict[str, Any]`.
- **TDD discipline:** every task except infra-apply ones (8, 9, 10, 19) starts with a failing test; integration tasks (20, 21) are explicit manual smoke steps.
- **Commit cadence:** one commit per task. The branch ends up with ~19 focused commits — easy to review, easy to bisect.
