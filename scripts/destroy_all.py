#!/usr/bin/env python3
"""Tear down every deployment recorded in the SQLite session store.

Reads project_name/env from each session's deployment row, runs `tofu workspace
select` + `tofu destroy` for each. Skips and logs sessions whose workspace no
longer exists (e.g., already destroyed manually).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "deploy-agent" / "data" / "sessions.db"
STACK_DIR = REPO_ROOT / "infra" / "stacks" / "static-website"


def main() -> int:
    db_path = Path(os.environ.get("DEPLOY_AGENT_DB", DEFAULT_DB))
    if not db_path.exists():
        print(f"No session DB at {db_path}; nothing to destroy.")
        return 0

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT session_id, project_name, env FROM sessions "
        "WHERE deployment IS NOT NULL AND project_name IS NOT NULL"
    ).fetchall()
    conn.close()

    if not rows:
        print("No deployments recorded.")
        return 0

    print(f"Found {len(rows)} deployment(s) to destroy.")

    # Ensure the stack is initialized — fresh checkouts have no .terraform/.
    init = subprocess.run(
        ["tofu", "init", "-input=false"],
        cwd=STACK_DIR, capture_output=True, text=True,
    )
    if init.returncode != 0:
        print(f"tofu init failed:\n{init.stderr.strip()}")
        return 1

    failures = 0
    for session_id, project, env in rows:
        workspace = f"{project}-{env}"
        print(f"\n→ Destroying {workspace} (session {session_id[:8]}…)")

        select = subprocess.run(
            ["tofu", "workspace", "select", workspace],
            cwd=STACK_DIR, capture_output=True, text=True,
        )
        if select.returncode != 0:
            print(f"  workspace select failed (likely already destroyed): "
                  f"{select.stderr.strip()[:200]}")
            continue

        destroy = subprocess.run(
            [
                "tofu", "destroy", "-auto-approve", "-input=false",
                f"-var=project_name={project}",
                f"-var=env={env}",
            ],
            cwd=STACK_DIR,
        )
        if destroy.returncode != 0:
            print(f"  destroy failed (rc={destroy.returncode})")
            failures += 1
        else:
            print("  destroyed.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
