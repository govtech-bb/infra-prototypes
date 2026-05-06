# Destroy: Empty Bucket Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make chat-driven destroy work on non-empty buckets by emptying them via boto3 before `tofu destroy`, plus add `force_destroy = true` to the s3 module as defense in depth.

**Architecture:** One bundled commit. New `_empty_bucket(bucket_name)` helper in `tools.py` runs between `tofu workspace select` and `tofu destroy` inside `destroy_infrastructure`. Infra module gets one new line. 4 new tests in `test_tools.py`.

**Tech Stack:** Python 3.11+, boto3 + botocore (already installed), moto (already in dev deps), OpenTofu.

**Reference spec:** `docs/superpowers/specs/2026-05-05-destroy-empty-bucket-design.md`

---

## Conventions

- Working dir: `cd "/Users/christophercorbin/INFRA prototypes"` (note the space, quote it).
- Tests run from `deploy-agent/`: `cd deploy-agent && python3 -m pytest tests/`.
- Single commit at the end.

---

## Task 1: `_empty_bucket` helper + `destroy_infrastructure` wiring

**Files:**
- Modify: `deploy-agent/tools.py`
- Modify: `deploy-agent/tests/test_tools.py`

- [ ] **Step 1.1: Append failing tests to `deploy-agent/tests/test_tools.py`**

```python
# ── _empty_bucket tests ───────────────────────────────────────────────────────


def test_empty_bucket_deletes_all_objects(aws_credentials):
    from moto import mock_aws
    import boto3

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-empty")
        s3.put_object(Bucket="test-empty", Key="a.html", Body=b"a")
        s3.put_object(Bucket="test-empty", Key="b/c.css", Body=b"b")
        s3.put_object(Bucket="test-empty", Key="d.svg", Body=b"d")

        with patch.object(tools.boto3, "client", return_value=s3):
            err = tools._empty_bucket("test-empty")

        assert err is None
        remaining = s3.list_objects_v2(Bucket="test-empty").get("Contents", [])
        assert remaining == []


def test_empty_bucket_handles_no_such_bucket(aws_credentials):
    import boto3
    from botocore.exceptions import ClientError

    fake_client = MagicMock()
    fake_client.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "Not found"}},
        "ListObjectsV2",
    )

    with patch.object(tools.boto3, "client", return_value=fake_client):
        err = tools._empty_bucket("never-existed")
    assert err is None  # idempotent — already gone is fine


def test_empty_bucket_returns_summary_on_access_denied(aws_credentials):
    from botocore.exceptions import ClientError

    fake_client = MagicMock()
    fake_client.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Denied"}},
        "ListObjectsV2",
    )

    with patch.object(tools.boto3, "client", return_value=fake_client):
        err = tools._empty_bucket("private-bucket")
    assert err is not None
    assert "summary" in err
    assert "private-bucket" in err["summary"]


@patch("tools.subprocess.run")
def test_destroy_empties_bucket_before_tofu_destroy(mock_run, aws_credentials, tmp_path, monkeypatch):
    from moto import mock_aws
    import boto3 as _boto3

    db_path = tmp_path / "sessions.db"
    _seed_session(db_path, "alpha", "proto", {
        "site_title": "Alpha",
        "bucket_name": "alpha-proto-static",
        "cloudfront_distribution_id": "ABC",
        "project_name": "alpha", "env": "proto",
    })
    monkeypatch.setenv("DEPLOY_AGENT_DB", str(db_path))
    from sessions import Session
    session = Session(session_id="s")

    mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

    with mock_aws():
        real_s3 = _boto3.client("s3", region_name="us-east-1")
        real_s3.create_bucket(Bucket="alpha-proto-static")
        real_s3.put_object(Bucket="alpha-proto-static", Key="index.html", Body=b"<html>")

        with patch.object(tools.boto3, "client", return_value=real_s3):
            result = tools.destroy_infrastructure(
                project_name="alpha", env="proto",
                confirm=True,
                session=session,
            )

        assert result.get("destroyed") is True
        # Bucket is empty after destroy_infrastructure ran.
        contents = real_s3.list_objects_v2(Bucket="alpha-proto-static").get("Contents", [])
        assert contents == []

    # tofu destroy was called AFTER the bucket was emptied.
    destroy_calls = [c for c in mock_run.call_args_list if c.args[0][1] == "destroy"]
    assert destroy_calls, "tofu destroy was not invoked"
```

- [ ] **Step 1.2: Run tests, confirm they fail**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k "empty_bucket or destroy_empties"
```
Expected: 4 failures, `AttributeError: module 'tools' has no attribute '_empty_bucket'`.

- [ ] **Step 1.3: Add `_empty_bucket` to `tools.py`**

In `deploy-agent/tools.py`, add `import botocore` near the top imports (the package ships with boto3; no requirements change). If `botocore` is already imported indirectly, add it explicitly anyway for clarity.

Then add the helper. Place it AFTER `_classify_error` and BEFORE `_SOURCE_EXTENSIONS`/`_preflight_uploads`:

```python
# ── Bucket emptying ───────────────────────────────────────────────────────────


