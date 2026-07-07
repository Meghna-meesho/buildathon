#!/usr/bin/env bash
# Insight Lens — one-command launcher.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi

echo "Installing dependencies…"
./.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

PORT="${PORT:-8000}"
if [ -n "$ANTHROPIC_API_KEY" ]; then
  echo "✓ ANTHROPIC_API_KEY detected — AI root-cause narratives enabled (model: ${ANTHROPIC_MODEL:-claude-opus-4-8})."
else
  echo "• No ANTHROPIC_API_KEY set — running with statistical insights (app still fully works)."
fi
echo ""
echo "➜  Open  http://localhost:${PORT}"
echo ""

cd backend
exec ../.venv/bin/uvicorn main:app --host 0.0.0.0 --port "${PORT}"
