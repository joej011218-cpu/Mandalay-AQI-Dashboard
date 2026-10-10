# Corrected Mandalay ESP32 firmware

Complete firmware: `src/main.cpp`, transport serializer `include/window_payload.h`, local configuration template `include/secrets.example.h`, PlatformIO configuration and optional environment-to-header build script. This is based on the user's actual uploaded `main.cpp`, not invented calibration. The input file is fingerprinted in `inspection.json`. No MQTT/WiFi credentials are committed. No device was flashed and no live messages were received.

## What remains unchanged

ESP32 GPIOs (MQ-7 34, MQ-135 35, DHT22 4), ADC resolution/attenuation, 20 ADC reads per observation, 10-second observation interval, 15-minute average, 30-second publish retry, gas/DHT sample accumulation and LED/buzzer logic remain. ADC divider correction 1.5, supply voltage 5.0, MQ-7 baseline 0.465 V, MQ-135 baseline 0.272 V, CO A=0.87/B=-0.554, molar mass 28.01 and molar volume 24.45 L, NH3 baseline 7.705 µg/m³/B=-0.442 are untouched. Host tests compare original calibration/sampling section hashes. DHT is actually DHT22 despite some old DHT11 comments; the configured sensor type was preserved.

The original firmware pauses sampling after a window closes until publishing succeeds, then starts a new window. That behavior remains, including existing blocking WiFi reconnect behavior. TLS handshakes can also delay loop execution; no strict realtime sampling guarantee is asserted. No heater control, temperature/pressure correction, calibration equation, warning threshold or gas validity range is changed.

## Transport and payload changes

`WiFiClientSecure` verifies a supplied root CA and broker hostname, without `setInsecure`. The firmware waits for an NTP clock before TLS certificate verification. NTP uses `pool.ntp.org` and `time.google.com`. Credentials are passed to authenticated MQTT connect. Device ID remains `node1`; current topic remains `aqi/node1/readings`. The public broker `broker.hivemq.com` is rejected, matching the Python worker. The user has now selected EMQX Cloud Serverless at `p0fb61ec.ala.asia-southeast1.emqxsl.com:8883`; private passwords and actual CA/connection validation remain pending. See [EMQX Render setup](../../docs/EMQX_RENDER_SETUP.md).

Payload now has `device_id`, `city: Mandalay`, `temperature`, `co_mg_m3`, `nh3_ug_m3`, `co_ppm`, `co_ratio`, `co_range_valid`, `co_valid`, `nh3_ratio`, `nh3_valid` and `humidity`. Original gas diagnostics, counts and local alert label remain. Explicit mass units are added. Nonfinite numbers serialize as JSON null, with invalid flags. A failed DHT reading does not invalidate otherwise valid gas observations. No synthetic readings are published.

Window-end UTC timestamp is frozen once on completion, not regenerated during retries. If a trustworthy clock is unavailable at completion, timestamp is null and a stable boot-random-ID/window-counter message ID permits reception-time ingestion. That ID is unchanged on retry. Original timestamp quality, sensor flags and diagnostic payload are retained by PostgreSQL. A later network retry does not falsely change measurement time when the clock was available. The worker rejects data older than its seven-day backfill limit or over 30 seconds in the future. Undated retained messages are rejected; publish remains non-retained.

**MQ-7 limitation:** the uploaded firmware declares CO valid only at 10–500 ppm. The earlier 1.388–1.734 mg/m³ examples correspond to about 1.21–1.51 ppm using the preserved conversion and therefore carry `co_range_valid=false`. The raw API stores/displays their audited values and validity; monitoring cards and AQI exclude them. Do not remove this flag to force an AQI. Sensor detection limits/calibration need separate hardware validation. NH3 calibration and MQ-135 selectivity also remain unverified. Mixed-source AQI remains provisional and potentially partial.

**Delivery limitation:** PubSubClient 2.8 publishes at QoS 0. Its successful return confirms a transport write, not broker acknowledgment or database commit. Worker subscription QoS 1 does not upgrade a QoS 0 publisher. This firmware preserves the working MQTT library and publish/reset behavior, so no lossless delivery claim is made. Broker authentication, TLS and database deduplication are implemented, but power loss or a connection break after send can lose a window. Reliable persistent QoS 1 publishing would require a separately validated client/queue change; no such change was silently made here.

## PlatformIO dependencies

ESP32 DevKitC V4 using PlatformIO board `esp32dev`:

- `espressif32@6.10.0` (Arduino framework)
- `knolleary/PubSubClient@2.8`
- `adafruit/DHT sensor library@1.4.6`
- `adafruit/Adafruit Unified Sensor@1.1.15`

WiFi, WiFiClientSecure, NTP/time and ESP random APIs are included in the ESP32 Arduino framework. No ArduinoJson dependency is needed; the actual C++ serializer is compiled and tested directly.

Install PlatformIO IDE or an isolated development environment (not deployment requirements):

```sh
python -m venv build/firmware-tools
build/firmware-tools/bin/pip install platformio==6.1.18
```

Choose ONE configuration route:

