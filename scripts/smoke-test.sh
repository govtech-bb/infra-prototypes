#!/usr/bin/env bash
# Full deploy → upload → verify → destroy cycle against a real AWS account.
# Requires: ANTHROPIC_API_KEY, AWS credentials (AWS_PROFILE or AWS_ACCESS_KEY_ID).
# Cost: ~$0.01 per run. Cleanup runs even on failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="smoke-$(date +%s)"
ENV="smoke"
PORT=8765
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  echo "→ Cleanup: destroying smoke deployment ($PROJECT-$ENV)..."
  ( cd "$REPO_ROOT/infra/stacks/static-website" \
      && tofu workspace select "$PROJECT-$ENV" 2>/dev/null \
      && tofu destroy -auto-approve -input=false \
           -var="project_name=$PROJECT" -var="env=$ENV" ) || true
}
trap cleanup EXIT

require() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 2; }; }
require curl
require tofu
require python3

[ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "ANTHROPIC_API_KEY required"; exit 2; }

echo "→ Starting agent server on :$PORT..."
cd "$REPO_ROOT/deploy-agent"
DEPLOY_AGENT_DB="$(mktemp -d)/smoke-sessions.db" \
  uvicorn app:app --host 127.0.0.1 --port "$PORT" --log-level warning &
SERVER_PID=$!

# Wait for server.
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && break
  sleep 1
done
curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null || { echo "server did not start"; exit 3; }

echo "→ Creating session..."
SESSION=$(curl -sf "http://127.0.0.1:$PORT/api/session" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")

echo "→ Uploading sample-site..."
cd "$REPO_ROOT/examples/sample-site"
curl -sf \
  -F "files=@index.html;filename=index.html" \
  -F "files=@style.css;filename=style.css" \
  -F "files=@assets/logo.svg;filename=assets/logo.svg" \
  "http://127.0.0.1:$PORT/api/upload/$SESSION" >/dev/null

echo "→ Asking agent to deploy (project=$PROJECT)..."
MSG="Deploy this site. site_title=Smoke Test, owner_name=Smoke Bot, owner_email=smoke@example.com, is_spa=false. Use project_name=$PROJECT and env=$ENV."
RESPONSE=$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json,sys;print(json.dumps({'session_id':sys.argv[1],'message':sys.argv[2]}))" "$SESSION" "$MSG")" \
  "http://127.0.0.1:$PORT/api/chat")

SITE_URL=$(echo "$RESPONSE" | python3 -c "import sys,json;d=json.load(sys.stdin).get('deployment') or {};print(d.get('site_url') or '')")
[ -n "$SITE_URL" ] || { echo "agent did not deploy. Response: $RESPONSE"; exit 4; }
echo "→ Site URL: $SITE_URL"

echo "→ Waiting for CloudFront propagation (up to 5 min)..."
for _ in $(seq 1 60); do
  BODY=$(curl -sf -L "$SITE_URL" || true)
  if echo "$BODY" | grep -q "deployed-via-infra-deploy-agent"; then
    echo "✓ Smoke test passed."
    exit 0
  fi
  sleep 5
done

echo "✗ Marker string not found in deployed site after 5 min."
exit 5
