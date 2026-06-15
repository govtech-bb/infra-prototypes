"""Tool implementations for the aibuilder agent.

Each tool returns either a success dict or a {"summary", "details"}
error dict — never raises. The agent's system prompt teaches it to
surface `summary` verbatim and offer `details` only if asked.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from analyzer import analyze_repo as _analyze
from patterns import recommend as _recommend
from pricing import estimate as _estimate

_TMP_DIR_DEFAULT = Path(__file__).parent / "tmp" / "repos"
_GITHUB_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
_MAX_FILES_DEFAULT = 5000
_MAX_SIZE_MB_DEFAULT = 500


def _tmp_root() -> Path:
    return Path(os.environ.get("AIBUILDER_TMP_DIR", str(_TMP_DIR_DEFAULT)))


def _max_files() -> int:
    return int(os.environ.get("AIBUILDER_MAX_FILES", _MAX_FILES_DEFAULT))


def _max_size_mb() -> int:
    return int(os.environ.get("AIBUILDER_MAX_SIZE_MB", _MAX_SIZE_MB_DEFAULT))


def clone_repo(github_url: str, *, session_id: str, **_: Any) -> dict:
    match = _GITHUB_URL_RE.match(github_url.strip())
    if not match:
        return {
            "summary": "That doesn't look like a GitHub repo URL. "
            "Try `https://github.com/<owner>/<repo>`.",
            "details": f"received: {github_url!r}",
        }

    owner = match.group("owner")
    repo_name = match.group("repo")
    # The regex allows "." / ".." since `.` is a literal in `[\w.-]+`. Reject
    # those (and any name made of only dots) to prevent path traversal — without
    # this, a URL like `https://github.com/foo/..` would resolve `target` to
    # the tmp root and shutil.rmtree the whole thing.
    if any(seg in ("", ".", "..") or set(seg) == {"."} for seg in (owner, repo_name)):
        return {
            "summary": "That doesn't look like a GitHub repo URL. "
            "Try `https://github.com/<owner>/<repo>`.",
            "details": f"received: {github_url!r}",
        }

    session_root = (_tmp_root() / session_id).resolve()
    target = session_root / repo_name
    # Defense in depth: even after the dot-segment check, confirm the resolved
    # target sits inside the session dir before doing destructive operations.
    try:
        target.resolve().relative_to(session_root)
    except ValueError:
        return {
            "summary": "That doesn't look like a GitHub repo URL. "
            "Try `https://github.com/<owner>/<repo>`.",
            "details": f"received: {github_url!r}",
        }
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        ["git", "clone", "--depth=1", github_url, str(target)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        stderr = proc.stderr or ""
        if "not found" in stderr.lower() or "could not read" in stderr.lower():
            return {
                "summary": "I couldn't reach that repo. "
                "I can only see public repos right now — make sure the URL is correct and public.",
                "details": stderr[-2000:],
            }
        return {
            "summary": "Cloning the repo failed.",
            "details": stderr[-2000:],
        }

    files = [p for p in target.rglob("*") if p.is_file() and ".git" not in p.parts]
    file_count = len(files)
    if file_count > _max_files():
        shutil.rmtree(target, ignore_errors=True)
        return {
            "summary": "This repo is too large for me to scan in one go. "
            "Point me at the subfolder for the app you want to deploy.",
            "details": f"file_count={file_count}, limit={_max_files()}",
        }

    size_bytes = sum(p.stat().st_size for p in files if p.exists())
    size_mb = size_bytes / (1024 * 1024)
    if size_mb > _max_size_mb():
        shutil.rmtree(target, ignore_errors=True)
        return {
            "summary": "This repo is too large for me to scan in one go. "
            "Point me at the subfolder for the app you want to deploy.",
            "details": f"size_mb={size_mb:.1f}, limit={_max_size_mb()}",
        }

    return {
        "path": str(target),
        "repo_name": repo_name,
        "file_count": file_count,
        "size_mb": round(size_mb, 2),
    }


def analyze_repo(path: str, **_: Any) -> dict:
    profile = _analyze(path)
    return profile.to_dict()


def _filter_to_dataclass_fields(data: dict, cls: type) -> dict:
    """Drop keys the dataclass doesn't know about.

    The LLM occasionally invents extra keys (e.g. `force_pattern`) when it
    wants behavior the tool doesn't support; without this filter the
    `cls(**data)` call raises TypeError and the whole chat falls over. Quietly
    swallowing unknowns gives the LLM a graceful path: the call still
    succeeds, the bogus key just doesn't affect routing.
    """
    from dataclasses import fields

    valid = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in valid}


def recommend_architecture(profile: dict, **_: Any) -> dict:
    from analyzer import RepoProfile
    from patterns import _CATALOG

    # Optional first-class override: the LLM can ask for a specific pattern by
    # name (e.g. user said "show me the Fargate alternative" — agent calls
    # recommend_architecture with pattern_override="fullstack_with_db"). If
    # the named pattern exists in the catalog, return it directly without
    # running the inference routing.
    override = profile.get("pattern_override")
    if override and override in _CATALOG:
        return _CATALOG[override].to_dict()

    rp = RepoProfile(**_filter_to_dataclass_fields(profile, RepoProfile))
    return _recommend(rp).to_dict()


def estimate_cost(architecture: dict, **_: Any) -> dict:
    from patterns import Architecture, ArchitectureService

    services = []
    for s in architecture.get("services", []):
        services.append(ArchitectureService(**_filter_to_dataclass_fields(s, ArchitectureService)))
    arch = Architecture(
        pattern=architecture.get("pattern", "unknown"),
        services=services,
        notes=list(architecture.get("notes", [])),
    )
    return _estimate(arch).to_dict()


# ── Tool registry ────────────────────────────────────────────────────────────


def _catalog_pattern_keys_csv() -> str:
    """Render the live catalog's pattern keys as a comma-separated string for
    embedding in the tool description. Generated at import time so adding a
    new pattern to _CATALOG automatically makes it discoverable to the agent —
    no need to update this docstring separately and no risk of drift."""
    from patterns import _CATALOG

    return ", ".join(f"'{k}'" for k in _CATALOG)


TOOL_DEFINITIONS = [
    {
        "name": "clone_repo",
        "description": (
            "Clone a public GitHub repository to a temporary directory so it can be analyzed. "
            "Returns the local path. Use this first, before analyze_repo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "github_url": {
                    "type": "string",
                    "description": "Full HTTPS URL like https://github.com/<owner>/<repo>",
                },
            },
            "required": ["github_url"],
        },
    },
    {
        "name": "analyze_repo",
        "description": (
            "Inspect a cloned repo and classify the app. Returns a RepoProfile dict "
            "(app_type, languages, frameworks, has_dockerfile, has_database_hints, "
            "summary, etc.). Always present the `summary` to the user verbatim and ask "
            "them to confirm before recommending an architecture."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Local filesystem path from clone_repo",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "recommend_architecture",
        "description": (
            "Given a RepoProfile, return the recommended AWS architecture as a list of "
            "named services with per-service purpose and sizing. Do NOT invent services — "
            "the returned services list is authoritative.\n\n"
            "To show an ALTERNATIVE pattern (e.g. user asks 'show me the Fargate "
            "alternative' or 'estimate both options' or 'use the workflow_worker "
            "pattern'): add `pattern_override` to the profile dict with one of these "
            f"exact catalog keys: {_catalog_pattern_keys_csv()}. When pattern_override "
            "is set, the routing inference is skipped and the named pattern is returned "
            "directly. Call recommend_architecture once per pattern you want to compare, "
            "then estimate_cost on each."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "object",
                    "description": "The RepoProfile dict returned by analyze_repo, "
                    "with any user corrections applied (e.g., user said they also "
                    "use a database → set has_database_hints to true). Optionally "
                    "include `pattern_override` (string) to skip routing and return "
                    "a specific catalog pattern by name.",
                },
            },
            "required": ["profile"],
        },
    },
    {
        "name": "estimate_cost",
        "description": (
            "Given an Architecture dict from recommend_architecture, return a monthly "
            "cost estimate with per-service breakdown and the assumptions used. Do NOT "
            "invent dollar amounts — the returned numbers are authoritative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "architecture": {
                    "type": "object",
                    "description": "The Architecture dict returned by recommend_architecture",
                },
            },
            "required": ["architecture"],
        },
    },
]

TOOL_DEFINITIONS.extend(
    [
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
                    "pattern": {
                        "type": "string",
                        "description": "Catalog pattern key, e.g. static_site",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "URL-safe slug derived from the site name",
                    },
                    "env": {"type": "string", "default": "proto"},
                    "knobs": {
                        "type": "object",
                        "description": "Pattern-specific options (e.g. {is_spa: true})",
                    },
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
    ]
)


_TOOL_FUNCS = {
    "clone_repo": clone_repo,
    "analyze_repo": analyze_repo,
    "recommend_architecture": recommend_architecture,
    "estimate_cost": estimate_cost,
    "deploy_repo": lambda **kw: deploy_repo(**kw),
    "get_deployment_status": lambda **kw: get_deployment_status(**kw),
    "list_deployments": lambda **kw: list_deployments(**kw),
    "redeploy": lambda **kw: redeploy(**kw),
    "modify_deployment": lambda **kw: modify_deployment(**kw),
    "destroy_deployment": lambda **kw: destroy_deployment(**kw),
    "extend_deployment": lambda **kw: extend_deployment(**kw),
}


def execute_tool(name: str, args: dict, *, session_id: str, session: Any) -> dict:
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        return {"summary": f"Unknown tool: {name}", "details": ""}
    return fn(**args, session_id=session_id, session=session)


# ── Deploy tool ──────────────────────────────────────────────────────────────

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

    from jobs_runtime import run_deploy_job  # late import to avoid cycle

    _enqueue_job(lambda: run_deploy_job(d.deployment_id))

    return {
        "deployment_id": d.deployment_id,
        "status": d.status.value,
        "message": (
            f"Deployment {d.deployment_id} queued. Ask me 'how is the deploy going?' "
            "or check `get_deployment_status` for live updates."
        ),
    }


from datetime import UTC, datetime  # noqa: E402


def get_deployment_status(deployment_id: str, *, session_id: str, session=None, **_: Any) -> dict:
    """Return one deployment's status. Team-wide visibility by design.

    `session_id` is a browser-session identifier, NOT an auth principal — the
    bearer token in app.py already gates all /api/* access. Multiple sessions
    from the same teammate (or different teammates sharing the token) all see
    each other's deployments deliberately, so anyone on the team can destroy
    or redeploy any prototype. If per-user identity ever lands, scope here.
    """
    if _STORE is None:
        return {"summary": "Deploy engine not initialized.", "details": ""}
    d = _STORE.get(deployment_id)
    if d is None:
        return {"summary": f"No deployment `{deployment_id}` found.", "details": ""}
    return _deployment_row(d)


def list_deployments(*, session_id: str, session=None, **_: Any) -> dict:
    """List active deployments (team-wide, no session scoping by design).

    See get_deployment_status's docstring — `session_id` is not an auth
    boundary. Returning all active deployments is the intended UX so
    teammates can see + manage each other's prototypes.
    """
    if _STORE is None:
        return {"summary": "Deploy engine not initialized.", "details": ""}
    rows = [_deployment_row(d) for d in _STORE.list_active()]
    return {"deployments": rows}


def _deployment_row(d) -> dict:
    now = datetime.now(UTC)
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


def _enqueue_job(factory) -> None:
    """Schedule a coroutine factory on the running loop; degrade safely in sync tests."""

    async def _wrapper():
        await factory()

    coro = _JOB_QUEUE.enqueue(_wrapper)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)  # noqa: RUF006
    except RuntimeError:
        if hasattr(coro, "close"):
            coro.close()


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

    _enqueue_job(lambda: run_redeploy_job(d.deployment_id))
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

    _enqueue_job(lambda: run_modify_job(d.deployment_id))
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

    _enqueue_job(lambda: run_destroy_job(d.deployment_id))
    return {"deployment_id": d.deployment_id, "status": "queued"}


def extend_deployment(deployment_id: str, *, session_id: str, session=None, **_: Any) -> dict:
    if _STORE is None:
        return {"summary": "Deploy engine not initialized.", "details": ""}
    d = _STORE.extend(deployment_id, days=_TTL_DAYS_DEFAULT)
    if d is None:
        return {"summary": f"No deployment `{deployment_id}`.", "details": ""}
    return _deployment_row(d)
