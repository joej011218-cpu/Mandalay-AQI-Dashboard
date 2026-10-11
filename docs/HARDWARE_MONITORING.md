# Live, diagnostic history and persistent ESP32 alerts

The active architecture remains ESP32 → EMQX → authenticated HTTPS webhook → Flask → PostgreSQL. No firmware, EMQX rules, secrets, prediction components or Open-Meteo collection changes are required for this update. No deployment has been performed by these changes.

## Database and APIs

`mandalay_hardware_observations` is reused without schema changes. It stores original measurements (including invalid numeric estimates), measurement and reception timestamps, effective CO/NH3 validity, original validity flags and calibrated-range status in JSONB, temperature/humidity and the original payload containing `message_id`. Device/event keys and the existing message-ID audit lookup deduplicate retries; the serializer exposes `message_id`. ESP32 provenance is explicit in public responses; this table contains hardware observations only. Original and effective flags are kept separate, so `co_valid=true` in firmware cannot override `co_range_valid=false`.

The only new table is `mandalay_hardware_alert_events`. It stores device, condition, pollutant, start/evidence/last-seen/resolution timestamps, active/resolved status, value, threshold, units, averaging period, severity and rule description. A partial unique index allows only one active episode per device/condition. PostgreSQL transaction advisory locks serialize ingestion and alert evaluation across Gunicorn processes. Measurement insertion and episode updates commit together; failures roll back both and the existing webhook returns retryable HTTP 503. Duplicate messages neither insert readings nor refresh their freshness.

The existing `flask --app wsgi migrate-hardware` command creates the additional table/indexes under the existing database migration lock. It is additive, transactional, safe to repeat and preserves all existing rows. The unchanged `scripts/start_web.sh` already runs it before Gunicorn. There are no destructive migrations or new measurement tables.

Existing endpoints are reused:

- `POST /api/hardware/ingest`: authentication and EMQX envelope are unchanged. HTTP 201 confirms durable insertion; 200 acknowledges duplicates or rejected synthetic tests; invalid production payloads remain rejected.
- `GET /api/hardware/latest` and `/api/hardware/history?days=1..366`: audited raw estimates and flags, ESP32 source, measurement/reception timestamps in Asia/Yangon. History additionally exposes `sample_period_seconds` for chart gap handling. The existing 50,000-row limit is retained.
- `GET /api/monitoring/latest`: covered combined CPCB calculations and current safe concentration fields remain separate from the raw audit observation. An invalid latest hardware observation is explicitly excluded from current AQI eligibility even if older hours have valid coverage.
- `GET /api/monitoring/history` and `/api/monitoring/analytics`: covered, valid hourly histories and statistics remain unchanged. Diagnostic estimates never enter these averages.
- `GET /api/monitoring/alerts?days=1..366`: existing Open-Meteo/AQI alerts plus persisted active/recent hardware episodes. Responses include `device_id`, `status`, `measurement_timestamp`, `last_seen` and `resolved_at` for stored episodes. Polling this endpoint evaluates and persists offline transitions, so it requires the existing application database write permissions.

## Page behavior

Live shows the latest finite raw CO/NH3 estimates, source and Asia/Yangon measurement time. Small values use significant digits rather than rounding to a misleading zero. CO with `co_range_valid=false` is labeled **Outside calibrated range — not AQI eligible**. Other invalid estimates are labeled invalid; stale estimates retain a stale label and the device status indicator. This visibility never changes flags or AQI eligibility. Missing readings remain unavailable; Open-Meteo never supplies replacement CO/NH3.

Analytics retains seven charts. The five Open-Meteo charts and all covered-hour statistics follow the existing hourly/daily view. CO/NH3 charts read persisted raw hardware history for the selected day-period, regardless of the hourly/daily summary selection; their captions explicitly explain this distinction. Valid estimates use the accent color; invalid estimates use amber points/dashed segments and diagnostic-only labels. Hover titles show units, validity, ESP32 and Yangon timestamps. Missing samples and gaps exceeding 1.5 publication intervals break lines; no zero padding or interpolation is used. The chart axes retain responsive Yangon time/date labels. Browser clock and existing period controls determine the visible range.

