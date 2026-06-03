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
            "alternative' or 'estimate both options'): add `pattern_override` to the "
            "profile dict with one of these exact catalog keys: 'static_site', "
            "'spa_with_api', 'node_api', 'python_api', 'dockerized_web', "
            "'fullstack_with_db', 'nextjs_amplify_hosting', 'worker'. When "
            "pattern_override is set, the routing inference is skipped and the named "
            "pattern is returned directly. Call recommend_architecture once per "
            "pattern you want to compare, then estimate_cost on each."
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


_TOOL_FUNCS = {
    "clone_repo": clone_repo,
    "analyze_repo": analyze_repo,
    "recommend_architecture": recommend_architecture,
    "estimate_cost": estimate_cost,
}


def execute_tool(name: str, args: dict, *, session_id: str, session: Any) -> dict:
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        return {"summary": f"Unknown tool: {name}", "details": ""}
    return fn(**args, session_id=session_id, session=session)
