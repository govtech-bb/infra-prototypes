# Upload Pre-Flight + Custom Entry Filename + Destroy Workflow — Design

**Date:** 2026-05-05
**Scope:** Two enhancements on top of sub-project A, bundled because both add tools to `tools.py` + bullets to the system prompt.

1. **Pre-flight upload check + custom entry filename** — stop deploys that won't render and let users deploy a site whose homepage isn't `index.html`.
2. **Chat-driven destroy** — the agent gains read-only `list_deployments` and a two-phase `destroy_infrastructure` so the user can tear down deployments by talking to the agent.

## Problems

**(P1) The agent provisions a CloudFront distribution + S3 bucket on uploads that won't render** — e.g., a single `.jsx` source file with no `index.html`. CloudFront returns S3's `AccessDenied` for the missing default root object, and the user gets a confusing 403 instead of a useful error. There is also no way to tell tofu "use `home.html` as the homepage."

**(P2) There is no chat-driven destroy.** The user can deploy by talking to the agent but has to drop to the terminal (`make destroy PROJECT=X ENV=Y`) to clean up. Worse, if they don't remember the exact `project_name`+`env`, they have to grep `sessions.db` themselves.

## Goals

- Stop deploys that won't work before any AWS API call.
- Support a non-`index.html` homepage when there's exactly one HTML file.
- Let the user say "destroy this site" / "destroy <name>" / "destroy everything" and have the agent do it safely.
- All destroys cite an exact `project_name`+`env` — no fuzzy matching. Confirmation required.

## Non-goals

- Build automation (`npm run build`) — future "C" option.
- HTML semantic validation (does `<head>` reference uploaded assets correctly).
- Auto-restore destroyed deployments.
- A separate UI for destroy (buttons in the chat); chat text is the interface.

## Architecture

### Pre-flight (P1)

A new module-level helper in `deploy-agent/tools.py`:

```python
def _preflight_uploads(upload_dir: str | None) -> tuple[dict | None, str | None]:
    """Inspect uploaded files before deploy.

    Returns:
        (error_dict, index_document)
        - error_dict: {"summary", "details"} dict if deploy should be blocked, else None.
        - index_document: filename to use as the website's homepage, or None
          (caller defaults to "index.html"). Only set when auto-detected.
    """
```

`deploy_infrastructure` calls it as the first thing it does. If `error_dict` is non-None, return it immediately (no AWS calls). If `index_document` is set, pass it through to tofu via `-var=index_document=<value>`.

Pre-flight rules — first match wins:

1. **Empty upload.** No files in `upload_dir` (or directory missing). Return `({"summary": "No files uploaded yet — drag a folder into the chat first.", "details": ""}, None)`.

2. **Source code with no HTML.** Any file matches the regex `\.(jsx|tsx|ts|vue|svelte|scss|sass|less)$` (case-insensitive) AND no file ends in `.html` or `.htm` (case-insensitive). Return `({"summary": "Looks like source code, not a built site. Run 'npm run build' (or your project's build command) and upload the output folder (usually 'dist/' or 'build/').", "details": "Found: <comma-separated list, max 10 entries>"}, None)`.

3. **HTML present, no `index.html`.**
   - **Exactly one HTML file** (case-insensitive, e.g. `home.html`, `Home.HTML`): return `(None, "<that filename>")`. Auto-selection — agent's reply will mention the chosen filename.
   - **Multiple HTML files, none named `index.html`**: return `({"summary": "Multiple HTML files but no index.html. Tell me which one is the homepage (e.g., 'use home.html as the entry').", "details": "Found: <list>"}, None)`.

4. **`index.html` present.** Return `(None, None)` — caller uses the default.

`deploy_infrastructure` gains an optional `index_document` parameter (string). Resolution order: explicit caller value > auto-detected from preflight > default `"index.html"`.

### Destroy (P2)

Two new tools in `tools.py`, exposed via `TOOL_DEFINITIONS`:

#### `list_deployments`

```python
{
    "name": "list_deployments",
    "description": "List every active static-site deployment recorded for this user. Read-only — no infrastructure changes. Use before destroy_infrastructure when the user's request is ambiguous (e.g., 'destroy my old test site').",
    "input_schema": {"type": "object", "properties": {}}
}
```

