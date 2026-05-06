# Destroy: Empty Bucket Before Tofu Destroy — Design

**Date:** 2026-05-05
**Scope:** One-commit fix that unblocks chat-driven destroy when the S3 bucket is non-empty.

## Problem

A real chat-driven destroy attempt failed with `BucketNotEmpty`:

> The destroy failed because the S3 bucket is not empty — AWS won't delete a bucket that still has files in it.

Terraform/AWS provider refuses to delete a non-empty bucket unless `force_destroy = true` is set. The current s3 module lacks this attribute. The agent classified the error correctly but couldn't recover; it told the user to run `aws s3 rm ... --recursive` manually.

## Goals

- Chat-driven destroy works on non-empty buckets without the user dropping to the CLI.
- Future deployments are protected against this same failure mode at the infra layer.

## Non-goals

- Recovering object versions on versioned buckets (rare for prototypes; out of scope).
- Adding bucket-emptying to `destroy_all.py` CLI (handled by the same code path indirectly when `destroy_infrastructure` is called via the agent; standalone CLI use is rare).

## Architecture — two complementary fixes

### Fix 1 — Agent empties the bucket before `tofu destroy`

In `tools.py:destroy_infrastructure`, between the successful `tofu workspace select` and the `tofu destroy` call, add a step that empties the bucket via `boto3`:

```python
# Pre-destroy: empty the S3 bucket so tofu destroy can succeed.
bucket_name = record.get("bucket_name")
if bucket_name:
    err = _empty_bucket(bucket_name)
    if err is not None:
        return err
```

`_empty_bucket(bucket_name)` is a new helper. It paginates `list_objects_v2`, batches deletes via `delete_objects` (1000-key cap per call), and handles two benign cases:

- `NoSuchBucket` → `None` (already gone, let tofu destroy do its thing).
- Successful empty → `None`.
- `AccessDenied` or other `ClientError` → `{"summary": ..., "details": ...}` so the agent surfaces it.

```python
def _empty_bucket(bucket_name: str) -> dict | None:
    """Delete all objects in the bucket. Returns None on success, error dict on failure."""
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

Note: `botocore` ships with `boto3` so no new requirement.

### Fix 2 — `force_destroy = true` on the s3 bucket resource

In `infra/modules/s3-static-site/main.tf`:

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

`force_destroy` is read by the AWS provider at destroy time. For existing deployments it kicks in only after `tofu apply -refresh-only` (or any apply that updates state); the agent's bucket-emptying covers that interim. New deployments get it baked into state from day one.

This is appropriate for a prototype: the whole system is "deploy fast, tear down fast." Production single-source-of-truth buckets would NOT want force_destroy.

## Tests

In `deploy-agent/tests/test_tools.py`:

- `test_empty_bucket_deletes_all_objects` — moto S3 with 3 objects; `_empty_bucket(name)` returns None and bucket is empty afterward.
- `test_empty_bucket_handles_no_such_bucket` — bucket doesn't exist; `_empty_bucket` returns None (no error).
- `test_empty_bucket_returns_summary_on_access_denied` — mock S3 raises ClientError code `AccessDenied`; helper returns `{"summary": ..., "details": ...}`.
- `test_destroy_empties_bucket_before_tofu_destroy` — full flow: matching record, `confirm=True`, mocked moto S3 with objects, mocked subprocess. Asserts `_empty_bucket` ran (bucket is empty by the end of the test) and `tofu destroy` was called after.

That's 4 new tests.

## Out of scope

- Versioned buckets (would need `delete_object_versions` paginator + delete-markers handling).
- A separate `empty_bucket` agent tool (folded into destroy; one call instead of two).
- Updating `destroy_all.py` to also empty buckets — currently it iterates per workspace via `tofu destroy`, which after Fix 2 (post-apply) handles non-empty buckets natively. For deployments that haven't had `tofu apply` re-run since Fix 2 lands, manual `aws s3 rm` remains the workaround for the CLI path.

## Build sequence

Single commit:

1. Add `_empty_bucket` to `tools.py`.
2. Wire it into `destroy_infrastructure` (between workspace select and tofu destroy).
3. Add `force_destroy = true` to `infra/modules/s3-static-site/main.tf`.
4. Add the 4 tests.
5. `make check` green → commit + push.
