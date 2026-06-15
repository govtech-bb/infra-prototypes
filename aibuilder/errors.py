# aibuilder/errors.py
"""Map tofu/AWS stderr to friendly {summary, details} error dicts.

Ported verbatim from deploy-agent/tools.py:_classify_error. The pattern
order matters — bucket-collision is checked before AccessDenied so the
more specific message wins. Add patterns in order of specificity.
"""

from __future__ import annotations

import re

_ERROR_PATTERNS: list[tuple[str, str]] = [
    (
        r"NoCredentialProviders|Unable to locate credentials",
        "No AWS credentials found in the deploy task — check the task role.",
    ),
    (
        r"BucketAlreadyOwnedByYou|BucketAlreadyExists",
        "A bucket with this name already exists. Pick a different project name.",
    ),
    (
        r"AccessDenied|UnauthorizedOperation|is not authorized to",
        "The deploy task role lacks permission for this operation. Check IAM.",
    ),
    (
        r"NoSuchBucket",
        "S3 bucket not found — it may have been deleted out-of-band.",
    ),
    (
        r"Error: error configuring",
        "AWS configuration error — check the region and credentials.",
    ),
]


def classify_error(stderr: str) -> dict:
    details = stderr[-2000:]
    for pattern, summary in _ERROR_PATTERNS:
        if re.search(pattern, stderr, re.IGNORECASE):
            return {"summary": summary, "details": details}
    return {"summary": "Deployment failed — see details.", "details": details}
