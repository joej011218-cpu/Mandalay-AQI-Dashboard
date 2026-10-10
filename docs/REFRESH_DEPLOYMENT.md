# Deploying asynchronous refresh to the existing Mandalay dashboard

Deployment files belong to `feature/mandalay-hardware-integration` in this
dashboard repository. No merge or Render deployment was performed. GitHub
publication does not establish successful Render deployment.

## Local verification

The complete suite passed: **142 passed, zero failures/skips** using disposable
PostgreSQL. New deployment tests exercise real schema-lock contention, idempotence,
record preservation, transactional DDL rollback, lock release, and the actual
startup script's ordering and failure gate. Existing authentication, async-job,
CPCB, hardware, prediction-link and isolation tests remain passing.

The startup script was also run with real Flask, PostgreSQL, and two Gunicorn
workers. Health/latest/analytics/alerts returned HTTP 200. Browser smoke checks
passed for pages, charts, filters, dark mode/mobile, and missing hardware. Repeated
migrations preserved all local table counts, including 6,961 original readings,
6,940 source observations and 6,772 CPCB results. This is local evidence, not a
claim that production Render migration or broker connectivity was tested.

## Render capabilities and current-service verification

The checked-in Blueprint declares a **Starter** web service. Paid Render web
services support Shell and pre-deploy commands; free web services do not provide
those facilities. Reference: [Render Shell](https://render.com/docs/shell) and
[pre-deploy commands](https://render.com/docs/deploys#pre-deploy-command).
The live service plan/settings were **not verified remotely**: no Render service
connection is available here, and documentation access is blocked by the cloud
network proxy. In Render, open the existing Mandalay dashboard service and check
its instance plan, Settings, and Shell tab. The repository's `plan: starter` does
not prove the running service uses that plan.

On a service with Shell, the command is supported by the application:

```sh
flask --app wsgi migrate-refresh
```

Run it from the application's deployed root after this code is present. Shell
on an older release cannot run a CLI command that release does not contain.
Shell is optional: deployment startup performs the same migration.

## Migration strategy

`migrate-refresh` creates only `mandalay_refresh_state` when missing, without
inserting, updating, or deleting existing observations or CPCB results. It
checks required columns are readable. It does not silently repair an incompatible
existing table; investigate such a failure before deploying again.

The migration and `init-db` share PostgreSQL **transaction advisory lock
219588960894**. Checking and creating the table happen in the same transaction
under this lock. Concurrent web starts, CLI migrations, and scheduled jobs wait
rather than race on `CREATE TABLE`. Transaction completion, rollback, or process
death releases this lock. Migration lock waits are bounded to 30 seconds and SQL
statements to 60 seconds. Failure rolls back transactional DDL and stops startup.
Database credentials need schema CREATE permission and access to the status table.

`scripts/start_web.sh` migrates first, then uses `exec` to start the existing
two-worker Gunicorn configuration. Gunicorn does not bind its port before a
successful migration. Every startup checks the schema; the create operation has
a one-time effect, so no fragile “migration already ran” environment flag is
needed. The cron command similarly migrates before running `refresh-data`.

This gate applies to the configured startup command. Directly invoking Gunicorn
bypasses it. Keep Render's Start Command set to the script below. Existing old
instances can continue serving during Render's deployment rollout; the new code
cannot receive HTTP requests until its migration succeeds.

## Exact deployment steps

1. In Render, disable automatic deploys while reviewing these changes. Verify
   this is the **existing Mandalay dashboard**, its existing PostgreSQL database,
   and the hardware integration branch. Do not connect or change the Prediction
   or Vadodara services. Take a database backup/snapshot and verify it can be
   restored. Preserve the current `DATABASE_URL`, `REFRESH_TOKEN`, prediction URL,
   device settings, and MQTT service credentials. No new environment variables
   are required. Do not regenerate the token from the Blueprint.
2. Set the existing
   web service commands (Blueprint values do not automatically update a manually
   configured service):

   **Branch:** `feature/mandalay-hardware-integration`

   **Root Directory:** leave blank (repository root). `app.py`, `wsgi.py`,
   `requirements.lock.txt`, and `scripts/` are directly in the Git repository;
   `Mandalay-AQI-Dashboard` is the local checkout name, not a nested repository
   directory. Do not use `scripts` as the Root Directory. If the service is
   attached to another branch, explicitly select this published feature branch.

   The failed deployment's startup script had been local/untracked and absent
   from the GitHub branches available during inspection. Ensure the deployed
   commit includes `scripts/start_web.sh`, `refresh_jobs.py`, and the matching
   `app.py`; changing the Start Command alone cannot publish these files.

   **Build Command**
   ```sh
   pip install -r requirements.lock.txt
   ```
   **Start Command — required, including without Shell**
   ```sh
   bash scripts/start_web.sh
   ```
   **Pre-Deploy Command — where supported**
   ```sh
   flask --app wsgi migrate-refresh && flask --app wsgi init-db && flask --app wsgi recalculate-cpcb && flask --app wsgi migrate-hardware
   ```
   The latter preserves existing initialization/recalculation steps and adds the
   explicit refresh migration. For an existing service needing only this schema
   upgrade, `flask --app wsgi migrate-refresh` alone is sufficient as the
   Pre-Deploy Command. Do not change the build command into a database migration:
   builds should not need production database access.
3. Keep `/api/health` as the Health Check Path. Render supplies `PORT`. If there
   is no Pre-Deploy Command or Shell, the Start Command still performs the safe
   migration before the application is usable. This fallback assumes the existing
   dashboard's base and hardware schemas already exist; it creates only the new
   refresh table. A first-ever database needs the documented full initialization.
4. Update the **existing hourly dashboard cron** Start Command to:
   ```sh
   flask --app wsgi migrate-refresh && flask --app wsgi refresh-data
   ```
   Preserve its `DATABASE_URL` and `5 * * * *` schedule. The migration can safely
   run before or alongside a web deploy. The unchanged MQTT worker command remains
   `python mqtt_worker.py`; this release needs no MQTT/firmware update.
5. When you choose to deploy, inspect the release/start logs for
   `Additive Mandalay refresh status table is ready` before Gunicorn starts.
   If migration fails, resolve database access/schema/lock errors and redeploy;
   do not bypass the script. Verify `/api/health` returns HTTP 200 and compare
   historical data, Live, Analytics, Alerts, and the external Prediction link.
6. In a trusted client or cron-job.org, POST to `/api/refresh`, with an empty
   body and the private header `Authorization: Bearer <existing REFRESH_TOKEN>`.
   Expect an **empty HTTP 202**, not a completion response. Missing/wrong tokens
   still produce 401; no configured token still disables refresh with 503.
   GET `/api/refresh/status` with the same header. Check the returned job ID when
   provided, then poll for `data.status: succeeded`, `finished_at`, and
   `imported_rows`. Investigate `failed` or `interrupted`; 202 alone is insufficient.

## Remaining risks

The schema migration is transactional and repeatable, but a malformed pre-existing
status table requires manual investigation. It validates column readability,
not every possible manual schema alteration. A long competing migration or
insufficient privileges fails deployment rather than starting without the table.
Keep deployment credentials able to perform this small startup schema check.

The job **status** is durable PostgreSQL state; the daemon-thread job is not a
durable queue. Render restarts/redeploys and Gunicorn worker termination can stop
an accepted refresh. A status check detects an abandoned queued/running job as
`interrupted`; a later POST or scheduled CLI can retry. A crash after data commit
but before status update can report interruption even though data was imported.
Connection loss can make coordination/status uncertain. There are no automatic
retries, exactly-once execution, or guaranteed completion. Monitor status and
the scheduled CLI; use a durable queue if stronger guarantees are needed.

Pre-deploy retains the existing full CPCB recalculation, which can take time as
history grows. The new migration itself does not rebuild AQI or change its
calculation. No historical workbook import or synthetic sensor insertion is part
of this deployment. Never run test fixtures against production: they intentionally
recreate tables in a disposable test database.
