"""Tests for tool implementations."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from tools import (
    analyze_repo,
    clone_repo,
    estimate_cost,
    recommend_architecture,
)


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


def test_clone_rejects_path_traversal_repo_names(tmp_path: Path, monkeypatch):
    """Regression: the URL regex's [\\w.-]+ allows '.', '..', '...' etc.,
    which without explicit rejection would let `target.parent / '..'` escape
    the session dir and let a subsequent shutil.rmtree wipe the tmp root.
    """
    monkeypatch.setenv("AIBUILDER_TMP_DIR", str(tmp_path))

    # Pre-create a sentinel directory under tmp_path that MUST survive.
    sentinel = tmp_path / "must-not-be-deleted"
    sentinel.mkdir()

    def must_not_run(cmd, **kwargs):
        raise AssertionError(f"subprocess.run was called with {cmd!r} — clone should have aborted")

    for bad in (
        "https://github.com/foo/..",
        "https://github.com/foo/.",
        "https://github.com/../foo",
        "https://github.com/.../bar",
    ):
        with patch("subprocess.run", side_effect=must_not_run):
            result = clone_repo(bad, session_id="s-traverse")
        assert "summary" in result, f"expected error dict for {bad!r}, got {result!r}"
        assert sentinel.exists(), f"sentinel was deleted while handling {bad!r}"


def test_clone_handles_git_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AIBUILDER_TMP_DIR", str(tmp_path))

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 128, stdout="", stderr="fatal: repository 'https://github.com/no/exist' not found"
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = clone_repo("https://github.com/no/exist", session_id="s3")
    assert "summary" in result
    # gh_clone returns "Could not clone the repository. Is the URL correct
    # and accessible?" when both attempts fail. Word "clone" is the stable
    # substring across summary wording changes.
    assert "clone" in result["summary"].lower() or "could not" in result["summary"].lower()


# Round-trip tests: analyze → recommend → estimate
FIXTURES = Path(__file__).parent / "fixtures"


def test_full_chain_static_site():
    profile = analyze_repo(str(FIXTURES / "static_site"))
    assert profile["app_type"] == "static_site"
    arch = recommend_architecture(profile)
    assert arch["pattern"] == "static_site"
    assert [s["aws_service"] for s in arch["services"]] == ["S3", "CloudFront"]
    cost = estimate_cost(arch)
    assert cost["total_monthly_usd"] > 0
    assert cost["is_fallback"] is True


def test_full_chain_fullstack_db():
    profile = analyze_repo(str(FIXTURES / "fullstack_with_db"))
    arch = recommend_architecture(profile)
    assert arch["pattern"] == "fullstack_with_db"
    cost = estimate_cost(arch)
    services = [line["service"] for line in cost["lines"]]
    assert "RDS PostgreSQL" in services


def test_recommend_handles_dict_profile():
    """The agent will pass profiles as JSON dicts, not dataclasses."""
    profile = {
        "app_type": "node_api",
        "languages": ["javascript"],
        "frameworks": ["express"],
        "has_dockerfile": False,
        "has_compose": False,
        "has_database_hints": False,
        "entry_points": ["server.js"],
        "build_command": None,
        "summary": "",
    }
    arch = recommend_architecture(profile)
    assert arch["pattern"] == "node_api"


def test_recommend_swallows_unknown_keys_from_llm():
    """Regression: live testing crashed when the LLM invented a `force_pattern`
    key. RepoProfile(**profile) raised TypeError and killed the whole chat.
    The wrapper now filters unknowns to the dataclass's known fields."""
    profile = {
        "app_type": "node_api",
        "languages": ["javascript"],
        "frameworks": ["express"],
        "has_dockerfile": False,
        "has_compose": False,
        "has_database_hints": False,
        "entry_points": ["server.js"],
        "build_command": None,
        "summary": "",
        "force_pattern": "fullstack_with_db",  # the offending hallucination
        "made_up_key": "anything",
    }
    arch = recommend_architecture(profile)
    # Bogus keys are dropped, routing runs normally → node_api.
    assert arch["pattern"] == "node_api"


