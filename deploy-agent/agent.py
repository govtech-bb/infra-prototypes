"""Claude tool-use loop and system prompt.

Extracted from app.py without behavior changes (other than serializing
ContentBlock objects to dicts for forward compatibility with the SQLite store).
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from sessions import Session
from tools import TOOL_DEFINITIONS, execute_tool

MAX_AGENT_ITERATIONS = 15

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
- If a tool result contains a `summary` field, that means the call failed. Tell the user the summary in plain language and offer to share the `details` if they ask. Suggest what to check based on the summary.
- If files are uploaded but no `index.html` is present, ask the user which file should be the homepage instead of guessing — only auto-select when there is exactly one HTML file.
- Never ask for AWS credentials — assume they're configured in the environment.
"""


def _serialize_content(blocks: list[Any]) -> list[dict]:
    """Convert Anthropic ContentBlock objects to JSON-serializable dicts."""
    return [b.model_dump() for b in blocks]


def run_agent_loop(client: anthropic.Anthropic, session: Session) -> str:
    """Run the Claude agentic loop until a final text response is produced."""
    for _ in range(MAX_AGENT_ITERATIONS):
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=session.messages,
        )

        session.messages.append(
            {"role": "assistant", "content": _serialize_content(response.content)}
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                result = execute_tool(
                    block.name,
                    block.input,
                    session_id=session.session_id,
                    session=session,
                )

                # Guards both the current `{"error": ...}` shape (Task 2-4) and the
                # `{"summary": ...}` shape introduced in Task 5.
                deploy_succeeded = (
                    block.name == "deploy_infrastructure"
                    and "summary" not in result
                    and "error" not in result
                )
                if deploy_succeeded:
                    session.deployment = result

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    }
                )

            session.messages.append({"role": "user", "content": tool_results})

    return "The deployment agent reached its iteration limit. Please try again."