Implementation:

```python
def list_deployments(*, session, **_) -> dict:
    """Read sessions.db, intersect with current tofu workspaces, return active deployments."""
```

Mechanics:
1. Read `DEPLOY_AGENT_DB` (or default `data/sessions.db`) — query sessions where `deployment IS NOT NULL` and pull `project_name`, `env`, plus the `deployment` JSON for `site_title`, `owner_name`, `site_url`, and `updated_at`.
2. Run `tofu workspace list` once in the stack dir; intersect with sessions.db rows so destroyed-but-still-recorded deployments don't appear.
3. Return `{"deployments": [{project_name, env, site_title, owner_name, site_url, updated_at}, ...]}`. Empty list when nothing is active. No `summary` field on success.
4. On error (DB missing, tofu not on PATH): return `{"summary": "Could not list deployments.", "details": "<reason>"}`.

#### `destroy_infrastructure`

```python
{
    "name": "destroy_infrastructure",
    "description": (
        "Destroy a static-site deployment. Two-phase: first call with confirm=False to "
        "get a preview, then call again with confirm=True to actually destroy. "
        "Always cite the exact project_name and env from list_deployments — never guess."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project_name": {"type": "string", "description": "Exact project_name from list_deployments / session.deployment."},
            "env":          {"type": "string", "description": "Exact env from list_deployments / session.deployment."},
            "confirm":      {"type": "boolean", "description": "Set to true to actually destroy. Default false returns a preview only."}
        },
        "required": ["project_name", "env"]
    }
}
```

Implementation:

```python
def destroy_infrastructure(project_name: str, env: str, confirm: bool = False, *, session, **_) -> dict:
    ...
```

Mechanics:
1. Look up the deployment in `sessions.db` to get `site_url` / `bucket_name` / `cloudfront_distribution_id` for the preview. If no matching record, return `{"summary": "No record of '<project>-<env>'. Run list_deployments first.", "details": ""}`.
2. If `confirm` is False: return `{"preview": True, "message": "Will destroy <project>-<env> (bucket: ..., site: ..., owner: ...). Reply 'yes destroy it' to proceed.", "deployment": {<full deployment dict>}}`. **Important:** preview returns NO `summary` field — that field's contract is "this call failed." The agent recognizes `preview: True` as a separate shape and surfaces `message` to the user.
3. If `confirm` is True:
   1. `tofu init -input=false` (idempotent; needed on fresh checkouts).
   2. `tofu workspace select <project>-<env>`. If the workspace doesn't exist, return a `{"summary": "Workspace already gone — nothing to destroy.", "details": "..."}` (treat as success-shaped, idempotent destroy).
   3. `tofu destroy -auto-approve -input=false -var=project_name=<...> -var=env=<...>`. Other variables fall back to defaults.
   4. On success: clear `session.deployment` if it matches `(project_name, env)` so the local session reflects reality. (For older sessions whose deployment matched, the next `list_deployments` call will drop them via the workspace intersection.) Return `{"destroyed": True, "project_name": project_name, "env": env}`.
   5. On failure: return `_classify_error(stderr)`.

The `agent.py` success-cache guard already excludes `destroy_infrastructure` (it only caches deploys), no changes needed there. But we DO need to handle the post-destroy session update — the cleanest place is the agent loop's existing post-tool block: when `block.name == "destroy_infrastructure"` and `result.get("destroyed") is True`, and `session.deployment` matches by `project_name`/`env`, clear `session.deployment`. This stays in `agent.py`, parallel to the deploy cache.

### CLI symmetry

Add `make destroy-list` in `deploy-agent/Makefile` calling a new `scripts/list_deployments.py` that prints the same data as the tool (sessions.db + tofu workspace intersection). This shares no code with the tool to keep coupling low; both are thin readers of the same SQLite + `tofu workspace list`. ~30 lines.

`scripts/destroy_all.py` already exists; no changes needed.

`infra/Makefile` already has `make destroy PROJECT=X ENV=Y`; no changes needed.

## System prompt changes

