"""
Tool implementations for the INFRA Deploy Agent.
Each function maps to a tool Claude can call during a deployment conversation.
"""

import os
import json
import subprocess
import mimetypes
import boto3
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
# Resolve infra directory relative to this file: deploy-agent/ → infra/
_HERE = Path(__file__).parent
INFRA_DIR = os.environ.get("INFRA_DIR", str(_HERE.parent / "infra"))
STACK_DIR = os.path.join(INFRA_DIR, "stacks", "static-website")

# ── Tool Definitions (passed to Claude) ───────────────────────────────────────
TOOL_DEFINITIONS = [
    {
        "name": "deploy_infrastructure",
        "description": (
            "Creates the S3 bucket and CloudFront distribution for a static website "
            "using OpenTofu. Call this once you have confirmed the site title, "
            "owner name, and owner email with the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "URL-safe slug (lowercase, hyphens only, max 20 chars). Derived from the site title."
                },
                "env": {
                    "type": "string",
                    "description": "Deployment environment label, e.g. 'proto', 'dev', 'staging'."
                },
                "site_title": {
                    "type": "string",
                    "description": "Human-readable website title, used as an AWS resource tag."
                },
                "owner_name": {
                    "type": "string",
                    "description": "Full name of the site owner."
                },
                "owner_email": {
                    "type": "string",
                    "description": "Email address of the site owner."
                },
                "is_spa": {
                    "type": "boolean",
                    "description": "True if this is a single-page app (React, Vue, etc.) that needs 404→index.html routing."
                }
            },
            "required": ["project_name", "env", "site_title", "owner_name", "owner_email"]
        }
    },
    {
        "name": "upload_files",
        "description": (
            "Uploads the user's files to the S3 bucket. "
            "Call this after deploy_infrastructure has succeeded and returned a bucket_name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bucket_name": {
                    "type": "string",
                    "description": "S3 bucket name returned by deploy_infrastructure."
                },
                "distribution_id": {
                    "type": "string",
                    "description": "CloudFront distribution ID returned by deploy_infrastructure."
                }
            },
            "required": ["bucket_name", "distribution_id"]
        }
    }
]


# ── Tool Implementations ───────────────────────────────────────────────────────

def deploy_infrastructure(
    project_name: str,
    env: str,
    site_title: str,
    owner_name: str,
    owner_email: str,
    is_spa: bool = False,
    **_
) -> dict:
    """Run tofu init + apply for the static-website stack."""
    try:
        # 1. Init
        r = subprocess.run(
            ["tofu", "init", "-input=false"],
            cwd=STACK_DIR, capture_output=True, text=True, timeout=120
        )
        if r.returncode != 0:
            return {"error": f"tofu init failed:\n{r.stderr[-2000:]}"}

        # 2. Create or select workspace (isolates state per deployment)
        workspace = f"{project_name}-{env}"
        subprocess.run(
            ["tofu", "workspace", "new", workspace],
            cwd=STACK_DIR, capture_output=True
        )
        subprocess.run(
            ["tofu", "workspace", "select", workspace],
            cwd=STACK_DIR, capture_output=True
        )

        # 3. Apply
        r = subprocess.run(
            [
                "tofu", "apply", "-auto-approve", "-input=false",
                f"-var=project_name={project_name}",
                f"-var=env={env}",
                f"-var=site_title={site_title}",
                f"-var=owner_name={owner_name}",
                f"-var=owner_email={owner_email}",
                f"-var=is_spa={'true' if is_spa else 'false'}",
            ],
            cwd=STACK_DIR, capture_output=True, text=True, timeout=600
        )
        if r.returncode != 0:
            return {"error": f"tofu apply failed:\n{r.stderr[-3000:]}"}

        # 4. Read outputs
        out = subprocess.run(
            ["tofu", "output", "-json"],
            cwd=STACK_DIR, capture_output=True, text=True
        )
        outputs = json.loads(out.stdout)

        return {
            "bucket_name":              outputs["bucket_name"]["value"],
            "site_url":                 outputs["site_url"]["value"],
            "cloudfront_distribution_id": outputs["cloudfront_distribution_id"]["value"],
        }

    except subprocess.TimeoutExpired:
        return {"error": "Deployment timed out after 10 minutes."}
    except Exception as e:
        return {"error": str(e)}


def upload_files(
    bucket_name: str,
    distribution_id: str,
    session: dict,
    **_
) -> dict:
    """Upload files from the session's temp directory to S3, then invalidate CloudFront."""
    upload_dir = session.get("upload_dir")
    if not upload_dir or not Path(upload_dir).exists():
        return {"error": "No uploaded files found for this session."}

    try:
        s3 = boto3.client("s3")
        cf = boto3.client("cloudfront")
        uploaded = []

        for file_path in sorted(Path(upload_dir).rglob("*")):
            if not file_path.is_file():
                continue

            key = str(file_path.relative_to(upload_dir))
            content_type, _ = mimetypes.guess_type(str(file_path))
            content_type = content_type or "application/octet-stream"

            s3.upload_file(
                str(file_path),
                bucket_name,
                key,
                ExtraArgs={"ContentType": content_type}
            )
            uploaded.append(key)

        # Bust CloudFront cache
        cf.create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": ["/*"]},
                "CallerReference": f"deploy-agent-{distribution_id}"
            }
        )

        return {
            "uploaded_count": len(uploaded),
            "files": uploaded,
            "cache_invalidated": True
        }

    except Exception as e:
        return {"error": str(e)}


def execute_tool(name: str, inputs: dict, session_id: str, session: dict) -> dict:
    """Dispatch a tool call from the Claude agent."""
    if name == "deploy_infrastructure":
        return deploy_infrastructure(**inputs)
    elif name == "upload_files":
        return upload_files(session=session, **inputs)
    return {"error": f"Unknown tool: {name}"}
