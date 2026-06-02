#!/usr/bin/env bash
# Start the aibuilder chat agent.
# Usage: ./run.sh

set -e
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "❌  ANTHROPIC_API_KEY is not set."
  echo "    Export it: export ANTHROPIC_API_KEY=sk-ant-..."
  exit 1
fi

VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "📦 Creating virtual environment..."
  python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if ! python3 -c "import fastapi" 2>/dev/null || ! python3 -c "import uvicorn" 2>/dev/null; then
  echo "📦 Installing dependencies..."
  pip install -r requirements.txt --quiet
fi

echo ""
echo "⬢  aibuilder"
echo "   Open http://localhost:8001 in your browser"
echo ""

python3 -m uvicorn app:app --host 0.0.0.0 --port 8001 --reload
