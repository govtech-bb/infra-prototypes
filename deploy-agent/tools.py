"""
Tool implementations for the INFRA Deploy Agent.
Each function maps to a tool Claude can call during a deployment conversation.
"""

import json
import mimetypes
import os
import re
import subprocess
from pathlib import Path

import boto3

# ── Config ─────────────────────────────────────────────────────────────────────
# Resolve infra directory relative to this file: deploy-agent/ → infra/
_HERE = Path(__file__).parent
INFRA_DIR = os.environ.get("INFRA_DIR", str(_HERE.parent / "infra"))
STACK_DIR = os.path.join(INFRA_DIR, "stacks", "static-website")

# ── Error classification ──────────────────────────────────────────────────────

_ERROR_PATTERNS: list[tuple[str, str]] = [
    (
        r"NoCredentialProviders|Unable to locate credentials",
        "No AWS credentials found. Set AWS_PROFILE or AWS_ACCESS_KEY_ID.",
    ),
    (
        r"BucketAlreadyOwnedByYou|BucketAlreadyExists",
        "A bucket with this name already exists in your account. Pick a different project_name.",
    ),
    (
        r"AccessDenied|UnauthorizedOperation|is not authorized to",
        "AWS credentials lack permission for this operation. Check IAM.",
    ),
    (r"Error: error configuring", "AWS configuration error — check your region and credentials."),
]


def _classify_error(stderr: str) -> dict:
    """Map raw stderr to a {summary, details} dict for the agent to surface."""
    details = stderr[-2000:]
    for pattern, summary in _ERROR_PATTERNS:
        if re.search(pattern, stderr, re.IGNORECASE):
            return {"summary": summary, "details": details}
    return {"summary": "Deployment failed — see details.", "details": details}


# ── Upload preflight ──────────────────────────────────────────────────────────

_SOURCE_EXTENSIONS = re.compile(r"\.(jsx|tsx|ts|vue|svelte|scss|sass|less)$", re.IGNORECASE)


def _preflight_uploads(upload_dir: str | None) -> tuple[dict | None, str | None]:
    """Inspect uploaded files before deploy.

    Returns (error_dict, index_document):
      - error_dict: {"summary", "details"} if deploy should be blocked, else None.
      - index_document: filename to use as the homepage, or None (caller defaults
        to "index.html"). Only set when auto-detected from a single non-index HTML.
    """
    if not upload_dir or not Path(upload_dir).exists():
        return (
            {
                "summary": "No files uploaded yet — drag a folder into the chat first.",
                "details": "",
            },
            None,
        )

    files = [p for p in Path(upload_dir).rglob("*") if p.is_file()]
    if not files:
        return (
            {
                "summary": "No files uploaded yet — drag a folder into the chat first.",
                "details": "",
            },
            None,
        )

    html_files = [p for p in files if p.suffix.lower() in (".html", ".htm")]
    source_files = [p for p in files if _SOURCE_EXTENSIONS.search(p.suffix)]

    if source_files and not html_files:
        sample = ", ".join(p.name for p in source_files[:10])
        return (
            {
                "summary": (
                    "Looks like source code, not a built site. Run 'npm run build' "
                    "(or your project's build command) and upload the output folder "
                    "(usually 'dist/' or 'build/')."
                ),
                "details": f"Found: {sample}",
            },
            None,
        )

    has_index = any(p.name.lower() == "index.html" for p in html_files)
    if has_index:
        return (None, None)

    if len(html_files) == 1:
        # Auto-select the single HTML file as the entry document.
        return (None, html_files[0].name)

    if len(html_files) > 1:
        sample = ", ".join(p.name for p in html_files[:10])
        return (
            {
                "summary": (
                    "Multiple HTML files but no index.html. Tell me which one is the "
                    "homepage (e.g., 'use home.html as the entry')."
                ),
                "details": f"Found: {sample}",
            },
            None,
        )

    # No HTML at all and no source files — let it through; tofu will create
    # the bucket and the user will see the empty-bucket error if any.
    return (None, None)


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
                    "description": "URL-safe slug (lowercase, hyphens only, max 20 chars). Derived from the site title.",
                },
                "env": {
                    "type": "string",
                    "description": "Deployment environment label, e.g. 'proto', 'dev', 'staging'.",
                },
                "site_title": {
                    "type": "string",
                    "description": "Human-readable website title, used as an AWS resource tag.",
                },
                "owner_name": {"type": "string", "description": "Full name of the site owner."},
                "owner_email": {
                    "type": "string",
                    "description": "Email address of the site owner.",
                },
                "is_spa": {
                    "type": "boolean",
                    "description": "True if this is a single-page app (React, Vue, etc.) that needs 404→index.html routing.",
                },
                "index_document": {
                    "type": "string",
                    "description": "Filename to use as the website's homepage (e.g., 'index.html', 'home.html'). Optional — auto-detected from uploaded files when there is exactly one HTML file. Defaults to 'index.html'.",
                },
            },
            "required": ["project_name", "env", "site_title", "owner_name", "owner_email"],
        },
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
                    "description": "S3 bucket name returned by deploy_infrastructure.",
                },
                "distribution_id": {
                    "type": "string",
                    "description": "CloudFront distribution ID returned by deploy_infrastructure.",
                },
            },
            "required": ["bucket_name", "distribution_id"],
        },
    },
]


