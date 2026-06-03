#!/usr/bin/env bash
# End-to-end smoke test for aibuilder.
#
# Starts uvicorn, hits the chat endpoint with a known public repo URL,
# verifies the agent walks through clone → analyze → recommend → estimate
# and returns a cost figure. Idempotent — cleans up uvicorn on exit.
#
# Requires: ANTHROPIC_API_KEY in the environment. Does NOT need AWS creds.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "❌ ANTHROPIC_API_KEY is not set" >&2
  exit 1
fi

# Use a separate DB so the smoke test doesn't pollute the dev session.
SMOKE_DB="$(mktemp).db"
export AIBUILDER_DB="$SMOKE_DB"

cleanup() {
  if [ -n "${UVICORN_PID:-}" ]; then
    kill "$UVICORN_PID" 2>/dev/null || true
  fi
  rm -f "$SMOKE_DB"
}
trap cleanup EXIT

echo "▶ Starting uvicorn..."
python3 -m uvicorn app:app --host 127.0.0.1 --port 8765 >/tmp/aibuilder-smoke.log 2>&1 &
UVICORN_PID=$!

# Wait for the server to come up.
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8765/api/health >/dev/null; then
    break
  fi
  sleep 0.5
done

echo "▶ Opening a session..."
SESSION_ID=$(curl -sf http://127.0.0.1:8765/api/session | python3 -c "import json,sys;print(json.load(sys.stdin)['session_id'])")
echo "   session_id=$SESSION_ID"

# octocat/Hello-World: tiny, stable, public, present forever.
URL="https://github.com/octocat/Hello-World"

echo "▶ Asking the agent to analyze $URL ..."
RESPONSE=$(curl -sf -X POST http://127.0.0.1:8765/api/chat \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION_ID\",\"message\":\"Analyze $URL and tell me what AWS services I'd need and the monthly cost. Confirm and recommend in one go.\"}")
echo "$RESPONSE" | python3 -m json.tool

# Continue the conversation so the agent makes the full chain of tool calls.
echo "▶ Confirming so the agent proceeds to recommendation + cost..."
RESPONSE=$(curl -sf -X POST http://127.0.0.1:8765/api/chat \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION_ID\",\"message\":\"Yes that's right, proceed.\"}")
echo "$RESPONSE" | python3 -m json.tool

# Loose assertion: the final message should mention a dollar amount or "month".
if echo "$RESPONSE" | grep -qi -e '\$' -e 'month'; then
  echo "✅ Smoke test passed — agent produced a cost estimate."
else
  echo "❌ Smoke test failed — agent response did not contain cost info." >&2
  exit 1
fi
