# Mandalay hardware monitoring integration

Work is confined to `feature/mandalay-hardware-integration`. No push, merge or Render deployment was performed. At initial integration no firmware or local XGBoost models were available. The user subsequently supplied working `main.cpp`; its calibration and sampling were inspected and preserved in `firmware/mandalay-esp32`. See that folder’s README for the complete corrected firmware, actual-payload verification and remaining broker setup. Hardware connectivity, calibration and the external prediction service were not verified. Historical example sensor numbers were never inserted into the application database.

## Sources and compatibility

Monitoring uses only Open-Meteo PM2.5, PM10, NO2, SO2 and O3, plus ESP32 CO (mg/m³) and NH3 (µg/m³). Missing hardware never falls back to modeled CO. Live, Analytics and Alerts now request `/api/monitoring/latest`, `/api/monitoring/history`, `/api/monitoring/analytics` and `/api/monitoring/alerts`. `/api/hardware/status`, `/api/hardware/latest` and `/api/hardware/history` expose device status and audited observations. Queries are scoped to the configured primary Mandalay device; multi-device data are not pooled.

Existing `/api/latest`, `/api/history`, `/api/analytics`, `/api/alerts`, Open-Meteo refresh, `cpcb.py`, modeled measurements, legacy calculated AQI and prediction exports remain unchanged for compatibility. These legacy endpoints may contain modeled CO and must not be used for hardware monitoring. The existing external prediction navigation and its AQI-standard guard are unchanged. No prediction model, features, training pipeline or remote endpoints were changed. This checkout contains no 24-model prediction application, so its operation must be verified separately. Vadodara is untouched.

## Confirmed non-secret firmware settings

The user reports `broker.hivemq.com:1883`, topic `aqi/node1/readings`, device `node1`, interval 900 seconds. The supplied firmware confirms the existing public topic, 10-second sampling and 900-second averaged publish window. This shared public broker provides no authenticated device provenance and plaintext port 1883 has no confidentiality. Production subscriber deliberately rejects this hostname. Before deployment, move firmware and worker to a private authenticated TLS broker with publish/subscribe ACLs restricting each device topic. Preserve sensor calibration and conversion equations. Actual firmware TLS, QoS and JSON compatibility still require verification.

Use these non-secret settings on BOTH web and worker:

```sh
export MANDALAY_DEVICE_IDS=node1
export HARDWARE_DEVICE_ID=node1
export HARDWARE_SAMPLE_SECONDS=900
export HARDWARE_STALE_SECONDS=1800
```

On the worker also configure:

```sh
export MQTT_HOST=YOUR_PRIVATE_BROKER_HOST
export MQTT_PORT=8883
export MQTT_TOPIC_TEMPLATE='aqi/{device_id}/readings'
export MQTT_CLIENT_ID=mandalay-dashboard-subscriber
```

Set `MQTT_USERNAME` and `MQTT_PASSWORD` in secret environment settings. Alternatively configure `MQTT_CLIENT_CERT` and `MQTT_CLIENT_KEY` paths for mutual TLS, optionally `MQTT_CA_FILE` for a private CA. Certificate and hostname verification are mandatory; never commit credentials. Broker ACLs are essential: the JSON device ID alone is not authentication. `MQTT_ALLOW_INSECURE_DEV=true` permits only local loopback development, never a public broker.

## Payload contract

Required identity is `device_id` from the allowlist. Optional `city` must identify Mandalay, Myanmar; conflicting cities are rejected. Fields supported: `timestamp`, `message_id`, `co_mg_m3`, `nh3_ug_m3`, `co_ppm`, `co_ratio`, `co_range_valid`, `nh3_ratio`, `nh3_valid`, `nh3_range_valid`, `co_valid`, `sensor_valid`, `temperature`, `humidity`. Gas concentrations can be null; finite nonnegative JSON numbers are required otherwise. Boolean validity flags exclude invalid measurements without discarding their original values. Absent validity flags are marked unverified. Optional `units` must match `co_mg_m3: mg/m3` and `nh3_ug_m3: ug/m3`. No conversion from ppm or sensor ratios is invented.

Use an ISO 8601 timestamp with timezone, accurate device clock, and unique measurement timestamps. Measurement time and server reception time are stored separately. Timestamps older than seven days or over 30 seconds ahead are rejected. A missing, malformed or timezone-less timestamp requires a stable `message_id` across retries; reception time is then used with explicit `server_received` quality. Retained undated messages are rejected. Unique device/event keys prevent duplicate observations or retries falsely refreshing freshness. Maximum JSON message size is 16 KiB; duplicate keys and nonfinite constants are rejected. Do not send secrets in payloads.

For durable delivery, a publisher using QoS 1 and a broker with persistent sessions/queues are needed. The corrected supplied PubSubClient firmware retains its existing QoS 0 publishing behavior, so end-to-end durable delivery is not guaranteed. Worker uses a persistent client ID, manual acknowledgments after PostgreSQL commit, reconnect backoff, and an advisory lock to prevent overlapping subscribers. Invalid messages are deliberately acknowledged and discarded with sanitized logs. Database failures leave messages unacknowledged and reconnect. Delivery guarantees depend on firmware QoS and broker persistence, which have not been inspected. Render supervises worker restart; a lost database leader connection terminates the worker. Gunicorn imports never start a subscriber.

## PostgreSQL migration and local commands

Only two additive tables are created: `mandalay_hardware_observations` (original payload, measurement/reception timestamps, validity, concentrations and device indexes) and `mandalay_mqtt_state` (connection/heartbeat). No existing tables are dropped, recreated or altered. Combined AQI is recomputed from validated histories on request rather than overwriting prediction-facing derived records. Workbook remains unchanged. Queries use PostgreSQL exclusively.

