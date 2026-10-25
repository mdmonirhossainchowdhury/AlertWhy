#!/usr/bin/env bash
# One-command start: installs deps, generates synthetic data on first run, serves UI + API on :8000
set -e
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
python3 -m pip install -q -r requirements.txt
python3 -m uvicorn alertwhy.api:app --host 0.0.0.0 --port "${PORT:-8000}"
