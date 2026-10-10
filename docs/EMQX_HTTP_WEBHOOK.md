# EMQX HTTPS delivery to the existing Mandalay dashboard

ESP32 → existing EMQX Serverless broker → HTTP Server rule/action → existing
Flask web service → existing PostgreSQL. No new Render background worker or
cron service is required. The ESP32, topic, calibration, sampling and prediction
system are unchanged. The existing database still has its current plan/cost;
this does not promise free PostgreSQL or unlimited EMQX/Render quotas.

## GitHub and Render — manual deployment only

1. Review the feature branch `feature/mandalay-hardware-integration`. The webhook
   files must be committed/published on that branch before deployment. Do not
   merge into main automatically. Your current Render service deploys main,
   which has no hardware/webhook integration: select this feature branch manually
   when ready, or separately review a PR targeting main first. Preserve any newer
   main changes when doing a future integration.
2. Back up the existing PostgreSQL database. Turn Auto-Deploy off while preparing.
   Open the **existing Mandalay dashboard web service**; do not create a second
   database or apply a new Blueprint to provision replacement resources. Keep
   the existing database URL, region, REFRESH_TOKEN and prediction settings.
3. Configure:

   | Setting | Value |
   | --- | --- |
   | Branch | `feature/mandalay-hardware-integration` |
   | Root Directory | Blank — repository root |
   | Build Command | `pip install -r requirements.lock.txt` |
   | Start Command | `bash scripts/start_web.sh` |
   | Health Check Path | `/api/health` |
   | Instance | Existing Free web instance; no new worker |

   Remove a Pre-Deploy Command when using Free: startup runs both
   `flask --app wsgi migrate-hardware` and `flask --app wsgi migrate-refresh`
   under the shared PostgreSQL schema advisory lock before Gunicorn listens.
   These migrations are additive/repeatable, create only missing hardware/state
   and refresh tables, and do not update/delete observations or recalculate AQI.
   The existing base dashboard tables must already exist. A brand-new empty
   database requires the separately documented full initialization.

4. Set web environment values:

   ```text
   DATABASE_URL=<keep the existing PostgreSQL binding>
   HARDWARE_TRANSPORT=webhook
   HARDWARE_WEBHOOK_TOKEN=<new strong secret entered privately>
   MQTT_PUBLISH_USERNAME=mandalay_esp32
   MQTT_TOPIC_TEMPLATE=aqi/{device_id}/readings
   MANDALAY_DEVICE_IDS=node1
   HARDWARE_DEVICE_ID=node1
   HARDWARE_SAMPLE_SECONDS=900
   HARDWARE_STALE_SECONDS=1800
   PYTHON_VERSION=3.12.14
   ```

   Generate a separate random secret securely (at least 32 random bytes). Put
   the **same value** in Render and the EMQX header field below. Do not use or
   rotate REFRESH_TOKEN for hardware ingestion. Do not commit the secret, include
   it in the URL/body, place it in browser JavaScript, or log request headers.
   The web service needs no MQTT password. `HARDWARE_TRANSPORT=webhook` labels
   delivery correctly; MQTT subscriber connectivity is not claimed.

5. Manually deploy only when ready. Confirm both migration readiness messages
   before Gunicorn starts and verify `/api/health`. Stop/remove any separately
   deployed MQTT worker after switching to this route; do not run subscribers
   inside Gunicorn. `mqtt_worker.py` remains as an optional legacy transport,
   but is absent from the new Render deployment configuration.

   The Blueprint also no longer provisions a paid cron service. If you need
   scheduled Open-Meteo refresh without an additional Render cron charge, use
   your existing cron-job.org job: POST `/api/refresh`, empty body, private
   `Authorization: Bearer <existing REFRESH_TOKEN>` header. Check authenticated
   GET `/api/refresh/status` for completion. No refresh secrets go in URLs.

## EMQX HTTP Server connector

In the **existing** EMQX Cloud Serverless deployment, open Data Integration and
create an HTTP Server connector named `mandalay_dashboard_https`. Console labels
vary by Serverless release; this is not the MQTT WSS port or a broker URL.

- Server/base URL: `https://YOUR_EXISTING_DASHBOARD.onrender.com`
- Enable TLS with server certificate/hostname verification. Keep verification
  enabled and the normal trusted public CA chain. Do not use insecure mode.
- Configure headers privately on the connector/action:

  ```text
  Content-Type: application/json
  Authorization: Bearer <same private HARDWARE_WEBHOOK_TOKEN>
  ```

- If available, use connect timeout **30 seconds**, request timeout **120 seconds**,
  pool size **1**, and HTTP pipelining **1**. Do not use a connector URL containing
  the token. Do not enable a separate OAuth flow for this header-secret design.
- If your console accepts only a complete request URL instead of a base/path,
  enter `https://YOUR_EXISTING_DASHBOARD.onrender.com/api/hardware/ingest` there
  and leave the action path empty. Do not append the path twice. Connector
  connectivity tests can fail if they send GET/HEAD to this POST-only endpoint;
  verify the actual rule/action POST instead.

## Rule and action

Create rule `mandalay_node1_to_dashboard` with this SQL:

```sql
SELECT
  topic,
  username,
  payload,
  flags.retain AS retained
FROM
  "aqi/node1/readings"
WHERE
  username = 'mandalay_esp32'
```

The username is the authenticated publisher account; preserve EMQX authorization
allowing this account to publish only to `aqi/node1/readings`. The webhook secret
authenticates EMQX delivery; allowlisted JSON device IDs alone do not authenticate
a physical device. Protect EMQX administrator access and the HTTP connector secret.

