"""Tests for tool implementations."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import clone_repo


def test_clone_rejects_non_github_url():
    result = clone_repo("https://gitlab.com/foo/bar", session_id="s1")
    assert "summary" in result
    assert "github" in result["summary"].lower()


def test_clone_rejects_garbage_url():
    result = clone_repo("not a url", session_id="s1")
    assert "summary" in result


def test_clone_accepts_canonical_github_urls(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_TMP_DIR", str(tmp_path))

    def fake_run(cmd, **kwargs):
        # Pretend git clone succeeded by creating the target dir.
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text("<html/>")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = clone_repo("https://github.com/octocat/Hello-World", session_id="s1")
    assert "path" in result
    assert result["repo_name"] == "Hello-World"
    assert result["file_count"] == 1


def test_clone_rejects_repo_too_many_files(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_TMP_DIR", str(tmp_path))
    monkeypatch.setenv("AIBUILDER_MAX_FILES", "3")

    def fake_run(cmd, **kwargs):
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            (target / f"f{i}.txt").write_text("x")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = clone_repo("https://github.com/octocat/big-repo", session_id="s2")
    assert "summary" in result
    assert "too large" in result["summary"].lower() or "subfolder" in result["summary"].lower()


def test_clone_handles_git_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_TMP_DIR", str(tmp_path))

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 128, stdout="", stderr="fatal: repository 'https://github.com/no/exist' not found"
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = clone_repo("https://github.com/no/exist", session_id="s3")
    assert "summary" in result
    assert "public" in result["summary"].lower()
