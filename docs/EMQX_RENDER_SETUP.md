# EMQX Cloud Serverless and Render: Mandalay

Confirmed non-secret connection settings:

| Setting | Value |
| --- | --- |
| Broker hostname | `p0fb61ec.ala.asia-southeast1.emqxsl.com` |
| TLS MQTT port | `8883` |
| Device ID / device allowlist | `node1` |
| Worker topic template | `aqi/{device_id}/readings` |
| Effective topic | `aqi/node1/readings` |
| ESP32 authentication account | `mandalay_esp32` |
| Worker authentication account | `mandalay_worker` |

No passwords are stored in source. Worker reads `MQTT_USERNAME` and `MQTT_PASSWORD` from its process environment. Render config sets `MQTT_EXPECTED_USERNAME=mandalay_worker`; startup refuses a different account. Host and port remain environment-driven. This task does not deploy, push, merge, flash a device, or create/change EMQX accounts/authorization rules.

## Authentication and authorization requirements

Account creation establishes authentication, not the required topic restrictions. In the EMQX deployment console, verify authentication is enabled for both accounts, then configure/check authorization for the exact topic. Use username-scoped rules rather than only client-ID rules:

| Account | Allow | Deny |
| --- | --- | --- |
| `mandalay_esp32` | Publish to `aqi/node1/readings` | Subscribe; publish to other topics |
| `mandalay_worker` | Subscribe to `aqi/node1/readings` | Publish; subscribe to other topics |

Ensure no broader default/global/wildcard allow or superuser setting defeats these restrictions. Apply the appropriate deny/default policy and precedence in the authorization controls available in your Serverless deployment. Exact UI names vary with console versions; do not assume account creation automatically installed these rules. Avoid permitting `#`, `+`, unrelated cities, or shared-subscription topics for these accounts. The worker requests only the single exact topic. If client-ID restrictions are enabled, allow worker client ID `mandalay-dashboard-subscriber` and the ESP32's existing `AQI-ESP32-<chip-id>` identity. Give worker and device separate passwords.

The code verifies username selection, trusted TLS with hostname checking, exact topic/device matching, JSON schema, timestamps, numeric validity and Mandalay city isolation. It now checks MQTT subscription acknowledgments: grants 0/1 are accepted, denial 128 is rejected, and readiness stays false until all configured topics are authorized. Disconnection clears readiness; automatic reconnect uses 1–60 second backoff and re-subscribes. Logs contain diagnostic messages, not passwords or payload contents.

