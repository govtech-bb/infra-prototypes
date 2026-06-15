# aibuilder/tests/test_limits.py
import pytest

from deployments import SqliteDeploymentStore
from limits import check_caps


@pytest.fixture
def store(tmp_path):
    return SqliteDeploymentStore(tmp_path / "deploys.db")


def test_passes_when_under_caps(monkeypatch, store):
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", "5")
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY", "10")
    assert check_caps(store, session_id="s1") is None


def test_blocks_when_session_cap_reached(monkeypatch, store):
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", "2")
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY", "100")
    for i in range(2):
        store.create("s1", "u", "static_site", f"p{i}", "e", ttl_days=14)
    err = check_caps(store, session_id="s1")
    assert err is not None
    assert "session" in err["summary"].lower()
    assert "details" in err


def test_blocks_when_global_cap_reached(monkeypatch, store):
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", "100")
    monkeypatch.setenv("AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY", "2")
    for i in range(2):
        store.create(f"s{i}", "u", "static_site", f"p{i}", "e", ttl_days=14)
    err = check_caps(store, session_id="other")
    assert err is not None
    assert "daily" in err["summary"].lower() or "global" in err["summary"].lower()


def test_uses_default_caps_when_env_unset(monkeypatch, store):
    monkeypatch.delenv("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", raising=False)
    monkeypatch.delenv("AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY", raising=False)
    assert check_caps(store, session_id="s1") is None