Add an HTTP Server action (also called Sink in newer consoles):

- Connector: `mandalay_dashboard_https`
- Method: **POST**
- Path: **`/api/hardware/ingest`**
- Body template, exactly:

  ```json
  {
    "topic": "${topic}",
    "username": "${username}",
    "retained": ${retained},
    "payload": ${payload}
  }
  ```

`payload` is deliberately **unquoted** so the original JSON is embedded as an
object. Forward every original sensor field, including `message_id`, timestamp,
CO/NH3 units and validity flags. Do not project just the concentrations. The
endpoint also accepts a JSON-encoded string in `payload`, but a proper object is
recommended. Both forms undergo strict duplicate-field/nonfinite JSON validation.

Do not create a fallback action that republishes to the source topic (loop), and
do not use the broker rule's event ID as a replacement for firmware `message_id`.

## Retry and cold-start settings

Use these action settings **where exposed by your Serverless console**:

| Setting | Recommended value |
| --- | --- |
| Query mode | Async/buffered, if available |
| Request timeout | 120 seconds |
| Request/message TTL | 1 hour |
| Retry/resume interval | 10 seconds |
| Batch size | 1 — endpoint accepts one observation, not an array |
| Inflight window | 1 |
| Buffer limit | 10 MiB, or the largest suitable limit within your plan |

Render Free sleeps after inactivity and can take a minute or more to wake. A
short timeout may expire while Flask is still starting. A failed response may
also follow a successful database commit. Never assume a timed-out POST was not
stored: retry the **same message_id and payload**. PostgreSQL constraints plus a
transaction advisory lock deduplicate concurrent attempts and check older
timestamp-keyed observations without rewriting them. A duplicate never changes
the original reception timestamp, validity or sensor values.

EMQX upstream 5.8's HTTP connector accepts 2xx and classifies 429/503 as recoverable;
other non-2xx HTTP replies are classified as unrecoverable. Verify your Serverless
release's actual retry behavior and controls: the upstream implementation is not
proof that all settings are exposed or that your cloud queue is durable. If the
console caps request timeout below 120 seconds, use its maximum and confirm
buffered retries survive a real Free cold start. Do not assume `Retry-After`
controls EMQX's backoff. Observe action success/failure/retry/drop metrics.
In particular, a Render gateway 502/504 is not classified like 503 by the cited
upstream connector. Confirm how your Serverless release handles those responses;
if cold-start failures are dropped without retry, this setup cannot promise
reliable cold-start delivery on the available plan.

References: [HTTP integration documentation](https://docs.emqx.com/en/emqx/latest/data-integration/data-bridge-webhook.html)
and [upstream HTTP retry handling](https://github.com/emqx/emqx/blob/release-58/apps/emqx_bridge_http/src/emqx_bridge_http_connector.erl).

If buffering, sufficient TTL, or retries are unavailable on your Serverless plan,
reliable cold-start delivery is a **deployment blocker**, not a reason to disable
TLS or return success before storage. A memory queue can lose messages on broker
restart; a full buffer or expired TTL can drop messages. Existing ESP32 QoS 0
publishing also provides no broker-delivery acknowledgment; this architecture
does not change firmware or guarantee lossless end-to-end delivery.

## HTTP responses and production verification

| HTTP | Meaning / action |
| --- | --- |
| 201 | New real observation committed |
| 200, `status=duplicate` | Already stored; acknowledge delivery |
| 200, `status=rejected`, `reason=synthetic_message` | Test/synthetic deliberately discarded, zero production rows; acknowledge to avoid retries |
| 401 | Header secret missing/wrong; fix private connector configuration |
| 413 | Entire JSON envelope exceeds 16 KiB |
| 415 | Incorrect Content-Type |
| 422 | Malformed JSON/envelope, wrong topic/publisher/device/city, missing ID, invalid units/values/flags/timestamps |
| 503 | Endpoint unconfigured or PostgreSQL temporarily unavailable; retry after correcting configuration / recovering DB |

Authentication uses constant-time comparison before parsing. The endpoint has
no anonymous ingestion, no background subscriber and no asynchronous storage
acknowledgment. The only HTTP 202 workflow remains the separate refresh API.
Delayed observations retain measurement timestamps and can become stale;
retries do not make an old reading look fresh. The existing seven-day backfill
limit remains. `co_range_valid=false` is audited with `co_valid=false`, shown as
invalid/unavailable on Live, and excluded from CO hourly means/CPCB AQI.

1. In EMQX Rule Test, inspect the generated envelope using a synthetic message
   marked `test:true`; use the publisher identity selected by the rule. Enable
   the action and verify deliberate rejection response, not a database reading.
2. Wait for an **actual unchanged ESP32 publish**. Observe action delivery and
   check `/api/hardware/latest`, `/api/hardware/history?days=1`, and
   `/api/hardware/status` on the deployed dashboard. Expect `transport=webhook`
   and `mqtt_connected=null`; sensor online means fresh received observations,
   not a proven current broker session.
3. Confirm the same firmware ID delivered twice produces one database row.
   Confirm the invalid CO flag and valid NH3 flag survive. Check Live, Analytics
   and Alerts use `/api/monitoring/*` and never modeled CO fallback. Enough valid
   8/24-hour history is still required for pollutant eligibility.
4. Allow a real Free cold start and verify a queued real message eventually
   appears. Do not claim production readiness from connector status alone.

No cloud deployment or live EMQX-to-PostgreSQL webhook delivery was performed by
this implementation. Never run the test suite against production; fixtures
drop/create tables only in a disposable `TEST_DATABASE_URL` database.