Actual cloud authorization settings cannot be inspected from this task: no EMQX console session or broker passwords are available. EMQX documentation access was also unavailable from this environment, so no claim is made that a specific console UI or deployment rule has been inspected. The table above is the required permission policy, checked in local worker tests; confirm and test the actual rules before operational deployment. Reference: [EMQX Cloud documentation](https://docs.emqx.com/en/cloud/latest/).

## TLS trust

Worker uses Python `ssl.create_default_context()` with `CERT_REQUIRED`, hostname verification and TLS 1.2 minimum. Default system CA trust is used when `MQTT_CA_FILE` is unset. Do not call `tls_insecure_set`, disable hostname verification or set `MQTT_ALLOW_INSECURE_DEV=true` on Render.

If the deployment presents a CA chain not trusted by the worker system, obtain its actual trusted root CA PEM from the EMQX deployment and mount it as a Render secret file, then set `MQTT_CA_FILE` to that file's absolute path. Do not guess the CA or download an unverified certificate and trust it automatically. The actual broker chain has not been tested here. ESP32 separately needs the actual root CA PEM in its firmware configuration; a worker trusting system CAs does not supply the ESP32 trust store.

## Render web service — existing dashboard

These instructions are for a later manual deployment. The local feature commit must be reviewed and made available in the connected Git remote before Render can build it. Nothing was pushed by this task.

1. Open the existing **Mandalay dashboard** web service. Keep the existing PostgreSQL database, region, prediction settings and Open-Meteo scheduled refresh. Do not create a second database or replace `DATABASE_URL`; hardware history must share the dashboard's current PostgreSQL records.
2. Turn Auto-Deploy off while preparing the configuration. When ready, select repository `joej011218-cpu/Mandalay-AQI-Dashboard` and branch `feature/mandalay-hardware-integration` in this dashboard service. Do not change the separate prediction service or its branch.
3. Use Python runtime and these commands:

```text
Build: pip install -r requirements.lock.txt
Pre-deploy: flask --app wsgi init-db && flask --app wsgi recalculate-cpcb && flask --app wsgi migrate-hardware
Start: gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60 --access-logfile -
Health check: /api/health
```

The existing init/recalculation commands are preserved. `migrate-hardware` adds only the hardware observation/state tables and indexes, is idempotent and does not drop existing data. If those tables are already installed, no additional migration is required. Back up production data before an operational release. Synthetic tests use a separate disposable PostgreSQL database, never the application database.

4. Web environment:

```text
DATABASE_URL=<existing Render PostgreSQL Internal Database URL>
MANDALAY_DEVICE_IDS=node1
HARDWARE_DEVICE_ID=node1
HARDWARE_SAMPLE_SECONDS=900
HARDWARE_STALE_SECONDS=1800
PYTHON_VERSION=3.12.14
```

Keep existing `REFRESH_TOKEN`, `PREDICTION_URL` and `PREDICTION_AQI_STANDARD` values unchanged. The web service does not need MQTT usernames/passwords and does not run MQTT ingestion. Do not replace the Render-generated `$PORT`. If configuring external PostgreSQL instead of Render internal access, use the database provider's required TLS settings.

5. After user approval, manually deploy the web revision, check the health endpoint and completed pre-deploy migration. Check `/api/monitoring/latest` and `/api/hardware/status`. Hardware is offline until real sensor data arrives; that is expected, not a reason to substitute modeled CO.

## Render background worker — separate service

After the web hardware migration is available, prepare a **Background Worker**, not a second web service or a Gunicorn thread:

```text
Name: mandalay-mqtt-ingestion
Repository: joej011218-cpu/Mandalay-AQI-Dashboard
Branch: feature/mandalay-hardware-integration
Runtime: Python
Instances: 1
Build: pip install -r requirements.lock.txt
Start: python mqtt_worker.py
Auto-Deploy: off during preparation
```

Use the same region and current database as the dashboard. No public HTTP port or health-check URL is needed for the worker. `render.yaml` contains the corresponding worker configuration. For an existing deployment, manual service configuration avoids accidentally provisioning the Blueprint's database declaration as a new empty database.

Set environment values exactly:

```text
DATABASE_URL=<same existing PostgreSQL Internal Database URL as web>
MANDALAY_DEVICE_IDS=node1
HARDWARE_DEVICE_ID=node1
HARDWARE_SAMPLE_SECONDS=900
HARDWARE_STALE_SECONDS=1800
MQTT_HOST=p0fb61ec.ala.asia-southeast1.emqxsl.com
MQTT_PORT=8883
MQTT_TOPIC_TEMPLATE=aqi/{device_id}/readings
MQTT_CLIENT_ID=mandalay-dashboard-subscriber
MQTT_EXPECTED_USERNAME=mandalay_worker
MQTT_USERNAME=mandalay_worker
MQTT_PASSWORD=<the mandalay_worker password, entered privately in Render>
PYTHON_VERSION=3.12.14
```

Leave `MQTT_ALLOW_INSECURE_DEV`, `MQTT_CLIENT_CERT`, `MQTT_CLIENT_KEY` and `MQTT_CA_FILE` unset for standard public-CA TLS and username/password auth, unless EMQX specifically requires custom CA trust as described above. Do not put the publisher password in this service. Do not put either password in `render.yaml` or Git. Username is explicitly entered as an environment variable; the Python worker does not embed account credentials.

When approved, manually start/deploy the worker. It waits for additive schema readiness and takes a PostgreSQL advisory lock to avoid duplicate subscribers during overlapping worker replacements. Render supervises process recovery. The existing scheduled `flask --app wsgi refresh-data` remains unchanged.

## Actual ESP32 connection

Sensor calibration, sampling, firmware equations and current topic are unchanged by this EMQX worker task. Configure the corrected firmware locally using its existing environment-to-ignored-header build script:

```text
WIFI_SSID=<actual WiFi network>
WIFI_PASSWORD=<actual password or empty for your open WiFi>
MQTT_HOST=p0fb61ec.ala.asia-southeast1.emqxsl.com
MQTT_PORT=8883
MQTT_USERNAME=mandalay_esp32
MQTT_PASSWORD=<the publisher password, kept locally>
MQTT_TOPIC=aqi/node1/readings
MQTT_CA_FILE=<path to the actual EMQX trusted root CA PEM>
```

Use the existing [firmware build/flash instructions](../firmware/mandalay-esp32/README.md). Device ID remains `node1`. Do not change to a Mandalay-specific topic unless both ends are deliberately changed together. Wait for firmware NTP clock synchronization before verified TLS connection. Only flash when you are ready; no flash is performed here.

## Verification after a later manual deployment

1. In worker logs, distinguish authenticated transport from `MQTT subscription authorized`. A successful login alone does not prove topic permission. `/api/hardware/status` reports `mqtt_connected=true` only after authorized subscription readiness and a recent worker heartbeat. This does not mean the sensor is online until fresh data exists.
2. An actual ESP32 publish must create a new audited hardware row. Check `/api/hardware/latest` and `/api/hardware/history?days=1` for `node1`, Mandalay, measurement/reception timestamps, CO mg/m³, NH3 µg/m³ and validity flags. A missing/invalid clock is explicitly labeled reception-time fallback.
3. Check `/api/monitoring/latest`, Analytics and Alerts. CO/NH3 remain hardware-only. Do not expect eligible CPCB hardware sub-indices before sufficient covered history. The original MQ-7 range excludes readings below 10 ppm; raw values remain audited but are not valid monitoring/AQI values.
4. Use the EMQX test client/MQTT tooling privately to verify negative authorization cases: worker cannot publish; publisher cannot subscribe; neither account can use unrelated topics. Authentication with an incorrect password must fail. Do not write test sensor history to the production topic; test negative publish permissions on a separate test deployment/topic with no dashboard ingestion.
5. Check reconnect after a controlled network interruption. Current PubSubClient firmware publishes at QoS 0: a successful send is not broker/database acknowledgment, and offline/power-loss delivery is not guaranteed. Subscriber QoS 1 cannot upgrade publisher QoS 0.

Local verification: all 112 tests pass, including confirmed EMQX host/port, environment account guard, TLS certificate/hostname settings, reconnect backoff and accepted/denied subscription acknowledgments, plus existing ingestion/database/payload/CPCB/prediction navigation tests. No live broker login, certificate handshake, EMQX permission check or real ESP32 reception was performed. Those steps require private credentials, live network access and the actual deployment/device.
