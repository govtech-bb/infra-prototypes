"""GitHub App auth — mint installation tokens for clone_repo to use.

Why GitHub App instead of a PAT: tokens auto-rotate every hour, the App
is owned by the govtech-bb org (survives team changes), audit logs show
the App as the actor. The tradeoff is ~30 more lines of code + 3 SSM
secrets instead of 1.

Flow per fetch:
  1. Sign a JWT with the App's RSA private key (RS256, max 10-min life).
     Claims: iat, exp, iss = APP_ID.
  2. POST that JWT to /app/installations/{install_id}/access_tokens.
     Response: {"token": "ghs_...", "expires_at": "..."} — token valid ~1h.

We cache the installation token in-memory and refresh after 55 minutes
(5-min safety margin). Tokens are process-local — every Fargate task
fetches its own.
"""

from __future__ import annotations

import os
import time

import jwt as pyjwt
import requests

_TOKEN_TTL_SECONDS = 55 * 60  # GitHub installation tokens last ~1h; refresh at 55m
_JWT_TTL_SECONDS = 9 * 60  # GitHub allows ≤ 10 min; use 9 to dodge clock skew

# Module-level cache. One token per process — fine for the single Fargate task.
_cached_token: str | None = None
_cached_token_minted_at: float = 0.0


class GhAppNotConfigured(RuntimeError):
    """Raised when the AIBUILDER_GITHUB_APP_* env vars aren't all set."""


class GhAppAuthFailed(RuntimeError):
    """Raised when GitHub rejects the JWT or installation-token request."""


def _config() -> tuple[str, str, str]:
    app_id = os.environ.get("AIBUILDER_GITHUB_APP_ID")
    install_id = os.environ.get("AIBUILDER_GITHUB_APP_INSTALLATION_ID")
    private_key = os.environ.get("AIBUILDER_GITHUB_APP_PRIVATE_KEY")
    if not (app_id and install_id and private_key):
        raise GhAppNotConfigured(
            "AIBUILDER_GITHUB_APP_ID, AIBUILDER_GITHUB_APP_INSTALLATION_ID, "
            "and AIBUILDER_GITHUB_APP_PRIVATE_KEY must all be set."
        )
    return app_id, install_id, private_key


def _sign_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {
        "iat": now - 30,  # 30-sec backdate to tolerate small clock skew
        "exp": now + _JWT_TTL_SECONDS,
        "iss": app_id,  # PyJWT 2.x requires string; GitHub accepts either
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256")


def get_installation_token() -> str:
    """Return a fresh-enough installation token, fetching from GitHub if needed."""
    global _cached_token, _cached_token_minted_at

    if _cached_token and (time.time() - _cached_token_minted_at) < _TOKEN_TTL_SECONDS:
        return _cached_token

    app_id, install_id, private_key = _config()
    jwt_token = _sign_jwt(app_id, private_key)

    r = requests.post(
        f"https://api.github.com/app/installations/{install_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    if r.status_code != 201:
        raise GhAppAuthFailed(
            f"GitHub returned {r.status_code} fetching installation token: {r.text[:200]}"
        )

    body = r.json()
    _cached_token = body["token"]
    _cached_token_minted_at = time.time()
    return _cached_token
