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


def test_clone_rejects_dash_prefix(tmp_path):
    _, err = clone("--upload-pack=evil", tmp_path)
    assert err is not None
    assert "invalid" in err["summary"].lower() or "cannot" in err["details"].lower()


def test_clone_failure_scrubs_token_in_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_GITHUB_TOKEN", "ghp_xyz")

    def fake_run(cmd, **kw):
        from subprocess import CompletedProcess

        return CompletedProcess(cmd, 128, "", "fatal: ghp_xyz invalid")

    with patch("gh_clone.subprocess.run", side_effect=fake_run):
        _, err = clone("https://github.com/foo/bar", tmp_path)
    assert err is not None
    assert "ghp_xyz" not in err["details"]
