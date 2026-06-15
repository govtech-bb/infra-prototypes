"""Deploy caps to keep sandbox spend bounded.

Per-session and global daily counters read from the deployments table.
Caps are env-configurable — defaults sized for a small team
prototyping. Failures use the standard {summary, details} shape so the
agent surfaces a friendly message via its existing convention.
"""

from __future__ import annotations

import os

from deployments import SqliteDeploymentStore

_DEFAULT_PER_SESSION = 10
_DEFAULT_GLOBAL = 50


def _session_cap() -> int:
    return int(os.environ.get("AIBUILDER_MAX_DEPLOYS_PER_SESSION_DAY", _DEFAULT_PER_SESSION))


def _global_cap() -> int:
    return int(os.environ.get("AIBUILDER_MAX_DEPLOYS_GLOBAL_DAY", _DEFAULT_GLOBAL))


def check_caps(store: SqliteDeploymentStore, *, session_id: str) -> dict | None:
    """Return None if a new deploy may proceed, else {summary, details}."""
    session_cap = _session_cap()
    global_cap = _global_cap()
    session_count = store.count_today_for_session(session_id)
    global_count = store.count_today_global()
    if session_count >= session_cap:
        return {
            "summary": (
                f"This session has used its daily deploy budget "
                f"({session_count}/{session_cap}). Try again tomorrow or destroy "
                "an existing deployment to free a slot."
            ),
            "details": f"session={session_id} count={session_count} cap={session_cap}",
        }
    if global_count >= global_cap:
        return {
            "summary": (
                f"Global daily deploy cap reached ({global_count}/{global_cap}). "
                "Wait until tomorrow."
            ),
            "details": f"global_count={global_count} cap={global_cap}",
        }
    return None