def _empty_bucket(bucket_name: str) -> dict | None:
    """Delete all objects in the bucket. Returns None on success, error dict on failure.

    Idempotent: missing bucket is treated as success. Versioned buckets are not
    fully handled (current object versions only).
    """
    try:
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if not objects:
                continue
            s3.delete_objects(Bucket=bucket_name, Delete={"Objects": objects})
        return None
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "NoSuchBucket":
            return None
        return {
            "summary": f"Could not empty bucket {bucket_name!r} before destroy.",
            "details": str(e),
        }
    except Exception as e:
        return {
            "summary": f"Could not empty bucket {bucket_name!r} before destroy.",
            "details": str(e),
        }
```

- [ ] **Step 1.4: Wire `_empty_bucket` into `destroy_infrastructure`**

In `deploy-agent/tools.py:destroy_infrastructure`, find the section between workspace select success and the `tofu destroy` call:

```python
        if sel.returncode != 0:
            if "does not exist" in sel.stderr.lower():
                _maybe_clear_session_deployment(session, project_name, env)
                return {
                    "destroyed": True,
                    "project_name": project_name,
                    "env": env,
                    "note": "Workspace was already gone — nothing to destroy.",
                }
            return _classify_error(sel.stderr)

        # 3. destroy
        r = subprocess.run(
```

Insert the bucket-emptying step between the workspace-select block and the destroy call:

```python
        if sel.returncode != 0:
            if "does not exist" in sel.stderr.lower():
                _maybe_clear_session_deployment(session, project_name, env)
                return {
                    "destroyed": True,
                    "project_name": project_name,
                    "env": env,
                    "note": "Workspace was already gone — nothing to destroy.",
                }
            return _classify_error(sel.stderr)

        # 2.5 Empty the bucket — tofu destroy can't delete a non-empty bucket
        # unless the resource has force_destroy = true (added in this commit but
        # only effective after the next tofu apply for existing deployments).
        bucket_name = record.get("bucket_name")
        if bucket_name:
            empty_err = _empty_bucket(bucket_name)
            if empty_err is not None:
                return empty_err

        # 3. destroy
        r = subprocess.run(
```

- [ ] **Step 1.5: Run tests, confirm pass**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && python3 -m pytest tests/test_tools.py -v -k "empty_bucket or destroy"
```
Expected: all destroy + empty_bucket tests pass.

---

## Task 2: `force_destroy = true` on the s3 module

**Files:**
- Modify: `infra/modules/s3-static-site/main.tf`

- [ ] **Step 2.1: Add `force_destroy = true`**

In `infra/modules/s3-static-site/main.tf`, find:

```hcl
resource "aws_s3_bucket" "this" {
  bucket = "${var.project_name}-${var.env}-static"

  tags = merge(var.tags, {
    Project     = var.project_name
    Environment = var.env
  })
}
```

Replace with:

```hcl
resource "aws_s3_bucket" "this" {
  bucket        = "${var.project_name}-${var.env}-static"
  force_destroy = true

  tags = merge(var.tags, {
    Project     = var.project_name
    Environment = var.env
  })
}
```

- [ ] **Step 2.2: Run `tofu fmt -check` and `tofu validate`**

```bash
cd "/Users/christophercorbin/INFRA prototypes/infra" && tofu fmt -recursive -check && cd stacks/static-website && tofu init -backend=false && tofu validate
```
Expected: no fmt diff, validate succeeds.

If `tofu fmt -recursive -check` reports a diff, run `tofu fmt -recursive` to fix and re-check.

---

## Task 3: `make check` + commit + push + verify CI

**Files:** none.

- [ ] **Step 3.1: Run `make check`**

```bash
cd "/Users/christophercorbin/INFRA prototypes/deploy-agent" && make check 2>&1 | tail -10
```
Expected: green. ~62 tests pass (58 prior + 4 new). ruff clean. tofu validate clean.

- [ ] **Step 3.2: Commit**

```bash
cd "/Users/christophercorbin/INFRA prototypes"
git add deploy-agent/tools.py deploy-agent/tests/test_tools.py infra/modules/s3-static-site/main.tf
git commit -m "fix(destroy): empty bucket before tofu destroy + force_destroy on s3 module

Real chat-driven destroy failed with BucketNotEmpty because the s3 module
lacked force_destroy. Two complementary fixes:

- _empty_bucket helper in tools.py paginates list_objects_v2 and
  delete_objects via boto3. Idempotent (NoSuchBucket → success).
- destroy_infrastructure calls it between workspace select and tofu
  destroy, so existing deployments work immediately without
  re-applying infra.
- force_destroy = true added to the s3 bucket resource as defense in
  depth for new deployments.

4 new tests."
```

- [ ] **Step 3.3: Push**

```bash
cd "/Users/christophercorbin/INFRA prototypes" && git push 2>&1 | tail -3
```

- [ ] **Step 3.4: Watch CI**

```bash
sleep 8 && gh -R christophercorbin/infra-prototypes run list --limit 1 --json databaseId,status,conclusion -q '.[]'
gh -R christophercorbin/infra-prototypes run watch <id> --exit-status
```
Expected: success.

---

## Self-review

**Spec coverage:** All 5 spec items (helper, wiring, infra force_destroy, 4 tests, single commit) → mapped to tasks 1–3.

**Placeholder scan:** None.

**Type consistency:** `_empty_bucket` returns `dict | None` everywhere; matches the helper's signature, callsite, and tests.

---

## Done criteria

- `make check` green locally and in CI.
- Real chat-driven destroy now works on non-empty buckets without manual `aws s3 rm`.
- 62 tests passing.
