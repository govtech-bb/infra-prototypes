#!/usr/bin/env bash
# Start the INFRA Deploy Agent
# Usage: ./run.sh

set -e

cd "$(dirname "$0")"

# Check for required env vars
if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "❌  ANTHROPIC_API_KEY is not set."
  echo "    Export it: export ANTHROPIC_API_KEY=sk-ant-..."
  exit 1
fi

if [ -z "$AWS_PROFILE" ] && [ -z "$AWS_ACCESS_KEY_ID" ]; then
  echo "⚠️  No AWS credentials detected (AWS_PROFILE or AWS_ACCESS_KEY_ID)."
  echo "   Deployments will fail without valid AWS credentials."
fi

# Install deps if needed
if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "📦 Installing dependencies..."
  pip3 install -r requirements.txt --quiet
fi

echo ""
echo "⬡  INFRA Deploy Agent"
echo "   Open http://localhost:8000 in your browser"
echo ""

uvicorn app:app --host 0.0.0.0 --port 8000 --reload
