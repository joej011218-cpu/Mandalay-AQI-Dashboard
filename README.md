# Mandalay CPCB AQI Dashboard

Flask, PostgreSQL and Open-Meteo dashboard for **Mandalay, Myanmar only** (21.9588, 96.0891). Live, Analytics, Alerts and About use **Indian CPCB AQI**, calculated from pollutant histories. The interface follows the public reference's top navigation, AQI panel, pollutant cards, charts and alerts, with the same six CPCB colors and light/dark mode. No existing Vadodara application or model was changed.

## Setup

Python 3.12 and PostgreSQL 17 are recommended. SQLite is rejected.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock.txt
export DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/mandalay_aqi'
flask --app wsgi init-db
flask --app wsgi recalculate-cpcb
flask --app wsgi refresh-data
flask --app wsgi run --host 0.0.0.0 --port 5000
```

Set `DATABASE_URL` securely. For remote PostgreSQL use TLS (`sslmode=require`). Configure `REFRESH_TOKEN` securely to enable authenticated `POST /api/refresh`; it is never needed in browser code. Trusted schedulers use the CLI instead.

The upgrade is additive: `init-db` creates `mandalay_cpcb_aqi` and `mandalay_source_observations` without modifying existing `mandalay_readings`. `recalculate-cpcb` populates derived results, including explicit missing statuses. Existing raw `us_aqi` values remain unchanged. Legacy UTC-request data is retained for audit but excluded from the canonical Yangon whole-hour timeline. Future schema changes need explicit migrations; `create_all` does not alter existing columns.

## CPCB methodology

See [docs/CPCB_METHOD.md](docs/CPCB_METHOD.md) for the concentration table, source references, interpolation formula, rounding convention, completeness safeguards, window boundaries, Severe-band handling and dataset limitations.

- PM2.5, PM10, NO2 and SO2 use 24-hour rolling means with at least 16 valid hourly observations.
- CO and O3 use the last eight hours, require at least 16 valid hours in the surrounding 24-hour period, and conservatively require all eight recent hourly values.
- At least three eligible pollutants, including PM2.5 or PM10, are required.
- Canonical raw concentrations use µg/m³; the CO mean is converted to mg/m³ for its sub-index.
- Overall AQI is the maximum eligible pollutant sub-index. Dominant ties are exposed.
- CPCB concentrations are rounded half-up to the table's reporting resolution before interpolation; indices are rounded half-up. The convention is versioned `cpcb-2014-rolling-v1`.
- Published Severe concentration bounds are open-ended. The app does not invent a finite concentration for AQI 500: these cases have a Severe-band warning, null numeric AQI and an explanatory unavailable status.
- Raw US/source AQI is never used in calculation, charts, statistics, threshold selection or prediction targets.

Categories and reference colors: Good 0–50 `#4E9F3D`; Satisfactory 51–100 `#A8C43A`; Moderate 101–200 `#E8C547`; Poor 201–300 `#E8823C`; Very Poor 301–400 `#D8453B`; Severe 401–500 `#7A2A2A`.

Open-Meteo provides **modeled CAMS concentrations**, not physical CPCB station observations. This is an hourly rolling application of the Indian CPCB methodology to Mandalay data, rather than an official Indian station index. Official documentation URLs were blocked by the cloud proxy during this change; the transcribed table and implementation conventions are exposed in the methodology document for review.

## Inspect and import the supplied workbook

[docs/dataset_inspection.json](docs/dataset_inspection.json) records the uploaded file SHA256, headers, counts and ranges. The sheet `Mandalay_AQI_2026` contains 6,768 consecutive hourly rows from January 1 to October 9, 2026, with no missing cells, negative concentrations or duplicate timestamps. Headers:

`timestamp`, `aqi`, `pm25`, `pm10`, `co_ug_m3`, `no2_ug_m3`, `so2_ug_m3`, `o3_ug_m3`.

Gas units are explicit. PM units, timezone and source AQI standard are not in the workbook metadata. All seven numeric fields matched Open-Meteo's Asia/Yangon response at all 168 overlapping hours. This validates using Yangon wall time, PM µg/m³ and retaining source `aqi` as raw US AQI for this file. These remain explicit operator inputs and are recorded in PostgreSQL provenance.

```sh
python scripts/import_history.py /path/to/mandalay_aqi_2026_hourly.xlsx \
  --mandalay-workbook --confirm-mandalay \
  --pm-unit ug/m3 --timestamp-timezone Asia/Yangon \
  --source-aqi-standard us-epa
```

This command was executed against the attached workbook and the application PostgreSQL database. It validates the entire workbook before atomically writing raw measurements, original cells and provenance, then recalculates CPCB history. Reimporting is idempotent. Duplicate timestamps, unsupported units, nonfinite/negative values and times not on the Yangon whole-hour grid are rejected. No US-to-CPCB conversion or relabeling occurs.

For other workbooks use `--column FIELD=HEADER` and `--unit FIELD=ug/m3` or `mg/m3` for each mapped pollutant. `original_aqi` can retain an unknown or CPCB source AQI without using it for calculated results. An explicit `us_aqi` mapping requires verified `--source-aqi-standard us-epa`. Unknown ppm/ppb units are rejected rather than converted without temperature and pressure. A workbook's city must be verified before `--confirm-mandalay`; filenames alone do not establish provenance.

## Storage and API

- `mandalay_readings`: one canonical measurement row per timestamp, normalized pollutant values, source and raw `us_aqi`. Old alternate-grid rows remain preserved.
- `mandalay_source_observations`: original source values, input units, timezone, file hash and mappings; unique by source and timestamp. Multiple sources can preserve their originals without duplicating the dashboard timeline.
- `mandalay_cpcb_aqi`: separately calculated AQI, eligibility/status, dominant pollutant, averaging counts, reasons, version and calculation timestamp.

