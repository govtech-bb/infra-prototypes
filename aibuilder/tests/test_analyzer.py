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


def test_summary_includes_app_type_and_frameworks():
    profile = analyze_repo(str(FIXTURES / "python_api"))
    assert "python backend api" in profile.summary.lower()
    assert "fastapi" in profile.summary.lower()
    assert "python" in profile.summary.lower()


def test_summary_for_unknown_repo(tmp_path: Path):
    # Repo with one stray text file → no manifest, no html
    (tmp_path / "notes.txt").write_text("just a note")
    profile = analyze_repo(str(tmp_path))
    assert profile.app_type == "unknown"
    assert "couldn't tell" in profile.summary.lower()


def test_next_ssr_classified_as_spa_with_api():
    """Next.js is server-rendered by default — needs a Node runtime, not S3+CF.
    Regression: surfaced during real testing against govtech-bb/st-thomas-sign-in.
    """
    profile = analyze_repo(str(FIXTURES / "next_ssr"))
    assert profile.app_type == "spa_with_api"
    assert "next" in profile.frameworks
    assert "react" in profile.frameworks
    assert "server" in profile.summary.lower()


def test_next_static_export_classified_as_static_site():
    """Next.js WITH `output: "export"` in next.config genuinely IS static —
    should land on S3+CloudFront, not App Runner."""
    profile = analyze_repo(str(FIXTURES / "next_static_export"))
    assert profile.app_type == "static_site"
    assert "next" in profile.frameworks
