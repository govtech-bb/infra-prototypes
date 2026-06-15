"""Register the static_site catalog pattern → vendored tofu stack.

The deploy task role's W1 IAM policy is scoped to `aibd-*` resource
names, so build_vars prepends that prefix to the project_name. Knobs
the agent is allowed to flip via modify_deployment are explicit.
"""

from __future__ import annotations

from pathlib import Path

from deploy_stacks import StackSpec, register
from deployments import Deployment

_STACK_DIR = str(Path(__file__).parent / "static_website")
_AIBD_PREFIX = "aibd-"


def _build_vars(d: Deployment) -> dict:
    return {
        "project_name": f"{_AIBD_PREFIX}{d.project_name}",
        "env": d.env,
        "is_spa": bool(d.knobs.get("is_spa", False)),
        "price_class": d.knobs.get("price_class", "PriceClass_100"),
        "site_title": d.knobs.get("site_title", ""),
        "owner_name": d.knobs.get("owner_name", ""),
        "owner_email": d.knobs.get("owner_email", ""),
    }


register(
    StackSpec(
        pattern="static_site",
        stack_dir=_STACK_DIR,
        build_vars=_build_vars,
        allowed_knobs=["is_spa", "price_class", "site_title", "owner_name", "owner_email"],
    )
)
