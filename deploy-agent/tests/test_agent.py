"""Tests for agent.run_agent_loop."""

from unittest.mock import MagicMock

import pytest  # noqa: F401

import agent
from sessions import Session


def _block(type_, **kwargs):
    """Build a fake Anthropic ContentBlock that supports model_dump and attribute access."""
    m = MagicMock()
    m.type = type_
    m.model_dump.return_value = {"type": type_, **kwargs}
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _response(content, stop_reason):
    r = MagicMock()
    r.content = content
    r.stop_reason = stop_reason
    return r


def test_happy_path_returns_text(monkeypatch):
    client = MagicMock()
    client.messages.create.return_value = _response(
        [_block("text", text="all done")], "end_turn"
    )
    session = Session(session_id="s1")
    session.messages.append({"role": "user", "content": "hi"})

    result = agent.run_agent_loop(client, session)
    assert result == "all done"
    # Assistant turn was appended with serialized content (dicts, not MagicMocks).
    last = session.messages[-1]
    assert last["role"] == "assistant"
    assert last["content"] == [{"type": "text", "text": "all done"}]


def test_iteration_cap_hits_safety_limit(monkeypatch):
    monkeypatch.setattr(agent, "MAX_AGENT_ITERATIONS", 2)

    client = MagicMock()
    # Always returns a tool_use block for an unknown tool — never end_turn.
    client.messages.create.return_value = _response(
        [_block("tool_use", id="t1", name="nonexistent", input={})], "tool_use"
    )

    # Patch execute_tool to return a benign no-op so the loop continues.
    monkeypatch.setattr(agent, "execute_tool",
                        lambda name, inputs, session_id, session: {"summary": "noop", "details": ""})

    session = Session(session_id="s1")
    result = agent.run_agent_loop(client, session)
    assert "iteration limit" in result
    assert client.messages.create.call_count == 2  # respected MAX_AGENT_ITERATIONS


def test_tool_use_round_trip_caches_deployment(monkeypatch):
    client = MagicMock()
    deploy_block = _block("tool_use", id="t1", name="deploy_infrastructure", input={})
    client.messages.create.side_effect = [
        _response([deploy_block], "tool_use"),
        _response([_block("text", text="✅ Live!")], "end_turn"),
    ]
    monkeypatch.setattr(
        agent, "execute_tool",
        lambda name, inputs, session_id, session: {
            "bucket_name": "b", "site_url": "https://x", "cloudfront_distribution_id": "C",
            "project_name": "p", "env": "proto",
        },
    )

    session = Session(session_id="s1")
    result = agent.run_agent_loop(client, session)
    assert result == "✅ Live!"
    assert session.deployment is not None
    assert session.deployment["bucket_name"] == "b"


def test_tool_failure_does_not_cache_deployment(monkeypatch):
    client = MagicMock()
    deploy_block = _block("tool_use", id="t1", name="deploy_infrastructure", input={})
    client.messages.create.side_effect = [
        _response([deploy_block], "tool_use"),
        _response([_block("text", text="that failed")], "end_turn"),
    ]
    monkeypatch.setattr(
        agent, "execute_tool",
        lambda name, inputs, session_id, session: {"summary": "boom", "details": "..."},
    )

    session = Session(session_id="s1")
    agent.run_agent_loop(client, session)
    assert session.deployment is None