Alerts distinguish sensor-quality, sensor-status, configured raw warnings and covered rolling pollution warnings. Repeated readings update one active hardware episode; a cleared/invalid/stale condition resolves its pollution episode. Recurrence starts a new episode. Resolved events remain stored and visible within the selected historical window. Out-of-range CO produces a quality warning, never a pollution alert from its numeric estimate. Invalid/stale latest readings suppress current hardware pollution warnings. Valid past events remain historical records rather than being erased when hardware becomes invalid/offline.

## Threshold methodology and limits

The existing rolling warning defaults remain CO 10 mg/m³ over 8 hours and NH3 800 µg/m³ over 24 hours, configurable through `ROLLING_ALERT_THRESHOLDS`. These are the project's warning thresholds; they are not new legal/regulatory limits. Existing `RAW_ALERT_THRESHOLDS` is optional and empty by default; any configured raw warning is explicitly described as non-CPCB. No new instantaneous regulatory threshold is invented.

The existing CPCB coverage/interpolation methodology is unchanged: valid covered hours, at least 16 hours within 24, all 8 recent covered hours for CO/O3, and the existing overall minimum-pollutant/PM requirement. Stale or invalid current hardware is excluded. Calibration verification and regulatory equivalence remain unproven; estimates/AQI remain provisional. No temperature/humidity correction or calibration equation is invented.

Hardware episodes start being recorded after this version is installed. Older observations remain available for diagnostic history but old alert episodes are not fabricated/backfilled. Offline events are detected on the next ingestion or Alerts poll, not by a continuously running scheduler. A sleeping Render Free service cannot guarantee exact-time offline detection or delivery; EMQX retries and message deduplication remain important. No outgoing notifications are added. No MQTT subscriber runs inside Gunicorn.

## Deployment and verification

After reviewing/publishing this branch, deploy the existing web service from `feature/mandalay-hardware-integration` when ready:

- Root Directory: blank (repository root).
- Build Command: `pip install -r requirements.lock.txt`.
- Start Command: `bash scripts/start_web.sh` (unchanged).
- Keep existing `DATABASE_URL`, `REFRESH_TOKEN`, `HARDWARE_WEBHOOK_TOKEN` and prediction-link settings unchanged.
- Retain `HARDWARE_TRANSPORT=webhook`, `MANDALAY_DEVICE_IDS=node1`, `HARDWARE_DEVICE_ID=node1`, `MQTT_TOPIC_TEMPLATE=aqi/{device_id}/readings`, `MQTT_PUBLISH_USERNAME=mandalay_esp32`, `HARDWARE_SAMPLE_SECONDS=900`, `HARDWARE_STALE_SECONDS=1800`.

`HARDWARE_SAMPLE_SECONDS` represents the delivered 15-minute averaged publication interval, not the ESP32's internal 10-second sampling loop. No new required environment variable or paid background worker is needed. Do not create a replacement database or regenerate tokens. The startup migration must finish successfully before webhook traffic is served. Keep EMQX's working connector, rule, authentication and body configuration unchanged.

To verify actual readings, watch the next real 15-minute publication: EMQX action should receive 201 (or 200 for a retry), `/api/hardware/latest` should advance its `message_id`, measurement timestamp and reception timestamp, and `/api/hardware/history?days=1` should contain the saved estimate with original flags. Live/Analytics/Alerts automatically reload every five minutes while open; a manual reload shows committed readings immediately. Check that out-of-range CO is numeric with the warning, amber in history, excluded from Live calculation details, and listed as a sensor-quality warning rather than a pollution exceedance. No live broker test was performed as part of this update.

Run validation only against a disposable PostgreSQL `TEST_DATABASE_URL`; fixtures drop/recreate test tables. Browser checks use intercepted diagnostic fixtures without inserting fabricated hardware observations.
