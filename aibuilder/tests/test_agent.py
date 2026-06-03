"""Tests for the agent loop."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent import SYSTEM_PROMPT, run_agent_loop
from sessions import Session


def test_system_prompt_mentions_four_stage_workflow():
    assert "clone_repo" in SYSTEM_PROMPT
    assert "analyze_repo" in SYSTEM_PROMPT
    assert "recommend_architecture" in SYSTEM_PROMPT
    assert "estimate_cost" in SYSTEM_PROMPT
    # The validation step is load-bearing for trust — must be in the prompt.
    assert "confirm" in SYSTEM_PROMPT.lower() or "verbatim" in SYSTEM_PROMPT.lower()


def _stub_response(text: str, *, stop_reason: str = "end_turn", tool_calls: list | None = None):
    blocks = []
    if text:
        blocks.append(SimpleNamespace(type="text", text=text, model_dump=lambda: {"type": "text", "text": text}))
    for call in tool_calls or []:
        blocks.append(SimpleNamespace(type="tool_use", **call, model_dump=lambda c=call: {"type": "tool_use", **c}))
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


def test_agent_returns_text_on_end_turn():
    client = MagicMock()
    client.messages.create.return_value = _stub_response("Hi there.")
    session = Session(session_id="s1")
    reply = run_agent_loop(client, session)
    assert reply == "Hi there."
    assert session.messages[-1]["role"] == "assistant"


def test_agent_stops_at_iteration_limit():
    client = MagicMock()
    # Always return tool_use → never end_turn → loop hits the cap
    client.messages.create.return_value = _stub_response(
        "",
        stop_reason="tool_use",
        tool_calls=[{"id": "x", "name": "analyze_repo", "input": {"path": "/nope"}}],
    )
    session = Session(session_id="s1")
    reply = run_agent_loop(client, session)
    assert "iteration limit" in reply.lower()