1. Copy `include/secrets.example.h` to ignored `include/secrets.h` and supply actual private broker hostname, WiFi settings, username/password and broker root CA PEM. Leave `MQTT_TLS_PORT=8883` and `MQTT_PUBLISH_TOPIC="aqi/node1/readings"` unless both ends are deliberately updated. The template is not usable credentials or a usable CA. An empty WiFi password permits your existing open WiFi.
2. Set local build environment variables. `configure_mqtt.py` writes an ignored mode-0600 `secrets.h`, without logging secret values. No operating-system environment variables exist on the running ESP32: values are consumed at build time and embedded in its flash image. Protect both the local header and firmware binary.

Environment names for option 2:

```text
WIFI_SSID=<your network>
WIFI_PASSWORD=<your WiFi password, or empty>
MQTT_HOST=p0fb61ec.ala.asia-southeast1.emqxsl.com
MQTT_PORT=8883
MQTT_USERNAME=<device publisher account>
MQTT_PASSWORD=<device publisher secret>
MQTT_TOPIC=aqi/node1/readings
MQTT_CA_FILE=<local path to the broker trusted root CA PEM>
```

With no `MQTT_HOST` in the local environment, the script preserves an existing manually configured `secrets.h`. With `MQTT_HOST` set, all required settings must be supplied. Do not place passwords in `platformio.ini`, shell history, Git, chat or Render source files. `secrets.h` and `.pio/` are ignored. The downloadable source archive excludes secrets and compiled binaries.

Then from the dashboard repository root:

```sh
build/firmware-tools/bin/pio run -d firmware/mandalay-esp32
# Only when you are ready to flash your actual connected board:
build/firmware-tools/bin/pio run -d firmware/mandalay-esp32 --target upload
build/firmware-tools/bin/pio device monitor -d firmware/mandalay-esp32
```

Build attempts here failed at the PlatformIO registry download with `HTTPClientError`/`ProxyError`; this environment cannot fetch that registry. The full ESP32 binary is therefore not verified. The actual serializer compiled on host with g++ C++11 and warning-as-error flags; its emitted JSON was passed through `mqtt_worker.Subscriber.on_message`, PostgreSQL persistence and latest API tests. Whole-board compile/flash and real TLS MQTT delivery remain to be completed on a machine with PlatformIO registry access and your real configuration.

## Render worker environment

Use the same host, port, CA trust and effective topic as the firmware; publisher and subscriber may use separate least-privilege broker accounts:

```text
DATABASE_URL=<existing PostgreSQL connection>
MANDALAY_DEVICE_IDS=node1
HARDWARE_DEVICE_ID=node1
HARDWARE_SAMPLE_SECONDS=900
HARDWARE_STALE_SECONDS=1800
MQTT_HOST=p0fb61ec.ala.asia-southeast1.emqxsl.com
MQTT_PORT=8883
MQTT_USERNAME=<subscriber account>
MQTT_PASSWORD=<subscriber secret>
MQTT_TOPIC_TEMPLATE=aqi/{device_id}/readings
MQTT_CLIENT_ID=mandalay-dashboard-subscriber
```

If the broker uses a publicly trusted CA, Python's default CA trust suffices; otherwise set worker `MQTT_CA_FILE` to a deployed CA PEM path. Firmware always needs the actual CA PEM. `MQTT_ALLOW_INSECURE_DEV` must not be enabled in Render. Web service needs the same database/device/cadence/staleness settings, not the MQTT credentials. Retain all existing prediction environment settings and scheduled modeled-data refresh.

Worker starts with `python mqtt_worker.py`, separately from Gunicorn. Broker ACLs should grant the device account publish permission only on `aqi/node1/readings` and worker subscribe permission on that topic. Neither a topic name nor a JSON device ID proves provenance on the old shared public broker. If switching to `aqi/mandalay/node1/readings`, simultaneously update firmware `MQTT_TOPIC`/`MQTT_PUBLISH_TOPIC` and worker `MQTT_TOPIC_TEMPLATE=aqi/mandalay/{device_id}/readings`.

## Remaining real-device steps

1. Use your confirmed EMQX hostname and obtain its actual trusted CA, publisher (`mandalay_esp32`) and subscriber (`mandalay_worker`) passwords and required authorization rules.
2. Configure firmware locally and matching Render environment settings; secrets are not included in this deliverable.
3. Compile, inspect the PlatformIO output, and flash your board when ready. Check NTP synchronization and authenticated TLS connection via the serial monitor.
4. Run the existing additive `flask --app wsgi migrate-hardware` before starting the worker. No new migration is introduced by this firmware task.
5. Receive an actual 15-minute window and inspect `/api/hardware/latest`, `/api/hardware/history?days=1` and `/api/monitoring/latest`. Confirm source, timestamp quality, validity, raw concentrations and hardware online status. Do not expect an eligible hardware CPCB index until sufficient covered hourly history exists, or when MQ-7 remains out of its declared range.
6. Verify disconnect/retry behavior and long-running sampling/calibration on the real board. Live connectivity has NOT been tested here.

Prediction application/models, dashboard prediction navigation, modeled refresh and Vadodara were not modified. No automatic push, merge, deployment or flash was performed.
