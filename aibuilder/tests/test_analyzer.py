"""Tests for the repo analyzer."""

from pathlib import Path

from analyzer import RepoProfile, analyze_repo


def test_unknown_for_nonexistent_path(tmp_path: Path):
    missing = tmp_path / "nope"
    profile = analyze_repo(str(missing))
    assert isinstance(profile, RepoProfile)
    assert profile.app_type == "unknown"
    assert "not found" in profile.summary.lower()


def test_unknown_for_empty_dir(tmp_path: Path):
    profile = analyze_repo(str(tmp_path))
    assert profile.app_type == "unknown"
