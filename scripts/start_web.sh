#!/usr/bin/env bash
# Required even where Render Shell/pre-deploy commands are unavailable.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
flask --app wsgi migrate-refresh
exec gunicorn wsgi:app --bind "0.0.0.0:${PORT:?PORT must be configured}" --workers 2 --timeout 60 --access-logfile -
