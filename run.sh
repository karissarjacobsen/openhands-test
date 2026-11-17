#!/usr/bin/env bash
# Start the Agreement Manager app.
set -euo pipefail
cd "$(dirname "$0")"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT:-12000}" "$@"