# ── Tool Implementations ───────────────────────────────────────────────────────


def deploy_infrastructure(
    project_name: str,
    env: str,
    site_title: str,
    owner_name: str,
    owner_email: str,
    is_spa: bool = False,
    index_document: str | None = None,
    *,
    session=None,
    **_,
) -> dict:
    """Run tofu init + apply for the static-website stack."""
    upload_dir = session.upload_dir if session is not None else None
    preflight_error, auto_index = _preflight_uploads(upload_dir)
    if preflight_error is not None:
        return preflight_error
    chosen_index = index_document or auto_index

    try:
        # 1. Init
        r = subprocess.run(
            ["tofu", "init", "-input=false"],
            cwd=STACK_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            return _classify_error(r.stderr)

        # 2. Create or select workspace (isolates state per deployment)
        workspace = f"{project_name}-{env}"
        subprocess.run(["tofu", "workspace", "new", workspace], cwd=STACK_DIR, capture_output=True)
        subprocess.run(
            ["tofu", "workspace", "select", workspace], cwd=STACK_DIR, capture_output=True
        )

        # 3. Apply
        apply_cmd = [
            "tofu",
            "apply",
            "-auto-approve",
            "-input=false",
            f"-var=project_name={project_name}",
            f"-var=env={env}",
            f"-var=site_title={site_title}",
            f"-var=owner_name={owner_name}",
            f"-var=owner_email={owner_email}",
            f"-var=is_spa={'true' if is_spa else 'false'}",
        ]
        if chosen_index:
            apply_cmd.append(f"-var=index_document={chosen_index}")
        r = subprocess.run(
            apply_cmd,
            cwd=STACK_DIR,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if r.returncode != 0:
            return _classify_error(r.stderr)

        # 4. Read outputs
        out = subprocess.run(
            ["tofu", "output", "-json"],
            cwd=STACK_DIR, capture_output=True, text=True
        )
        if out.returncode != 0:
            return _classify_error(out.stderr)
        outputs = json.loads(out.stdout)

        return {
            "bucket_name": outputs["bucket_name"]["value"],
            "site_url": outputs["site_url"]["value"],
            "cloudfront_distribution_id": outputs["cloudfront_distribution_id"]["value"],
            "project_name": project_name,
            "env": env,
        }

    except subprocess.TimeoutExpired:
        return {"summary": "Deployment timed out after 10 minutes.", "details": ""}
    except Exception as e:
        return {"summary": "Deployment failed unexpectedly.", "details": str(e)}


def upload_files(bucket_name: str, distribution_id: str, session, **_) -> dict:
    """Upload files from the session's temp directory to S3, then invalidate CloudFront."""
    upload_dir = session.upload_dir
    if not upload_dir or not Path(upload_dir).exists():
        return {"summary": "No uploaded files found for this session.", "details": ""}

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
                str(file_path), bucket_name, key, ExtraArgs={"ContentType": content_type}
            )
            uploaded.append(key)

        # Bust CloudFront cache
        cf.create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": ["/*"]},
                "CallerReference": f"deploy-agent-{distribution_id}",
            },
        )

        return {"uploaded_count": len(uploaded), "files": uploaded, "cache_invalidated": True}

    except Exception as e:
        return {"summary": "File upload failed.", "details": str(e)}


def execute_tool(name: str, inputs: dict, session_id: str, session) -> dict:
    """Dispatch a tool call from the Claude agent."""
    if name == "deploy_infrastructure":
        return deploy_infrastructure(session=session, **inputs)
    elif name == "upload_files":
        return upload_files(session=session, **inputs)
    return {"summary": f"Unknown tool: {name}", "details": ""}