Add three bullets under `Rules:` in `agent.py`:

```
- If files are uploaded but no `index.html` is present, ask the user which file should be the homepage instead of guessing — only auto-select when there is exactly one HTML file.
- To destroy a deployment, always pass the exact `project_name` and `env`. If the user says "destroy this" / "destroy it", use the values from `session.deployment`. If they name a different site, call `list_deployments` first and ask the user to pick — never fuzzy-match.
- Destroy is two-phase: call `destroy_infrastructure` with `confirm=False` first. The result will have `preview: True` and a `message` field — relay that message verbatim to the user and ask them to confirm. Only call again with `confirm=True` after the user explicitly confirms in their next reply.
```

Update the workflow numbered list to mention destroy as a possible user request alongside deploy. Keep the deploy workflow steps unchanged.

## Tests

In `deploy-agent/tests/test_tools.py`, add:

| Test | Coverage |
|---|---|
| `test_preflight_empty_upload` | empty `tmp_path` → empty-upload summary, no aux value |
| `test_preflight_jsx_with_no_html` | `App.jsx` + `index.css` → source-code summary; details lists files |
| `test_preflight_single_non_index_html` | `home.html` + `style.css` → `(None, "home.html")` |
| `test_preflight_multiple_html_no_index` | `home.html` + `about.html` → multi-html summary |
| `test_preflight_index_html_present` | `index.html` + `style.css` → `(None, None)` |
| `test_deploy_short_circuits_on_preflight_fail` | empty upload_dir → `subprocess.run` never called, summary returned |
| `test_deploy_passes_index_document_var` | preflight returns `(None, "home.html")` → tofu apply CLI args contain `-var=index_document=home.html` |
| `test_list_deployments_filters_to_active` | sessions.db has 3 deployed sessions; `tofu workspace list` mock returns 2 of them → `list_deployments` returns those 2 |
| `test_list_deployments_empty` | sessions.db empty → returns `{"deployments": []}` |
| `test_destroy_preview_returns_preview_shape` | sessions.db has matching row, `confirm=False` → returns `{preview: True, message: ..., deployment: {...}}`; no `summary` field; no subprocess call |
| `test_destroy_unknown_project_returns_summary` | no matching row → returns "No record of …" summary |
| `test_destroy_workspace_already_gone_is_idempotent` | tofu workspace select fails → returns workspace-already-gone summary, NOT an error |
| `test_destroy_confirm_runs_destroy_and_clears_session` | matching row, `confirm=True`, mocked subprocess success → tofu destroy invoked with right `-var=` flags; if `session.deployment` matches, it's cleared |
| `test_destroy_confirm_failure_returns_classified_summary` | tofu destroy returns `AccessDenied` stderr → classified summary surface |

That's 6 preflight tests + 8 destroy tests = 14 new tests.

## Build sequence

Single small commit on top of `main` is doable but risky to review. Suggested splits:

1. **Commit 1: Preflight.** `_preflight_uploads` helper, `index_document` parameter on `deploy_infrastructure`, system-prompt bullet, 7 preflight tests.
2. **Commit 2: list_deployments tool.** New tool, `scripts/list_deployments.py`, `make destroy-list`, 2 list tests.
3. **Commit 3: destroy_infrastructure tool.** New tool with two-phase confirm, agent-loop post-destroy session-clear, system-prompt bullets, 5 destroy tests.

Each step ends with `make check` green.

## Architecture impact

- `deploy-agent/tools.py`: ~150 LOC added. File grows from ~230 to ~380 LOC. Approaching the boundary where a split (e.g., `tools/preflight.py`, `tools/destroy.py`) would help, but we stay flat for now to avoid restructuring without a forcing function.
- `deploy-agent/agent.py`: 4 new system-prompt bullets, 1 new post-tool branch (post-destroy session clear). ~10 LOC.
- `deploy-agent/tests/test_tools.py`: 14 new tests.
- `deploy-agent/Makefile`: 1 new target (`destroy-list`).
- `scripts/list_deployments.py`: new, ~30 LOC.
- No changes to `app.py`, `sessions.py`, `infra/`, CI, README (CLAUDE.md gets a note about destroy after the fact).
