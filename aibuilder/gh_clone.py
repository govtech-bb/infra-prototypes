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

    if github_url.startswith("-"):
        return None, {
            "summary": "Invalid GitHub URL.",
            "details": "URL cannot start with '-'.",
        }

    def _try(url: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "clone", "--depth=1", "--", url, str(target)],
            capture_output=True,
            text=True,
            timeout=120,
        )

    r = _try(github_url)
    if r.returncode == 0:
        return target, None

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
