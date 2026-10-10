# Hourly refresh with cron-job.org

This change updates the existing POST /api/refresh route. It calls the same refresh() used by `flask --app wsgi refresh-data`: existing Open-Meteo collection, validation, PostgreSQL upsert and CPCB rebuild. No schema, CPCB formula or UI changes. Do not create a new database.

After reviewing and approving this PR, deploy the dashboard change. Set REFRESH_SECRET in the existing dashboard's Render environment to a long randomly generated secret (for example, generate locally with Python secrets.token_urlsafe(32)). Keep DATABASE_URL unchanged. Never put either value in Git, a URL, browser code, screenshots or logs. REFRESH_TOKEN and Authorization bearer authentication no longer enable this route.

In cron-job.org create a job:

- URL: https://mandalay-aqi-dashboard-bk49.onrender.com/api/refresh
- Schedule: every hour; optionally minute 35 to match the previous Myanmar refresh timing. Check the account/job timezone when selecting the schedule.
- HTTP method: POST (advanced request settings).
- Custom header name: X-Refresh-Secret. Value: the same secret configured as REFRESH_SECRET in Render.
- Request body: empty. No login/basic-auth configuration is required.
- Enable failure notifications and inspect execution history after the first scheduled run. Do not paste the header value into a job title or description.

cron-job.org supports POST and custom headers: https://cron-job.org/en/faq/ . Review its execution timeout before enabling: a synchronous request includes provider fetch and historical CPCB recalculation; a timeout may occur while server work continues. Inspect execution results and dashboard timestamps before retrying.

Responses: 200 JSON status=success with imported count and metadata; 401 missing/wrong header; 503 server secret absent; 409 refresh already running in this process; 502 provider error or invalid data; 500 other refresh failure. GET returns 405. Secrets and exception details are never returned or logged by this route.

The HTTP and CLI commands share a nonblocking per-process lock, always released after success/failure. Different workers or services can still fetch concurrently; the existing PostgreSQL transaction advisory lock serializes database updates. This is not a distributed whole-operation lock. Use one hourly scheduler and avoid overlapping manual triggers. If replacing an existing Render cron, disable that duplicate only after approval and verification of successful cron-job.org runs.

Verification: confirm successful scheduled response and advancing /api/latest timestamp/CPCB values, then confirm the prediction application's next five-minute poll sees the new valid origin. This task does not trigger a live refresh, modify database records, create a cron-job.org job, or deploy.
