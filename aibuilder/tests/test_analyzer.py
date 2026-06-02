"""Tests for the repo analyzer."""

from pathlib import Path

from analyzer import RepoProfile, analyze_repo

FIXTURES = Path(__file__).parent / "fixtures"


def test_unknown_for_nonexistent_path(tmp_path: Path):
    missing = tmp_path / "nope"
    profile = analyze_repo(str(missing))
    assert isinstance(profile, RepoProfile)
    assert profile.app_type == "unknown"
    assert "not found" in profile.summary.lower()


def test_unknown_for_empty_dir(tmp_path: Path):
    profile = analyze_repo(str(tmp_path))
    assert profile.app_type == "unknown"


def test_static_site_detected():
    profile = analyze_repo(str(FIXTURES / "static_site"))
    assert profile.app_type == "static_site"
    assert "index.html" in profile.entry_points
    assert "html" in profile.languages
    assert profile.has_dockerfile is False
    assert profile.has_database_hints is False


def test_node_api_detected():
    profile = analyze_repo(str(FIXTURES / "node_api"))
    assert profile.app_type == "node_api"
    assert "express" in profile.frameworks
    assert "javascript" in profile.languages
    assert profile.has_database_hints is False


def test_python_api_detected():
    profile = analyze_repo(str(FIXTURES / "python_api"))
    assert profile.app_type == "python_api"
    assert "fastapi" in profile.frameworks
    assert "python" in profile.languages


def test_dockerized_web_detected():
    profile = analyze_repo(str(FIXTURES / "dockerized_web"))
    assert profile.app_type == "dockerized_web"
    assert profile.has_dockerfile is True
    assert "go" in profile.languages


def test_spa_with_api_detected():
    profile = analyze_repo(str(FIXTURES / "spa_with_api"))
    assert profile.app_type == "spa_with_api"
    assert "react" in profile.frameworks
    assert "express" in profile.frameworks
    assert profile.build_command == "npm run build"
    assert profile.has_database_hints is False


def test_fullstack_with_db_detected():
    profile = analyze_repo(str(FIXTURES / "fullstack_with_db"))
    assert profile.app_type == "fullstack_with_db"
    assert profile.has_database_hints is True


def test_worker_detected():
    profile = analyze_repo(str(FIXTURES / "worker"))
    assert profile.app_type == "worker"
    assert "celery" in profile.frameworks
