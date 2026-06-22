# aibuilder/tests/test_gh_clone.py
from unittest.mock import patch

from gh_clone import _scrub_token, clone


def test_scrub_strips_token_from_url():
    s = "fatal: clone of https://x-access-token:ghp_secret@github.com/foo/bar failed"
    assert "ghp_secret" not in _scrub_token(s, "ghp_secret")
    assert "<token>" in _scrub_token(s, "ghp_secret")


def test_scrub_handles_no_token_set():
    assert _scrub_token("plain text", None) == "plain text"


def test_clone_public_succeeds_first_try(tmp_path):
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

    with (
        patch("gh_clone._get_token_or_none", return_value=None),
        patch("gh_clone.subprocess.run", side_effect=fake_run),
    ):
        path, err = clone("https://github.com/public/repo", tmp_path)
    assert err is None
    assert path is not None
    assert len(calls) == 1


def test_clone_private_retries_with_token(tmp_path):
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

    with (
        patch("gh_clone._get_token_or_none", return_value="ghp_xyz"),
        patch("gh_clone.subprocess.run", side_effect=fake_run),
    ):
        _path, err = clone("https://github.com/govtech-bb/private", tmp_path)
    assert err is None
    assert len(calls) == 2
    # First call uses bare URL; second injects token
    assert "x-access-token" in calls[1][-2]


def test_clone_rejects_dash_prefix(tmp_path):
    _, err = clone("--upload-pack=evil", tmp_path)
    assert err is not None
    assert "invalid" in err["summary"].lower() or "cannot" in err["details"].lower()


def test_clone_failure_scrubs_token_in_error(tmp_path):
    def fake_run(cmd, **kw):
        from subprocess import CompletedProcess

        return CompletedProcess(cmd, 128, "", "fatal: ghp_xyz invalid")

    with (
        patch("gh_clone._get_token_or_none", return_value="ghp_xyz"),
        patch("gh_clone.subprocess.run", side_effect=fake_run),
    ):
        _, err = clone("https://github.com/foo/bar", tmp_path)
    assert err is not None
    assert "ghp_xyz" not in err["details"]


def test_clone_falls_back_to_no_token_when_app_unconfigured(tmp_path):
    """When the App isn't configured (no env vars), clone behaves like public-only."""
    calls = []

    def fake_run(cmd, **kw):
        from subprocess import CompletedProcess

        calls.append(cmd)
        import os

        target = cmd[-1]
        os.makedirs(target, exist_ok=True)
        return CompletedProcess(cmd, 0, "", "")

    with (
        patch("gh_clone._get_token_or_none", return_value=None),
        patch("gh_clone.subprocess.run", side_effect=fake_run),
    ):
        path, err = clone("https://github.com/public/repo", tmp_path)

    assert err is None
    assert path is not None
    # No retry happened — only one git clone call
    assert len(calls) == 1
    # And the URL doesn't contain x-access-token (since no token was injected)
    assert "x-access-token" not in calls[0][-2]
