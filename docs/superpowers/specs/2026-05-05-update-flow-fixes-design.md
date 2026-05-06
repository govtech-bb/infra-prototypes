# Update Flow Fixes — Design

**Date:** 2026-05-05
**Scope:** Three bundled fixes that surfaced from a real chat-driven update attempt. No new features — these unblock an already-supported flow.

## Problems

From an actual chat session attempting to update `multi-mda-agri-proto`:

1. **`list_deployments` doesn't expose `bucket_name` or `cloudfront_distribution_id`.** When the user said "update multi-mda-agri", the agent had to guess the bucket name (constructed `<project>-<env>` and got `multi-mda-agri-proto`, but the real format is `<project>-<env>-static`) and then ask the user for the CloudFront distribution ID. Bad UX, and impossible without the user knowing the IDs.

2. **No "update" workflow in the system prompt.** The agent improvised the right intent ("look up the deployment, then upload") but had no playbook.

3. **`[Uploaded files: …]` is injected exactly once per session.** Re-uploading mid-chat doesn't re-announce the new file. After the user renamed `index (1).html` → `index.html` locally and re-uploaded, the agent told them "no file came through" because the chat context still showed only the first upload.

4. **Browser-duplicate-download filenames slip through preflight.** `index (1).html` was happily auto-selected as the homepage and deployed — technically working but obviously not what the user wanted.

## Goals

- The agent can update an existing deployment using just the user's natural-language request — no guessing, no follow-up "what's the bucket name?" turns.
- Mid-chat re-uploads are visible to the agent immediately on the next user message.
- Preflight catches `index (N).html` patterns and asks the user to rename.

## Non-goals

- Auto-renaming files server-side. Too magical; the user's local filename is still wrong.
- Server-side build automation (`npm run build`).
- A new "update_deployment" tool. The existing `upload_files` tool already does the work; the agent just needs the right inputs.

## Architecture

### Fix 1 — `list_deployments` includes bucket + CF distribution

In `deploy-agent/tools.py:_read_active_deployments`, expand each returned dict:

```python
deployments.append({
    "project_name": r["project_name"],
    "env":          r["env"],
    "site_title":   dep.get("site_title", ""),
    "owner_name":   dep.get("owner_name", ""),
    "site_url":     dep.get("site_url", ""),
    "bucket_name":  dep.get("bucket_name", ""),
    "cloudfront_distribution_id": dep.get("cloudfront_distribution_id", ""),
    "updated_at":   r["updated_at"],
})
```

Also update `scripts/list_deployments.py` to print bucket/CF columns (or at least the bucket — CF distribution IDs are 14 chars and noisy in a terminal table; can hide it behind a `--verbose` flag or just append).

### Fix 2 — Re-inject file list when new files arrive

**Schema change.** Add a column to the `sessions` table:

```sql
ALTER TABLE sessions ADD COLUMN last_injected_file_count INTEGER DEFAULT 0;
```

`SqliteSessionStore.__init__` runs the ALTER defensively (catches `sqlite3.OperationalError: duplicate column name` for existing DBs that already have it).

**`Session` dataclass:**

```python
@dataclass
class Session:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    upload_dir: str | None = None
    deployment: dict | None = None
    last_injected_file_count: int = 0  # NEW
```

`SqliteSessionStore.get` / `save` read/write the new column.

**`app.py` chat handler:**

```python
last_announced = session.last_injected_file_count
current_count = len(session.files)
user_content = req.message
if current_count > last_announced:
    new_files = session.files[last_announced:]
    file_list = ", ".join(new_files)
    if last_announced == 0:
        user_content = f"{req.message}\n\n[Uploaded files: {file_list}]"
    else:
        user_content = f"{req.message}\n\n[Newly uploaded: {file_list}]"
    session.last_injected_file_count = current_count
```

