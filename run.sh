#!/usr/bin/env bash
# One-command setup + start: venv, deps, product DB, server on http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "Created .env: add VALIO_API_KEY (and optionally GEMINI_API_KEY), then rerun."; exit 1; }
if command -v uv >/dev/null; then
  [ -d .venv ] || uv venv -q .venv --python 3.12
  uv pip install -q -p .venv -r requirements.txt
else
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
ls data/raw/valio_aimo_junction26_product_data.json >/dev/null 2>&1 || { echo "Put the 3 Valio data files in data/raw/ (see README)"; exit 1; }
[ -f data/valio.db ] || .venv/bin/python -m data.load_data
echo "Open http://localhost:8000  (MCP endpoint: http://localhost:8000/mcp, OpenAPI: http://localhost:8000/docs)"
exec .venv/bin/uvicorn ui.app:app --port "${PORT:-8000}"
