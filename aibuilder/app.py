"""aibuilder FastAPI routes."""

from __future__ import annotations

import os
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import run_agent_loop
from sessions import Session, SqliteSessionStore

app = FastAPI(title="aibuilder")
client = anthropic.AnthropicBedrock(
    aws_region=os.environ.get("AWS_REGION", "us-east-1"),
)
_DB_PATH = Path(os.environ.get("AIBUILDER_DB", Path(__file__).parent / "data" / "sessions.db"))
store = SqliteSessionStore(_DB_PATH)

_AUTH_TOKEN = os.environ.get("AIBUILDER_TOKEN")
# Paths that are always open (no auth required), checked as prefixes
_OPEN_PREFIXES = ("/api/health", "/static/")
# Paths that are always open, checked as exact matches
_OPEN_EXACT = ("/", "/govtech-barbados.png", "/favicon.ico")


@app.middleware("http")
async def require_bearer_token(request: Request, call_next):
    """Reject /api/* requests that don't carry a matching bearer token.

    Local dev: leave AIBUILDER_TOKEN unset and the middleware passes
    everything through. Production sets the env var via ECS task secrets,
    making the API surface unreachable without the right header.
    """
    path = request.url.path
    if _AUTH_TOKEN is None:
        return await call_next(request)
    if path in _OPEN_EXACT or any(path.startswith(p) for p in _OPEN_PREFIXES):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header != f"Bearer {_AUTH_TOKEN}":
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid Authorization header"},
        )
    return await call_next(request)


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