The first batch keeps the existing `[Uploaded files: …]` format (backward-compat with the agent's training cue). Subsequent batches use `[Newly uploaded: …]`.

### Fix 3 — Preflight catches `index (N).html` pattern

In `deploy-agent/tools.py:_preflight_uploads`, after the empty-upload check and before the source-only check, add:

```python
DUPLICATE_DOWNLOAD = re.compile(r"^index\s*\(\d+\)\.html?$", re.IGNORECASE)

# (in the function body, after `files` is computed)
duplicate_index = next(
    (p for p in files if DUPLICATE_DOWNLOAD.match(p.name)),
    None,
)
if duplicate_index is not None:
    return (
        {
            "summary": (
                f"Your homepage is named '{duplicate_index.name}' — looks like a "
                "browser-duplicate download. Rename it to 'index.html' and upload again."
            ),
            "details": "",
        },
        None,
    )
```

The regex matches `index (1).html`, `Index (2).HTML`, `index(1).htm`, `index   (3).html`, etc.

If the user truly wants this filename, they can pass `index_document="index (1).html"` explicitly via the agent's tool call — but the agent's system prompt already discourages guessing.

### Fix 4 — System prompt: "update" workflow

In `agent.py:SYSTEM_PROMPT`, after the destroy workflow section and before `Rules:`, add:

```
When the user wants to **update** an existing deployment (push new files to a site that's already live):
1. Identify which deployment by name. If they say "this" or "the one I just deployed", use `session.deployment`. Otherwise, call `list_deployments` and ask them to pick.
2. From the matched record, take the `bucket_name` and `cloudfront_distribution_id` — never guess them.
3. If they haven't uploaded files yet, ask them to upload.
4. Once files are uploaded (you'll see `[Newly uploaded: ...]` in their next message), call `upload_files` with the bucket + distribution from step 2.
5. Report success with the live URL. CloudFront cache invalidation already runs as part of upload_files.
```

And one new Rules bullet:

```
- For updates, use upload_files directly with the existing bucket_name + cloudfront_distribution_id from list_deployments. Don't call deploy_infrastructure again — the infra is already there.
```

## Testing

In `deploy-agent/tests/test_tools.py`:

- `test_list_deployments_returns_bucket_and_cf` — seeded session with deployment containing both fields; assert they're in the returned dicts.
- `test_preflight_rejects_duplicate_index` — upload contains `index (1).html`; preflight returns the rename-and-reupload summary.
- `test_preflight_rejects_duplicate_index_case_variants` — `Index (2).HTML`, `index(1).htm` all caught.

In `deploy-agent/tests/test_app.py`:

- `test_chat_injects_uploaded_files_on_first_turn` — fresh session, upload then chat; assistant message history contains `[Uploaded files: ...]`.
- `test_chat_re_injects_newly_uploaded_files` — first upload + chat (already_injected), second upload, second chat; assistant sees `[Newly uploaded: <2nd batch>]`. NO repetition of the first batch.
- `test_chat_no_injection_when_files_unchanged` — already-injected, then a second chat with no new uploads; user message is plain (no `[Uploaded` or `[Newly` suffix).

In `deploy-agent/tests/test_sessions.py`:

- `test_last_injected_file_count_round_trips` — set, save, get, assert preserved.
- `test_alter_table_idempotent_on_existing_db` — open store on a DB that already has the column; no error.

## Build sequence

Single commit on top of `main`:

1. Schema migration in `SqliteSessionStore` (defensive ALTER).
2. `Session` dataclass field.
3. `SqliteSessionStore.get`/`save` round-trip.
4. `app.py` chat handler: re-injection logic.
5. `tools.py` `_read_active_deployments`: include bucket + CF.
6. `tools.py` preflight: duplicate-download detection.
7. `agent.py` SYSTEM_PROMPT: update workflow + Rules bullet.
8. Tests (5 new in test_tools/app/sessions).
9. `make check` green → commit + push.

`scripts/list_deployments.py` gets a small bucket column added to the table for symmetry, but no `--verbose` flag (keep it simple).

## Out of scope (explicit)

- Server-side build automation.
- "Pre-deploy diff" (show the user what files will change before upload).
- Concurrent updates to the same session (still single-tenant per session).
- Removing files from S3 that aren't in the upload (no `aws s3 sync --delete` semantics — current `upload_files` is additive).