From the project root, with `DATABASE_URL` set to the existing PostgreSQL connection:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
.venv/bin/flask --app wsgi migrate-hardware
.venv/bin/gunicorn wsgi:app --bind 127.0.0.1:5010 --workers 2
# In a second terminal, with secure MQTT settings and the same DATABASE_URL:
.venv/bin/python mqtt_worker.py
```

Migration is idempotent. Back up the database before operational deployment. The worker waits for additive schema readiness. Existing hourly refresh remains the existing `flask --app wsgi refresh-data` command.

## Coverage and CPCB limitations

Sampling interval must divide 3600 seconds (10–900). At 900 seconds, an hour needs at least three of four distinct cadence slots (default 75% coverage). Burst samples count only once per slot. Slot averages receive equal weight; no missing samples are filled. Hour bins align to Asia/Yangon, closed at the labeled hour. The latest raw sensor sample appears immediately, while rolling calculations require completed covered hours. A 30-minute staleness setting accommodates the reported 15-minute cadence; MQTT connection status and observation freshness are separate.

CPCB rolling means use 24 hours for PM2.5, PM10, NO2, SO2, NH3 and 8 hours for CO/O3. Each pollutant requires at least 16 covered hours in the preceding 24; CO/O3 additionally require all eight recent hours. Overall index requires at least three eligible pollutants, including a particulate pollutant, and is their maximum sub-index. Current stale hardware and modeled data older than three hours are excluded from Live eligibility. Historical calculations retain valid historical coverage. Current CO is already mg/m³ and is never divided by 1000 again. NH3 uses official CPCB integer breakpoints 0–200, 201–400, 401–800, 801–1200 and 1201–1800 µg/m³. Existing CPCB interpolation/category colors are reused for other pollutants.

Open-ended Severe concentration bands have no finite upper endpoint: category Severe is reported when identifiable, but exact numeric AQI is unavailable instead of inventing interpolation. Seven-pollutant coverage, per-pollutant reasons and missing histories are exposed. Estimates are always provisional because low-cost calibration, temporal/spatial source agreement and regulatory instrument equivalence have not been verified. Eligible subset results are labeled partial; insufficient results are unavailable. No result certifies regulatory compliance or safe conditions. Temperature/humidity are stored without inventing corrections.

Analytics support `days=1..366` and `view=hourly|daily` on history. Daily values are descriptive means with valid-hour counts, not a recalculated regulatory daily AQI. Seven charts retain time/date axes even when observations are absent. Gaps remain gaps. Raw hardware history is capped at 50,000 records: request a shorter interval or aggregated history.

## Alerts

Default rolling concentration thresholds are PM2.5 90, PM10 250, NO2 180, SO2 380, O3 168, NH3 800 µg/m³ and CO 10 mg/m³, using the pollutant's CPCB averaging period and coverage. They are configurable warning thresholds, not instantaneous regulatory exceedances. Overall CPCB alerts begin at 201; open-ended Severe categories also warn. Optional `RAW_ALERT_THRESHOLDS` JSON enables separate instantaneous warnings and is empty by default. `ROLLING_ALERT_THRESHOLDS` JSON overrides rolling thresholds. Keys use API pollutant names; concentrations use the units above. Example configuration, not readings:

```sh
export RAW_ALERT_THRESHOLDS='{"carbon_monoxide":2,"ammonia":1000}'
```

Events include value, threshold, unit, source, timestamp, severity, averaging period and health precautions. Raw, rolling, AQI and offline/stale/unconfigured events are distinct. Stable event IDs deduplicate repeated requests; no outbound notification system is added. Raw warnings alone are not CPCB exceedances.

## Validation performed

Use a DISPOSABLE PostgreSQL database for tests: fixtures intentionally drop and recreate tables only in `TEST_DATABASE_URL`. Never point this variable to application data.

```sh
TEST_DATABASE_URL=postgresql+psycopg://agent@127.0.0.1:55432/mandalay_test .venv/bin/python -m pytest -q
# Browser tooling is optional and separate from deployment dependencies:
.venv/bin/pip install playwright
DASHBOARD_BASE_URL=http://127.0.0.1:5010 .venv/bin/python scripts/hardware_browser_smoke.py
```

95 tests pass, including existing CPCB/import/API tests and new source isolation, NH3 interpolation, coverage, missing/stale data, secure transport configuration, durable acknowledgment behavior, advisory-lock exclusion, migration preservation and seven-pollutant alerts. Browser smoke checks seven cards/charts, missing hardware, chart axes, daily selection, theme/mobile layout, rendering a browser-only hardware fixture and US-AQI rejection without writing sensor data. The existing `scripts/browser_smoke.py` entry point now runs these monitoring checks. Screenshots go to ignored `build/hardware-browser/`.

## Render preparation — not deployed

`render.yaml` adds a separate single-instance background worker and hardware migration to the existing web pre-deploy command. Existing database, hourly refresh and prediction URL remain. Web and worker must share the same Mandalay device/cadence/staleness settings and PostgreSQL connection. Set both device variables to `node1`, keep interval 900 and stale threshold 1800, configure the private broker secrets and topic, and update actual firmware transport before deployment. Never change existing prediction service environment/features to this monitoring calculator without a separately validated retraining task. No services were provisioned or deployed here.

Modified: `app.py` (blueprint registration only), `static/app.js`, `templates/dashboard.html`, `render.yaml`, `requirements.txt`, `requirements.lock.txt`, `README.md`. Added: `hardware.py`, `combined_cpcb.py`, `mqtt_worker.py`, `tests/test_hardware.py`, `scripts/hardware_browser_smoke.py`, this document. The existing `scripts/browser_smoke.py` is updated to run the seven-pollutant checks. Firmware, workbook, legacy calculator/importers and prediction export code are unchanged.
