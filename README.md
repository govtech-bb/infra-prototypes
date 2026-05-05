# INFRA Deploy Agent

Chat with an AI agent to deploy a static website to AWS (S3 + CloudFront). Drag a folder of HTML/CSS/JS into the chat, answer a few questions, and the agent provisions the infrastructure with OpenTofu and uploads your files. Friend-of-the-engineer level prototype.

## Quickstart

```bash
git clone https://github.com/<you>/infra-prototypes.git
cd infra-prototypes/deploy-agent
make install
export ANTHROPIC_API_KEY=sk-ant-...
export AWS_PROFILE=...        # or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
./run.sh
# open http://localhost:8000, drag examples/sample-site/ into the chat
```

The agent will collect a site title, owner name, and email, then call `deploy_infrastructure` (provisions S3 + CloudFront) and `upload_files` (syncs your folder + invalidates the CDN cache). It returns the live URL.

## What it costs

CloudFront has no idle cost beyond the distribution fee (~$0.50/month). Egress is the bigger lever — for a low-traffic prototype, expect under $1/month total. To clean up:

```bash
cd infra && make destroy PROJECT=<your-project> ENV=<your-env>
# or to wipe every deployment recorded in the local session DB:
cd deploy-agent && make destroy-all
```

## Architecture

```
┌──────────────┐    /api/chat      ┌──────────────────┐
│  Static UI   │ ─────────────────▶│  FastAPI agent   │
│ static/      │                   │  app.py          │
└──────────────┘                   │  agent.py        │
                                   │  sessions.py     │
                                   │  tools.py        │
                                   └────────┬─────────┘
                                            │ tofu / boto3
                                   ┌────────▼─────────┐
                                   │  infra/          │
                                   │  modules + stack │
                                   └────────┬─────────┘
                                            │
                                   ┌────────▼─────────┐
                                   │  AWS: S3 + CF    │
                                   └──────────────────┘
```

The agent loop in `agent.py` runs Claude with two tools (`deploy_infrastructure`, `upload_files`), executes them via subprocess (`tofu`) and `boto3`, and feeds the structured results back to Claude until it produces a final text response. Sessions persist in `deploy-agent/data/sessions.db`.

## Verifying it works

`./scripts/smoke-test.sh` runs a full deploy → upload → assert-content → destroy cycle against your AWS account. ~$0.01 per run. Cleanup runs on failure.

## Project layout

```
deploy-agent/
  app.py            FastAPI route handlers
  agent.py          Claude tool-use loop + system prompt
  sessions.py       SQLite-backed session store
  tools.py          deploy_infrastructure, upload_files
  static/           Chat UI
  tests/            pytest suite (mocked, no AWS calls)
infra/
  modules/          Reusable s3-static-site, cloudfront modules
  stacks/           static-website stack composition
examples/
  sample-site/      Drag-in example, also fixture for smoke test
scripts/
  smoke-test.sh     Real-AWS smoke test
  destroy_all.py    Tears down every deployment in the session DB
docs/
  superpowers/      Design docs and implementation plans
```

## Limitations

- **Local OpenTofu state.** Fine for one developer. Multi-user requires migrating to S3+DynamoDB backend (`infra/stacks/static-website/backend.tf` has the migration block ready).
- **One stack type.** Hardcoded to `static-website`. Future stacks (Lambda, ECS) require a `stack_name` parameter on `deploy_infrastructure`.
- **No auth on the FastAPI server.** Bind to `127.0.0.1` if running on a shared machine.
- **Single-tenant.** Concurrent requests against the same session aren't safe; design assumes one user, one tab.

## Contributing

```bash
cd deploy-agent
make install-dev
make check     # ruff + pytest + tofu validate
```

CI runs the same on every push and PR.

## License

MIT — add a `LICENSE` file before publishing.
