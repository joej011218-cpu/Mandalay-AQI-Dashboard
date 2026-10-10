#!/usr/bin/env bash
# Development only. Uses retained local PostgreSQL; managed DATABASE_URL takes precedence.
set -euo pipefail
cd /workspace/Mandalay-AQI-Dashboard
export PATH="/workspace/mandalay-venv/bin:$PATH"
if [ -z "${DATABASE_URL:-}" ]; then
  pg_bin=/workspace/mandalay-postgres/usr/lib/postgresql/17/bin
  export LD_LIBRARY_PATH="/workspace/mandalay-postgres/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  if ! "$pg_bin/pg_ctl" -D /workspace/mandalay-postgres/data status >/dev/null 2>&1; then
    "$pg_bin/pg_ctl" -D /workspace/mandalay-postgres/data -l /workspace/mandalay-postgres/server.log -o '-h 127.0.0.1 -p 55432 -k /tmp' start
  fi
  export DATABASE_URL='postgresql://agent@127.0.0.1:55432/mandalay_aqi'
fi
flask --app wsgi init-db
flask --app wsgi recalculate-cpcb
if ! flask --app wsgi refresh-data; then
  echo "Provider refresh failed; serving stored Mandalay history with freshness status." >&2
fi
exec gunicorn wsgi:app --bind "127.0.0.1:${DASHBOARD_PORT:-5000}" --workers 2 --timeout 60
