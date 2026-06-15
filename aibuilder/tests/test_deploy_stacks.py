import pytest

from deploy_stacks import StackSpec, get_spec, list_supported_patterns, register


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    """Each test gets an empty registry."""
    from deploy_stacks import _registry

    monkeypatch.setattr(_registry, "STACK_REGISTRY", {})


def test_get_spec_returns_registered():
    spec = StackSpec(
        pattern="static_site",
        stack_dir="x",
        build_vars=lambda d: {},
        allowed_knobs=["is_spa"],
    )
    register(spec)
    assert get_spec("static_site") is spec


def test_get_spec_returns_none_for_unknown():
    assert get_spec("nope") is None


def test_list_supported_patterns_is_generated():
    register(StackSpec("a", "x", lambda d: {}, []))
    register(StackSpec("b", "x", lambda d: {}, []))
    assert sorted(list_supported_patterns()) == ["a", "b"]


def test_not_deployable_message_lists_supported():
    register(StackSpec("static_site", "x", lambda d: {}, []))
    from deploy_stacks import not_deployable_message

    msg = not_deployable_message("worker")
    assert "worker" in msg
    assert "static_site" in msg
