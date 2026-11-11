#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: setting up Scout. This takes a few minutes..."
  python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m playwright install chromium
fi
if command -v node >/dev/null && [ ! -d wa-service/node_modules ]; then (cd wa-service && npm install --no-audit --no-fund); fi
command -v node >/dev/null || echo "NOTE: install Node.js from nodejs.org to enable WhatsApp auto-send."
echo "Starting Scout... keep this window open. Close it to stop."
# caffeinate keeps the Mac awake while Scout runs so follow-ups keep sending
caffeinate -i .venv/bin/python app.py