Import and refresh acquire a PostgreSQL transaction advisory lock and rebuild CPCB results in the same transaction as raw updates. Corrected history changes downstream rolling results; failed calculations roll back the import. This implementation recalculates the full single-city history, suitable for the current annual dataset; benchmark and use bounded recalculation before extending to many cities/years.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/health` | PostgreSQL connectivity and raw/derived schema health |
| `GET /api/latest` | Current raw pollutant concentrations plus calculated CPCB AQI, dominant pollutant, completeness and precautions; 404 when no canonical data |
| `GET /api/history?days=7` | Ordered Mandalay readings with CPCB results; `details=1` includes per-pollutant calculation details |
| `GET /api/analytics?days=7` | CPCB/pollutant means, minima, maxima, valid counts and unavailable AQI count; no US AQI summary |
| `GET /api/alerts?days=7` | CPCB AQI ≥201 and explicitly nonnumeric Severe-band warnings, with Mandalay health precautions |
| `POST /api/refresh` | `Authorization: Bearer REFRESH_TOKEN`; validated Mandalay Open-Meteo ingestion and CPCB rebuild |

Periods support 1–366 days. All UI times use Asia/Yangon. Future readings are excluded from these endpoints. Staleness starts after three hours. A row with missing CPCB never falls back to raw US AQI. Empty alerts never establish safety. Means of stored AQI are descriptive, not a new regulatory AQI computation. Chart gaps are not interpolated.

## Mandalay XGBoost integration

No prediction model was supplied or relabeled. Export available, nonfuture CPCB training targets:

```sh
python scripts/export_cpcb_training.py /path/to/new_training.csv
```

The exporter recalculates first, excludes unavailable targets, refuses overwrite, writes raw concentration features plus `target_cpcb_aqi`, and saves a methodology sidecar. Raw US AQI is absent from the training CSV. A local export was verified. The separate XGBoost application must be trained and evaluated against these CPCB targets; use a chronological train/validation split and explicitly shift targets for the prediction horizon. Same-hour measurements alone are not a validated future forecast design.

After training, the model's saved metadata and prediction API should declare:

```json
{
  "city": "Mandalay, Myanmar",
  "aqi_standard": "Indian CPCB AQI",
  "calculation_version": "cpcb-2014-rolling-v1",
  "target": "target_cpcb_aqi"
}
```

Set `PREDICTION_URL` to the deployed HTTPS Mandalay application and, **only after verifying its training metadata**, set `PREDICTION_AQI_STANDARD=cpcb-2014-rolling-v1`. Both are required to activate the navigation link. This is an explicit deployment assertion, not automatic model verification. A US AQI model must be retrained; changing its label or configuration does not change its target.

## Render

Create a Render Blueprint from `render.yaml`. It provisions paid managed PostgreSQL, a Gunicorn web service and a minute-5 hourly UTC cron. Review Render pricing before deployment. The predeploy command initializes the additive schema and recalculates historical CPCB results. The cron refreshes Mandalay measurements and derived AQI atomically. Import the supplied workbook and run the first refresh in a secure shell with the managed database URL; enable database backups, TLS and freshness monitoring. No Render deployment was performed during this change.

## Tests

Use a **disposable PostgreSQL database**: integration tests drop and recreate these Mandalay tables.

```sh
TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/mandalay_test' python -m pytest -q
node --check static/app.js
```

Calculation tests cover breakpoint interpolation, reporting precision, mass conversions, 24/8-hour windows, 16-hour completeness boundaries, recent gaps, PM/minimum-pollutant eligibility, invalid values, Severe bounds and US AQI exclusion. Integration tests cover API consistency, source preservation, atomic recalculation/rollback, idempotent import, coordinate/unit validation, refresh authentication and the CPCB prediction gate.

Browser smoke tests require Chromium and a separately installed development dependency:

```sh
pip install playwright==1.55.0
python scripts/browser_smoke.py
```

The current isolated cloud instance retains PostgreSQL 17 under `/workspace/mandalay-postgres` (loopback 55432, development trust authentication) and dependencies under `/workspace/mandalay-venv`. Run `scripts/cloud_start.sh`; existing managed `DATABASE_URL` takes precedence. The local cluster is for isolated development only. Application data is in `mandalay_aqi`; destructive integration tests use `mandalay_test`.

## Remaining data limitations

The workbook covers only part of 2026, has no NH3/Pb or instrument QA flags, and does not itself document timezone, PM units or station provenance. Provider overlap supports the chosen interpretations but does not certify ground measurements. The first 15 rows lack enough history; later missing data can also prevent eligibility. Eight-of-eight CO/O3 completeness is conservative. Severe bands cannot receive reliable numerical interpolation without an authoritative finite endpoint. The separately deployed CPCB prediction model and URL remain unavailable.

## ESP32 CO/NH3 monitoring integration

The hardware integration on `feature/mandalay-hardware-integration` adds source-isolated provisional CPCB monitoring, PostgreSQL sensor history and a separate authenticated TLS MQTT worker. CO/NH3 never fall back to modeled values in the monitoring pages. Existing prediction navigation and modeled-data pipeline remain unchanged. See [hardware integration setup, coverage, testing and Render configuration](docs/HARDWARE_INTEGRATION.md). No firmware or live broker connection was verified; public HiveMQ plaintext settings require migration before production ingestion.

The actual supplied ESP32 firmware is now integrated in [firmware/mandalay-esp32](firmware/mandalay-esp32/README.md), preserving calibration and sampling while adding verified TLS and backend-compatible payloads. Real broker credentials and board validation remain pending.
