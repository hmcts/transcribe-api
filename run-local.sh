#!/usr/bin/env bash
# Start the merged backend locally. Requires docker-compose services up.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m uvicorn transcribe_api.api.app:create_app --factory \
  --host 127.0.0.1 --port 8000 "$@"
