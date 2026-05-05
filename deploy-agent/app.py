"""
INFRA Deploy Agent — FastAPI backend
Chat with Claude to deploy a static website to AWS S3 + CloudFront.
"""

import os
import json
import uuid
from pathlib import Path
from typing import List

import anthropic
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tools import execute_tool, TOOL_DEFINITIONS

# ── App + client setup ─────────────────────────────────────────────────────────
app = FastAPI(title="INFRA Deploy Agent")
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

# In-memory session store — fine for a small team, swap for Redis if needed
sessions: dict = {}

# ── Agent system prompt ────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are the INFRA Deploy Agent — a friendly assistant that deploys \
static websites to AWS (S3 + CloudFront) on behalf of the user.

Your deployment workflow:
1. Greet the user briefly and ask what they'd like to deploy.
2. If files have been uploaded (you'll see them listed in the message), acknowledge them.
3. Collect the following through natural conversation — only ask for what you don't have:
   - Site title / name  (e.g. "My Portfolio", "Acme Landing Page")
   - Owner's full name
   - Owner's email address
   - Whether it's a single-page app (React, Vue, etc.)
4. Confirm the details in a short summary, then proceed — don't ask for confirmation twice.
5. Call deploy_infrastructure to provision S3 + CloudFront.
6. Call upload_files to push their files live.
7. Return the live URL clearly, e.g.:
   "✅ Your site is live! → https://d1234.cloudfront.net"

Rules:
- Be concise. One question at a time.
- Derive project_name from the site title (lowercase slug, hyphens, max 20 chars).
- Use env="proto" for all prototype deployments unless the user says otherwise.
- If a tool returns an error, explain it simply and suggest what to check.
- Never ask for AWS credentials — assume they're configured in the environment.
"""


# ── Request/response models ────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    message: str
    deployment: dict | None = None


# ── Helpers ────────────────────────────────────────────────────────────────────
def get_or_create_session(session_id: str) -> dict:
    if session_id not in sessions:
        sessions[session_id] = {
            "messages": [],
            "files": [],
            "upload_dir": None,
            "deployment": None,
        }
    return sessions[session_id]


def run_agent_loop(session: dict, session_id: str) -> str:
    """Run the Claude agentic loop until a final text response is produced."""
    for _ in range(15):  # safety cap on tool-call iterations
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=session["messages"],
        )

        # Append assistant turn to history
        session["messages"].append({"role": "assistant", "content": response.content})

        # End turn — return the text
        if response.stop_reason == "end_turn":
            return next(
                (b.text for b in response.content if hasattr(b, "text")), ""
            )

        # Tool use — execute each tool and feed results back
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                result = execute_tool(
                    block.name, block.input,
                    session_id=session_id,
                    session=session
                )

                # Cache successful deployment info on the session
                if block.name == "deploy_infrastructure" and "error" not in result:
                    session["deployment"] = result

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

            session["messages"].append({"role": "user", "content": tool_results})

    return "The deployment agent reached its iteration limit. Please try again."


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/api/session")
def new_session():
    """Create a new chat session and return its ID."""
    session_id = str(uuid.uuid4())
    sessions[session_id] = {
        "messages": [],
        "files": [],
        "upload_dir": None,
        "deployment": None,
    }
    return {"session_id": session_id}


@app.post("/api/upload/{session_id}")
async def upload_files_endpoint(session_id: str, files: List[UploadFile] = File(...)):
    """Receive uploaded website files and store them in a temp directory."""
    session = get_or_create_session(session_id)

    upload_dir = f"/tmp/deploy-sessions/{session_id}"
    Path(upload_dir).mkdir(parents=True, exist_ok=True)
    session["upload_dir"] = upload_dir

    saved = []
    for f in files:
        # Preserve relative paths if the browser sends them (e.g., dist/index.html)
        safe_path = Path(upload_dir) / Path(f.filename).name
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        content = await f.read()
        safe_path.write_bytes(content)
        saved.append(f.filename)

    session["files"] = session["files"] + saved
    return {"uploaded": saved, "total_files": len(session["files"])}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """Send a message to the deploy agent and get a response."""
    session = get_or_create_session(req.session_id)

    # Inject file list into the first user message that arrives after an upload
    user_content = req.message
    files = session.get("files", [])
    already_injected = any(
        "[Uploaded files:" in str(m.get("content", ""))
        for m in session["messages"]
        if isinstance(m, dict)
    )
    if files and not already_injected:
        file_list = ", ".join(files)
        user_content = f"{req.message}\n\n[Uploaded files: {file_list}]"

    session["messages"].append({"role": "user", "content": user_content})

    reply = run_agent_loop(session, req.session_id)
    return ChatResponse(message=reply, deployment=session.get("deployment"))


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ── Serve the chat UI ──────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")
