"""Job bodies. Imports are kept local to avoid circular deps with tools.py."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import subprocess
from pathlib import Path

import boto3

import gh_clone
from deploy_stacks import get_spec
from deployments import Deployment, DeploymentStatus, SqliteDeploymentStore
from errors import classify_error

log = logging.getLogger("aibuilder.jobs")
_STORE: SqliteDeploymentStore | None = None


def configure(store: SqliteDeploymentStore) -> None:
    global _STORE
    _STORE = store


def _workdir(deployment_id: str) -> Path:
    root = Path(os.environ.get("AIBUILDER_DEPLOY_WORKDIR", "/aibuilder/data/deploys"))
    p = root / deployment_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _update(d: Deployment, status: DeploymentStatus, error: str | None = None) -> None:
    d.status = status
    if error is not None:
        d.last_error = error
    _STORE.save(d)


def _backend_config_args() -> list[str]:
    return [
        f"-backend-config=bucket={os.environ.get('AIBUILDER_DEPLOY_STATE_BUCKET', '')}",
        f"-backend-config=region={os.environ.get('AWS_REGION', 'us-east-1')}",
        f"-backend-config=dynamodb_table={os.environ.get('AIBUILDER_DEPLOY_LOCK_TABLE', '')}",
    ]


def _var_args(spec, d) -> list[str]:
    args: list[str] = []
    for k, v in spec.build_vars(d).items():
        if isinstance(v, bool):
            args.append(f"-var={k}={'true' if v else 'false'}")
        else:
            args.append(f"-var={k}={v}")
    return args


async def run_deploy_job(deployment_id: str) -> None:
    d = _STORE.get(deployment_id)
    if d is None:
        log.error("deployment %s vanished before job ran", deployment_id)
        return

    spec = get_spec(d.pattern)
    if spec is None:
        _update(d, DeploymentStatus.FAILED, f"pattern not registered: {d.pattern}")
        return

    work = _workdir(deployment_id)

    # 1. Clone
    _update(d, DeploymentStatus.CLONING)
    repo_path, err = gh_clone.clone(d.repo_url, work / "src")
    if err:
        _update(d, DeploymentStatus.FAILED, err["summary"] + " :: " + err["details"])
        return

    # 2. Apply
    _update(d, DeploymentStatus.APPLYING)
    state_key = f"deployments/{d.project_name}-{d.env}.tfstate"
    env = {
        **os.environ,
        "TF_DATA_DIR": str(work / "tf"),
    }
    init = subprocess.run(
        ["tofu", "init", "-input=false", "-reconfigure",
         f"-backend-config=key={state_key}", *_backend_config_args()],
        cwd=spec.stack_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    if init.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(init.stderr)["details"])
        return

    apply_res = subprocess.run(
        ["tofu", "apply", "-auto-approve", "-input=false", *_var_args(spec, d)],
        cwd=spec.stack_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=900,
    )
    if apply_res.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(apply_res.stderr)["details"])
        return

    out_res = subprocess.run(
        ["tofu", "output", "-json"],
        cwd=spec.stack_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    outputs_raw = json.loads(out_res.stdout) if out_res.stdout else {}
    d.outputs = {k: v.get("value") for k, v in outputs_raw.items()}

    # 3. Sync content
    _update(d, DeploymentStatus.SYNCING)
    sync_err = await sync_content(d, repo_path)
    if sync_err:
        _update(d, DeploymentStatus.FAILED, sync_err["summary"] + " :: " + sync_err["details"])
        return

    _update(d, DeploymentStatus.LIVE)


async def sync_content(d: Deployment, repo_path: Path) -> dict | None:
    """W1 only: boto3 sync the cloned repo to the deployment's bucket + invalidate CF.

    Returns None on success or {summary, details} on error. Runs in a thread
    pool because boto3 is sync.
    """
    bucket = d.outputs.get("bucket_name")
    distribution = d.outputs.get("cloudfront_distribution_id")
    if not bucket:
        return {"summary": "tofu output missing bucket_name.", "details": str(d.outputs)}

    def _sync() -> dict | None:
        try:
            s3 = boto3.client("s3")
            cf = boto3.client("cloudfront")
            for p in sorted(Path(repo_path).rglob("*")):
                if not p.is_file() or any(part.startswith(".git") for part in p.parts):
                    continue
                key = str(p.relative_to(repo_path))
                content_type, _ = mimetypes.guess_type(str(p))
                s3.upload_file(
                    str(p), bucket, key,
                    ExtraArgs={"ContentType": content_type or "application/octet-stream"},
                )
            if distribution:
                cf.create_invalidation(
                    DistributionId=distribution,
                    InvalidationBatch={
                        "Paths": {"Quantity": 1, "Items": ["/*"]},
                        "CallerReference": f"aibuilder-{d.deployment_id}",
                    },
                )
            return None
        except Exception as e:
            return {"summary": "Content sync failed.", "details": str(e)}

    return await asyncio.to_thread(_sync)


async def run_redeploy_job(deployment_id: str) -> None:
    d = _STORE.get(deployment_id)
    if d is None:
        return
    _update(d, DeploymentStatus.CLONING)
    work = _workdir(deployment_id)
    if (work / "src").exists():
        import shutil
        shutil.rmtree(work / "src")
    repo_path, err = gh_clone.clone(d.repo_url, work / "src")
    if err:
        _update(d, DeploymentStatus.FAILED, err["summary"] + " :: " + err["details"])
        return
    _update(d, DeploymentStatus.SYNCING)
    sync_err = await sync_content(d, repo_path)
    if sync_err:
        _update(d, DeploymentStatus.FAILED, sync_err["summary"])
        return
    _update(d, DeploymentStatus.LIVE)


async def run_modify_job(deployment_id: str) -> None:
    d = _STORE.get(deployment_id)
    if d is None:
        return
    spec = get_spec(d.pattern)
    if spec is None:
        _update(d, DeploymentStatus.FAILED, f"no spec for {d.pattern}")
        return
    _update(d, DeploymentStatus.MODIFYING)
    work = _workdir(deployment_id)
    env = {**os.environ, "TF_DATA_DIR": str(work / "tf")}
    state_key = f"deployments/{d.project_name}-{d.env}.tfstate"
    init = subprocess.run(
        ["tofu", "init", "-input=false", "-reconfigure",
         f"-backend-config=key={state_key}", *_backend_config_args()],
        cwd=spec.stack_dir, capture_output=True, text=True, env=env, timeout=180,
    )
    if init.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(init.stderr)["details"])
        return
    apply_res = subprocess.run(
        ["tofu", "apply", "-auto-approve", "-input=false", *_var_args(spec, d)],
        cwd=spec.stack_dir, capture_output=True, text=True, env=env, timeout=900,
    )
    if apply_res.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(apply_res.stderr)["details"])
        return
    _update(d, DeploymentStatus.LIVE)


async def run_destroy_job(deployment_id: str) -> None:
    d = _STORE.get(deployment_id)
    if d is None:
        return
    spec = get_spec(d.pattern)
    if spec is None:
        _update(d, DeploymentStatus.FAILED, f"no spec for {d.pattern}")
        return
    _update(d, DeploymentStatus.DESTROYING)

    bucket = d.outputs.get("bucket_name")
    if bucket:
        err = await asyncio.to_thread(_empty_bucket_sync, bucket)
        if err:
            _update(d, DeploymentStatus.FAILED, err["summary"])
            return

    work = _workdir(deployment_id)
    env = {**os.environ, "TF_DATA_DIR": str(work / "tf")}
    state_key = f"deployments/{d.project_name}-{d.env}.tfstate"
    init = subprocess.run(
        ["tofu", "init", "-input=false", "-reconfigure",
         f"-backend-config=key={state_key}", *_backend_config_args()],
        cwd=spec.stack_dir, capture_output=True, text=True, env=env, timeout=180,
    )
    if init.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(init.stderr)["details"])
        return
    destroy_res = subprocess.run(
        ["tofu", "destroy", "-auto-approve", "-input=false", *_var_args(spec, d)],
        cwd=spec.stack_dir, capture_output=True, text=True, env=env, timeout=600,
    )
    if destroy_res.returncode != 0:
        _update(d, DeploymentStatus.FAILED, classify_error(destroy_res.stderr)["details"])
        return
    _update(d, DeploymentStatus.DESTROYED)


def _empty_bucket_sync(bucket: str) -> dict | None:
    import botocore.exceptions

    try:
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        return None
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "NoSuchBucket":
            return None
        return {"summary": f"Could not empty bucket {bucket}.", "details": str(e)}
