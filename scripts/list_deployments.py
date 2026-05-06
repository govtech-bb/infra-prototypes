#!/usr/bin/env python3
"""Print every active static-site deployment recorded in sessions.db.

Active = recorded in sessions.db AND tofu workspace still exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make tools.py importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "deploy-agent"))

from tools import _read_active_deployments  # noqa: E402


def main() -> int:
    deployments = _read_active_deployments()
    if not deployments:
        print("No active deployments.")
        return 0

    print(f"{'project':<25} {'env':<10} {'site_title':<25} {'site_url'}")
    print("-" * 90)
    for d in deployments:
        print(
            f"{d['project_name']:<25} "
            f"{d['env']:<10} "
            f"{d['site_title']:<25.25} "
            f"{d['site_url']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
