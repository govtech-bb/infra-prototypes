"""aibuilder FastAPI routes."""

from __future__ import annotations

import os
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import run_agent_loop
from sessions import Session, SqliteSessionStore

app = FastAPI(title="aibuilder")
client = anthropic.Anthropic()
_DB_PATH = Path(os.environ.get("AIBUILDER_DB", Path(__file__).parent / "data" / "sessions.db"))
store = SqliteSessionStore(_DB_PATH)


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    message: str
    last_profile: dict | None = None


def _get_or_404(session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown session_id: {session_id}")
    return session


@app.get("/api/session")
def new_session() -> dict:
    session = store.create()
    return {"session_id": session.session_id}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    session = _get_or_404(req.session_id)
    session.messages.append({"role": "user", "content": req.message})
    reply = run_agent_loop(client, session)
    store.save(session)
    return ChatResponse(message=reply, last_profile=session.last_profile)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
