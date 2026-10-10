# CPCB calculation contract

The source table is the Indian CPCB **National Air Quality Index (2014)** concentration breakpoint table, also reproduced in CPCB's *How AQI is calculated*. Reference URLs:

- https://cpcb.nic.in/uploads/Projects/AirQuality/National_Air_Quality_Index.pdf
- https://app.cpcbccr.com/ccr_docs/How_AQI_Calculated.pdf

These official URLs were blocked by the cloud proxy during this change. The table and requirements below are explicitly transcribed for review; direct document retrieval was not verified.

| AQI | PM2.5 24h | PM10 24h | NO2 24h | SO2 24h | O3 8h | CO 8h (mg/m³) |
| --- | --- | --- | --- | --- | --- | --- |
| 0–50 | 0–30 | 0–50 | 0–40 | 0–40 | 0–50 | 0–1.0 |
| 51–100 | 31–60 | 51–100 | 41–80 | 41–80 | 51–100 | 1.1–2.0 |
| 101–200 | 61–90 | 101–250 | 81–180 | 81–380 | 101–168 | 2.1–10 |
| 201–300 | 91–120 | 251–350 | 181–280 | 381–800 | 169–208 | 10.1–17 |
| 301–400 | 121–250 | 351–430 | 281–400 | 801–1600 | 209–748 | 17.1–34 |
| 401–500 | >250 | >430 | >400 | >1600 | >748 | >34 |

All concentration columns except CO use µg/m³. The canonical raw database concentrations use µg/m³ for all six pollutants; only the CO rolling mean is divided by 1,000 for its CPCB sub-index. Raw numbers and input units are also retained in a separate source-observation table. NH3 and Pb are not present in this workbook or the selected Open-Meteo response; they are not invented.

For each finite band, interpolate `I = (I_hi - I_lo) * (C - C_lo) / (C_hi - C_lo) + I_lo`. The explicit reporting convention in this implementation rounds mean concentrations half-up to integer µg/m³ (CO to 0.1 mg/m³) to resolve the published discrete band gaps; indices are rounded half-up to integers. This convention is versioned and used identically in history and prediction-target exports. It is not US EPA truncation.

CPCB requires at least three pollutant sub-indices including PM2.5 or PM10, and at least 16 valid hours of data for a sub-index. At timestamp `t`, this application examines the exact 24 hourly slots `(t−24h, t]`. PM2.5, PM10, NO2 and SO2 use the mean of valid slots, with at least 16/24. CO and O3 use the last eight hours, require at least 16 valid hours in the surrounding 24-hour period, and conservatively require **all eight recent hours**. Eight-of-eight is an additional completeness safeguard, not a claimed official six-of-eight rule. Missing values, invalid/negative values, irregular timestamps, duplicate slots and future data cannot satisfy completeness.

These are **hourly rolling CPCB calculations** for a modeled dataset. They are not calendar-day station AQI or a daily maximum of eight-hour averages. Do not represent them as physical CPCB station observations.

The published Severe band has no finite concentration endpoint corresponding to AQI 500. The implementation does **not invent that endpoint**. A sufficiently complete Severe-band pollutant yields an identifiable Severe band and health precautions, but `cpcb_aqi: null`, `status: unavailable`, and an explanatory reason instead of an arbitrary numeric interpolation. Alerts include these explicitly labeled Severe-band warnings once the minimum-pollutant requirements are satisfied. Below the Severe band, overall AQI is the maximum eligible integer sub-index. Dominant ties are exposed as a list; a deterministic first pollutant is used for the headline.

A small change to raw history can change every rolling result through the next 23 hours. Import, refresh and `recalculate-cpcb` rebuild the derived table transactionally under a PostgreSQL advisory lock. Calculation details, counts, reasons, version and timestamp are stored separately from measurements. No original `aqi` or `us_aqi` participates in this calculation.

## Workbook inspection

`Mandalay_AQI_2026` contains 6,768 observations from **2026-01-01 00:00 to 2026-10-09 23:00**, in Excel datetime cells without timezone offsets. There are no duplicate timestamps, missing rows, missing pollutant cells or negative concentrations. Columns: `timestamp`, `aqi`, `pm25`, `pm10`, `co_ug_m3`, `no2_ug_m3`, `so2_ug_m3`, `o3_ug_m3`.

The gas column names declare µg/m³. PM units and timestamp timezone are not declared in the workbook. A read-only comparison with Open-Meteo at the Mandalay coordinates and timezone Asia/Yangon found **168/168 overlapping hours matching all seven numeric fields**, including the unlabelled `aqi` matching Open-Meteo `us_aqi`. This supports interpreting timestamps as Yangon wall time, PM concentrations as µg/m³, and retaining the source `aqi` as raw US AQI. These interpretations remain explicit importer arguments and are captured in import provenance. The file contains no coordinates or station metadata; Mandalay provenance rests on the user's supplied file and the matching provider overlap, not on a station certification.

The first 15 hours cannot meet the 16-hour requirement. CO/O3 windows may be ineligible after gaps even where other pollutants qualify. The workbook does not cover the entire calendar year, does not include NH3/Pb or instrument QA flags, and is modeled data. Only canonical Yangon whole-hour observations feed the dashboard. Legacy UTC-request rows on a different half-hour grid are retained for audit but do not become extra observations in these calculations or charts.