def test_estimate_cost_swallows_unknown_service_keys():
    """Same robustness: if the LLM passes an architecture dict with extra
    keys on services, estimate_cost should not raise."""
    architecture = {
        "pattern": "static_site",
        "services": [
            {
                "aws_service": "S3",
                "purpose": "Stores assets",
                "sizing": {"storage_gb": 1},
                "extra_invented_field": "ignore me",
            },
        ],
        "notes": [],
    }
    cost = estimate_cost(architecture)
    assert cost["lines"][0]["service"] == "S3"


def test_recommend_pattern_override_returns_named_pattern():
    """Audit follow-up: give the LLM a first-class way to ask for a specific
    pattern (the underlying need behind the `force_pattern` hallucination).
    Used for 'show me the alternative' / 'estimate both options' chat flows."""
    profile = {
        "app_type": "spa_with_api",
        "frameworks": ["next", "react"],
        "has_dockerfile": False,
        "has_database_hints": True,
        "pattern_override": "fullstack_with_db",
    }
    # Without the override, this profile would route to nextjs_amplify_hosting
    # (Next.js + no Dockerfile + db hints). The override forces the alternative.
    arch = recommend_architecture(profile)
    assert arch["pattern"] == "fullstack_with_db"
    services = [s["aws_service"] for s in arch["services"]]
    assert "ECS Fargate" in services and "RDS PostgreSQL" in services


def test_recommend_ignores_unknown_pattern_override():
    """If the LLM passes a bogus override key, fall back to routing."""
    profile = {
        "app_type": "node_api",
        "frameworks": ["express"],
        "has_dockerfile": False,
        "pattern_override": "not_a_real_pattern",
    }
    arch = recommend_architecture(profile)
    # Falls through to normal routing → node_api.
    assert arch["pattern"] == "node_api"


def test_tool_definitions_shape():
    from tools import TOOL_DEFINITIONS

    names = [t["name"] for t in TOOL_DEFINITIONS]
    assert names == [
        "clone_repo",
        "analyze_repo",
        "recommend_architecture",
        "estimate_cost",
        "deploy_repo",
        "get_deployment_status",
        "list_deployments",
        "redeploy",
        "modify_deployment",
        "destroy_deployment",
        "extend_deployment",
    ]
    for t in TOOL_DEFINITIONS:
        assert "description" in t
        assert "input_schema" in t


def test_recommend_tool_description_lists_every_catalog_pattern():
    """Regression: a user asked the bot to use `workflow_worker` and the bot
    refused because its tool description's hardcoded pattern list was stale —
    the pattern existed in `_CATALOG` (and unit tests passed) but the agent
    couldn't see it. Tool descriptions are now generated from `_CATALOG.keys()`
    at import time. This test locks that contract: every catalog pattern must
    be mentioned in the recommend_architecture tool description, with no
    exceptions, so adding a new pattern automatically makes it discoverable."""
    from patterns import _CATALOG
    from tools import TOOL_DEFINITIONS

    recommend_def = next(t for t in TOOL_DEFINITIONS if t["name"] == "recommend_architecture")
    description = recommend_def["description"]
    for pattern_key in _CATALOG:
        assert f"'{pattern_key}'" in description, (
            f"pattern '{pattern_key}' is in _CATALOG but not advertised in the "
            f"recommend_architecture tool description — the agent won't know it "
            f"can use it as a pattern_override"
        )


def test_execute_tool_dispatches():
    from tools import execute_tool

    result = execute_tool(
        "analyze_repo",
        {"path": str(FIXTURES / "static_site")},
        session_id="s1",
        session=None,
    )
    assert result["app_type"] == "static_site"


def test_execute_unknown_tool_returns_error():
    from tools import execute_tool

    result = execute_tool("does_not_exist", {}, session_id="s1", session=None)
    assert "summary" in result
    assert "unknown tool" in result["summary"].lower()
