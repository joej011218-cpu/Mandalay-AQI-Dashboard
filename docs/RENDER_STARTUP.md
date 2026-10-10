# Render startup file fix on main

Render is configured to deploy **main** with **Root Directory blank**. The prior
startup script was published only to the hardware feature branch, so Render's
main checkout could not find it. This fix publishes the script and the required
refresh backend/migration to main without merging hardware/design changes.

Use the existing Render service and database, with these settings:

| Setting | Value |
| --- | --- |
| Branch | `main` |
| Root Directory | Leave blank (Git repository root) |
| Build Command | `pip install -r requirements.lock.txt` |
| Start Command | `bash scripts/start_web.sh` |
| Health Check Path | `/api/health` |

`scripts/`, `wsgi.py`, and `requirements.lock.txt` are at the repository root.
The local checkout directory name is not a nested Git directory. Do not set
Root Directory to `Mandalay-AQI-Dashboard` or `scripts`.

The executable script resolves its repository directory, runs
`flask --app wsgi migrate-refresh`, then execs the existing two-worker Gunicorn
configuration on Render's `PORT`. A failed migration stops startup before the
HTTP listener opens. Repeated migrations are safe: they only create the missing
`mandalay_refresh_state` table. PostgreSQL transactional DDL and advisory lock
219588960894 serialize migration and init-db, with a 30-second lock timeout and
60-second statement timeout. Existing measurements and CPCB results are not
updated or deleted by the migration.

No environment values change. Preserve the existing `DATABASE_URL` and
`REFRESH_TOKEN`. The refresh and status APIs require the same private
`Authorization: Bearer <token>` header with constant-time comparison. POST returns
empty HTTP 202; GET `/api/refresh/status` reports completion or failure. Status
is durable, but daemon jobs can be interrupted by process restarts; 202 does not
guarantee completion. This does not introduce a durable job queue.

The existing pre-deploy init/recalculation steps remain, with explicit
`migrate-refresh` added first. Shell/pre-deploy are optional for this upgrade:
the Start Command always migrates before serving. Keep the hourly cron command
`flask --app wsgi migrate-refresh && flask --app wsgi refresh-data`.

When deploying, select the main commit containing this script. Confirm the log
`Additive Mandalay refresh status table is ready` appears before Gunicorn starts,
then verify health and authenticated refresh status. GitHub publication and local
tests are not confirmation of a successful Render deployment.
