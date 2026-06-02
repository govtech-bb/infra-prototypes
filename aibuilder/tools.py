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

    repo_name = match.group("repo")
    target = _tmp_root() / session_id / repo_name
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


def recommend_architecture(profile: dict, **_: Any) -> dict:
    from analyzer import RepoProfile

    rp = RepoProfile(**profile)
    return _recommend(rp).to_dict()


def estimate_cost(architecture: dict, **_: Any) -> dict:
    from patterns import Architecture, ArchitectureService

    arch = Architecture(
        pattern=architecture.get("pattern", "unknown"),
        services=[ArchitectureService(**s) for s in architecture.get("services", [])],
        notes=list(architecture.get("notes", [])),
    )
    return _estimate(arch).to_dict()
