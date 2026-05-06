#!/usr/bin/env bash
# Start the INFRA Deploy Agent
# Usage: ./run.sh

set -e

cd "$(dirname "$0")"

# Auto-load .env if present (gitignored — see .env.example for the template).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

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

# Create / activate virtual environment
VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "📦 Creating virtual environment..."
  python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# Install deps if needed
if ! python3 -c "import fastapi" 2>/dev/null || ! python3 -c "import uvicorn" 2>/dev/null; then
  echo "📦 Installing dependencies..."
  pip install -r requirements.txt --quiet
fi

echo ""
echo "⬡  INFRA Deploy Agent"
echo "   Open http://localhost:8000 in your browser"
echo ""

python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
