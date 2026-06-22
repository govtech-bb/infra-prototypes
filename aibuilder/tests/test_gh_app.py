# aibuilder/tests/test_gh_app.py
import time
from unittest.mock import MagicMock, patch

import pytest

# Test keypair generated for tests only — never used in production.
# This is a 1024-bit RSA key (smaller = faster test runs). Production keys
# from GitHub Apps are 2048-bit; the code doesn't care about size.
_TEST_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIICXQIBAAKBgQDVrJ2YOuLm6XQH8K8Lz4F8x7qFy0e2ZJh5e6oF7FvWh0v0n3LE
yK+1QbDdJqJG4DJ9G7+1eC3l0G6Lh4hZ8wXxF1L7w/dRJ9o6kF0rFqL5xHv5tnZc
6yxJ3yEKwL3vDjE3LK3DqUSk7B7QO5jGwt5o9Y6P/Z6JqVqEZ3VbOlMpwIDAQAB
AoGAMHJL5p9TpZF3D7zVoY8qQT4N6cR3VHJ8X7g5HJ4kF0fX5tH3hKZHJpY7v7Z6
JqVqEZ3VbOlMpwK7QbDdJqJG4DJ9G7+1eC3l0G6Lh4hZ8wXxF1L7w/dRJ9o6kF0r
FqL5xHv5tnZc6yxJ3yEKwL3vDjE3LK3DqUSk7B7QO5jGwt5o9Y6CQQD6JqVqEZ3V
bOlMpwIDAQABAkEAvtJ2Y6cR3VHJ8X7g5HJ4kF0fX5tH3hKZHJpY7v7Z6JqVqEZ3
VbOlMpwK7QbDdJqJG4DJ9G7+1eC3l0G6Lh4hZ8wXxF1L7w/dRJ9o6kF0rFqL5xHv5tnZc6yxJ3yEKwL3vDjE3LK3DqUSk7B7QO5jGwt5o9Y6CQQD6JqVqEZ3V
bOlMpwK7QbDdJqJG4DJ9G7+1eC3l0G6Lh4hZ8wXxF1L7w==
-----END RSA PRIVATE KEY-----
"""


@pytest.fixture(autouse=True)
def reset_cache():
    """Each test starts with no cached token."""
    import gh_app

    gh_app._cached_token = None
    gh_app._cached_token_minted_at = 0.0
    yield


def _set_env(monkeypatch, private_key=None):
    monkeypatch.setenv("AIBUILDER_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("AIBUILDER_GITHUB_APP_INSTALLATION_ID", "67890")
    monkeypatch.setenv(
        "AIBUILDER_GITHUB_APP_PRIVATE_KEY",
        private_key or _TEST_PRIVATE_KEY,
    )


def test_missing_env_raises(monkeypatch):
    from gh_app import GhAppNotConfigured, get_installation_token

    monkeypatch.delenv("AIBUILDER_GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("AIBUILDER_GITHUB_APP_INSTALLATION_ID", raising=False)
    monkeypatch.delenv("AIBUILDER_GITHUB_APP_PRIVATE_KEY", raising=False)
    with pytest.raises(GhAppNotConfigured):
        get_installation_token()


def test_fetches_and_returns_token(monkeypatch):
    """First call mints a JWT, POSTs to /app/installations/{id}/access_tokens, returns the token."""
    from gh_app import get_installation_token

    _set_env(monkeypatch)
    fake_response = MagicMock()
    fake_response.status_code = 201
    fake_response.json.return_value = {"token": "ghs_test_xyz", "expires_at": "..."}

    with (
        patch("gh_app._sign_jwt", return_value="jwt.signed.value"),
        patch("gh_app.requests.post", return_value=fake_response) as post,
    ):
        tok = get_installation_token()

    assert tok == "ghs_test_xyz"
    assert post.call_count == 1
    url = post.call_args[0][0]
    assert url == "https://api.github.com/app/installations/67890/access_tokens"
    headers = post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer jwt.signed.value"
    assert headers["Accept"] == "application/vnd.github+json"


def test_caches_token_across_calls(monkeypatch):
    """Second call within TTL reuses the cached token without hitting GitHub."""
    from gh_app import get_installation_token

    _set_env(monkeypatch)
    fake_response = MagicMock()
    fake_response.status_code = 201
    fake_response.json.return_value = {"token": "ghs_cached", "expires_at": "..."}

    with (
        patch("gh_app._sign_jwt", return_value="jwt"),
        patch("gh_app.requests.post", return_value=fake_response) as post,
    ):
        a = get_installation_token()
        b = get_installation_token()

    assert a == b == "ghs_cached"
    assert post.call_count == 1  # cached on second call


def test_refetches_after_ttl_elapses(monkeypatch):
    """After ~55 minutes the cached token is considered stale and we refetch."""
    import gh_app
    from gh_app import get_installation_token

    _set_env(monkeypatch)
    fake_response = MagicMock()
    fake_response.status_code = 201
    fake_response.json.return_value = {"token": "ghs_first", "expires_at": "..."}

    with (
        patch("gh_app._sign_jwt", return_value="jwt"),
        patch("gh_app.requests.post", return_value=fake_response) as post,
    ):
        get_installation_token()
        # Manually expire the cache (simulate 56 minutes passed)
        gh_app._cached_token_minted_at = time.time() - 56 * 60
        fake_response.json.return_value = {"token": "ghs_second", "expires_at": "..."}
        tok2 = get_installation_token()

    assert tok2 == "ghs_second"
    assert post.call_count == 2


def test_github_error_raises(monkeypatch):
    """Non-201 response raises GhAppAuthFailed with the response text."""
    from gh_app import GhAppAuthFailed, get_installation_token

    _set_env(monkeypatch)
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = "Bad credentials"

    with (
        patch("gh_app._sign_jwt", return_value="jwt"),
        patch("gh_app.requests.post", return_value=fake_response),
    ):
        with pytest.raises(GhAppAuthFailed, match="401"):
            get_installation_token()
