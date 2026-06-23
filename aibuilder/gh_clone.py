# aibuilder/gh_clone.py
"""Clone GitHub repos with optional private-repo retry.

First attempt is bare https. On failure (non-zero exit), if a GitHub
App installation token is available (via gh_app.get_installation_token),
retry with the token injected as `x-access-token:<token>@`. The token
is scrubbed from any error message before it leaves this module.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_GITHUB_PREFIX = re.compile(r"^https://github\.com/")


def _get_token_or_none() -> str | None:
    """Mint a GitHub App installation token, or return None if the App isn't configured.

    Falling back to None matches the public-repo path — clone proceeds without
    auth and only fails if the repo is private (in which case the error will
    say so and the user can configure the App).
    """
    try:
        from gh_app import GhAppNotConfigured, get_installation_token
    except ImportError as e:
        print(f"[gh_clone] gh_app import failed: {e}", flush=True)
        return None
    try:
        return get_installation_token()
    except GhAppNotConfigured as e:
        print(f"[gh_clone] gh_app not configured: {e}", flush=True)
        return None
    except Exception as e:
        # GhAppAuthFailed, network, key-format, etc. Logged so the next CloudWatch
        # query shows what's going on; still falls back to None so public-repo
        # clones don't crash when the App isn't usable.
        print(f"[gh_clone] gh_app token mint failed ({type(e).__name__}): {e}", flush=True)
        return None


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
    token = _get_token_or_none()
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
