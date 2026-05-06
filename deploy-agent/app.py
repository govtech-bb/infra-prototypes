"""INFRA Deploy Agent — FastAPI routes."""

from __future__ import annotations

import os
from pathlib import Path

import anthropic
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import run_agent_loop
from sessions import Session, SqliteSessionStore

app = FastAPI(title="INFRA Deploy Agent")
client = anthropic.Anthropic()
_DB_PATH = Path(os.environ.get("DEPLOY_AGENT_DB", Path(__file__).parent / "data" / "sessions.db"))
store = SqliteSessionStore(_DB_PATH)


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    message: str
    deployment: dict | None = None


def _get_or_404(session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown session_id: {session_id}")
    return session


@app.get("/api/session")
def new_session() -> dict:
    session = store.create()
    return {"session_id": session.session_id}


@app.post("/api/upload/{session_id}")
async def upload_files_endpoint(
    session_id: str,
    files: list[UploadFile] = File(default=...),  # noqa: B008
) -> dict:
    session = _get_or_404(session_id)

    upload_dir = f"/tmp/deploy-sessions/{session_id}"
    Path(upload_dir).mkdir(parents=True, exist_ok=True)
    session.upload_dir = upload_dir

    saved = []
    for f in files:
        # Normalize Windows-style separators so Path-based traversal guards work cross-platform.
        filename = (f.filename or "").replace("\\", "/")
        parts = Path(filename).parts
        if not parts or any(p in ("..", "") or p.startswith("/") for p in parts):
            raise HTTPException(400, f"Invalid filename: {filename!r}")
        safe_path = Path(upload_dir) / filename
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        content = await f.read()
        safe_path.write_bytes(content)
        saved.append(filename)

    session.files = session.files + saved
    store.save(session)
    return {"uploaded": saved, "total_files": len(session.files)}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    session = _get_or_404(req.session_id)

    user_content = req.message
    last_announced = session.last_injected_file_count
    current_count = len(session.files)
    if current_count > last_announced:
        new_files = session.files[last_announced:]
        file_list = ", ".join(new_files)
        if last_announced == 0:
            user_content = f"{req.message}\n\n[Uploaded files: {file_list}]"
        else:
            user_content = f"{req.message}\n\n[Newly uploaded: {file_list}]"
        session.last_injected_file_count = current_count

    session.messages.append({"role": "user", "content": user_content})
    reply = run_agent_loop(client, session)
    store.save(session)

    return ChatResponse(message=reply, deployment=session.deployment)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


app.mount(
    "/",
    StaticFiles(directory=str(Path(__file__).parent / "static"), html=True),
    name="static",
)
