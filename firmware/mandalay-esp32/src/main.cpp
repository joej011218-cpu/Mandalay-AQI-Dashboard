#include <Arduino.h>

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>
#include <string.h>
#include <esp_system.h>
#include "secrets.h"
#include "window_payload.h"

#include <PubSubClient.h>

#include <DHT.h>

#include <math.h>

// ============================================================

// WIFI AND MQTT: local secrets.h, never committed.
// ============================================================
const char *MQTT_BROKER = MQTT_HOST;
const uint16_t MQTT_PORT = MQTT_TLS_PORT;
const char *MQTT_TOPIC = MQTT_PUBLISH_TOPIC;
WiFiClientSecure wifiClient;
PubSubClient mqttClient(wifiClient);

// ============================================================

// SENSOR PINS

// ============================================================

#define MQ7_PIN 34

#define MQ135_PIN 35

#define DHT_PIN 4

#define DHT_TYPE DHT22

DHT dht(DHT_PIN, DHT_TYPE);

// ============================================================
// LOCAL LED + BUZZER ALERTS (illustrative, not official AQI)
// ============================================================
// LED anodes: GPIO -> 220-330 ohm resistor -> LED -> GND.
// Active buzzer: use a transistor driver, NOT direct GPIO power.
#define GREEN_LED_PIN 25
#define YELLOW_LED_PIN 26
#define RED_LED_PIN 27
#define BUZZER_PIN 23

// Provisional gas thresholds. Adjust only after validation.
// CO below 10 ppm is outside the declared MQ-7 valid range;
// such readings cannot establish that air is safe.
const float CO_WARNING_PPM = 20.0f;
const float CO_DANGER_PPM = 35.0f;
const float NH3_WARNING_UG_M3 = 200.0f;
const float NH3_DANGER_UG_M3 = 400.0f;

// 0=unknown/invalid, 1=normal (indicative), 2=warning, 3=danger
uint8_t alertLevel = 0;
unsigned long lastBuzzerToggleMs = 0;
bool buzzerOn = false;
const unsigned long BUZZER_ON_MS = 180;
const unsigned long BUZZER_OFF_MS = 820;

const char *alertName(uint8_t level)
{
    switch (level)
    {
    case 1:
        return "NORMAL";
    case 2:
        return "WARNING";
    case 3:
        return "DANGER";
    default:
        return "SENSOR_ERROR";
    }
}

void setAlertLevel(uint8_t level)
{
    if (alertLevel == level)
        return;
    alertLevel = level;
    buzzerOn = false;
    lastBuzzerToggleMs = millis();
    digitalWrite(BUZZER_PIN, LOW);
    digitalWrite(GREEN_LED_PIN, level == 1 ? HIGH : LOW);
    digitalWrite(YELLOW_LED_PIN, level == 2 ? HIGH : LOW);
    digitalWrite(RED_LED_PIN, level == 3 ? HIGH : LOW);
    Serial.print("Local gas alert: ");
    Serial.println(alertName(level));
}

// This evaluates an instantaneous, uncalibrated gas estimate.
// It is not a CPCB AQI classification or a safety certification.
void updateGasAlert(float coPpm, float nh3UgM3, bool valid)
{
    if (!valid || !isfinite(coPpm) || !isfinite(nh3UgM3) ||
        coPpm < 0 || nh3UgM3 < 0)
    {
        setAlertLevel(0);
        return;
    }
    const bool coInRange = coPpm >= 10.0f && coPpm <= 500.0f;
    if ((coInRange && coPpm >= CO_DANGER_PPM) ||
        nh3UgM3 >= NH3_DANGER_UG_M3)
    {
        setAlertLevel(3);
    }
    else if ((coInRange && coPpm >= CO_WARNING_PPM) ||
             nh3UgM3 >= NH3_WARNING_UG_M3)
    {
        setAlertLevel(2);
    }
    else
    {
        // CO outside the sensor's valid range is ignored, not assumed 0.
        setAlertLevel(1);
    }
}

// Called frequently; non-blocking. Red danger beeps; all others mute.
void serviceBuzzer()
{
    if (alertLevel != 3)
    {
        if (buzzerOn)
        {
            buzzerOn = false;
            digitalWrite(BUZZER_PIN, LOW);
        }
        return;
    }
    unsigned long now = millis();
    unsigned long interval = buzzerOn ? BUZZER_ON_MS : BUZZER_OFF_MS;
    if (now - lastBuzzerToggleMs >= interval)
    {
        buzzerOn = !buzzerOn;
        lastBuzzerToggleMs = now;
        digitalWrite(BUZZER_PIN, buzzerOn ? HIGH : LOW);
    }
}

// ============================================================

// SAMPLING

// ============================================================

// Sensor observation every 10 seconds

const unsigned long SAMPLE_INTERVAL_MS =

    10UL * 1000UL;

// Publish one averaged record every 15 minutes

const unsigned long WINDOW_INTERVAL_MS =

    15UL * 60UL * 1000UL;

// If publishing fails, retry every 30 seconds

const unsigned long MQTT_RETRY_MS =

    30UL * 1000UL;

// Number of ADC measurements averaged inside each observation

const int ADC_SAMPLES = 20;

// ============================================================

// MQ CIRCUIT

// ============================================================

const float VC = 5.0;

// External divider:

//

// MQ AO ---- 10k ----+---- ESP32 ADC

//                    |

//                   20k

//                    |

//                   GND

//

// ESP32 sees approximately 2/3 of MQ AO.

// Therefore recover module voltage with x1.5.

const float DIVIDER_CORRECTION = 1.5;

// ============================================================

// FINAL ELECTRICAL BASELINES

// ============================================================

const float MQ7_BASELINE_V =

    0.465;

const float MQ135_BASELINE_V =

    0.272;

// ============================================================

// MQ-7 CO MODEL

// ============================================================

const float CO_A =

    0.87;

const float CO_B =

    -0.554;

const float CO_MOLAR_MASS =

    28.01;

const float MOLAR_VOLUME_L =

    24.45;

const float MQ7_MIN_PPM =

    10.0;

const float MQ7_MAX_PPM =

    500.0;

// ============================================================

// MQ-135 NH3 MODEL

// ============================================================

// CPCB-referenced baseline

const float NH3_BASELINE_UG_M3 =

    7.705;

const float NH3_B =

    -0.442;

// ============================================================

// STRUCTURES

// ============================================================

struct AnalogReading

{

    float raw;

    float voltage;
};

struct GasReading

{

    bool valid;

    float coRaw;

    float coVoltage;

    float coRatio;

    float coRelative;

    float coPPM;

    float coMgM3;

    float nh3Raw;

    float nh3Voltage;

    float nh3Ratio;

    float nh3Relative;

    float nh3UgM3;
};

struct DHTReading

{

    bool valid;

    float temperature;

    float humidity;
};

// ============================================================

// ACCUMULATORS

// ============================================================

// CO

double sumCoMgM3 = 0;

double sumCoPPM = 0;

double sumCoRaw = 0;

double sumCoVoltage = 0;

double sumCoRatio = 0;

double sumCoRelative = 0;

// NH3

double sumNh3UgM3 = 0;

double sumNh3Raw = 0;

double sumNh3Voltage = 0;

double sumNh3Ratio = 0;

double sumNh3Relative = 0;

// DHT11

double sumTemperature = 0;

double sumHumidity = 0;

// Counts

unsigned long gasSampleCount = 0;

unsigned long dhtSampleCount = 0;

// Timing

unsigned long lastSampleMs = 0;

unsigned long windowStartMs = 0;

unsigned long lastPublishAttemptMs = 0;

bool windowReady = false;

// Identity is captured once when a window closes and reused on retries.
char bootId[40] = {};
char windowMessageId[80] = {};
char windowTimestamp[32] = {};
unsigned long windowSequence = 0;

bool trustedClockReady() {
    return time(nullptr) >= 1704067200; // At least 2024; backend also checks clock bounds.
}

void captureWindowIdentity() {
    snprintf(windowMessageId,sizeof(windowMessageId),"%s:%lu",bootId,windowSequence);
    windowTimestamp[0] = '\0';
    if (trustedClockReady()) {
        const time_t stamp = time(nullptr);
        struct tm utc;
        if (gmtime_r(&stamp,&utc))
            strftime(windowTimestamp,sizeof(windowTimestamp),"%Y-%m-%dT%H:%M:%SZ",&utc);
    }
}

// ============================================================

// READ ANALOG SENSOR

// ============================================================

AnalogReading readAnalogSensor(int pin)

{

    unsigned long rawTotal = 0;

    unsigned long mvTotal = 0;

    for (int i = 0; i < ADC_SAMPLES; i++)

    {

        rawTotal += analogRead(pin);

        mvTotal += analogReadMilliVolts(pin);

        delay(5);
    }

    AnalogReading reading;

    reading.raw =

        (float)rawTotal / ADC_SAMPLES;

    float adcVoltage =

        ((float)mvTotal / ADC_SAMPLES) / 1000.0;

    reading.voltage =

        adcVoltage * DIVIDER_CORRECTION;

    return reading;
}

// ============================================================

// CALCULATE Rs/R0

// ============================================================

float calculateRsR0(

    float currentVoltage,

    float baselineVoltage)

{

    if (

        currentVoltage <= 0.001 ||

        baselineVoltage <= 0.001 ||

        currentVoltage >= VC ||

        baselineVoltage >= VC)

    {

        return NAN;
    }

    float rsFactor =

        (VC / currentVoltage) - 1.0;

    float r0Factor =

        (VC / baselineVoltage) - 1.0;

    if (r0Factor <= 0)

    {

        return NAN;
    }

    return rsFactor / r0Factor;
}

// ============================================================

// CO CALCULATION

// ============================================================

float calculateCOPPM(float ratio)

{

    if (isnan(ratio) || ratio <= 0)

    {

        return NAN;
    }

    return pow(

        ratio / CO_A,

        1.0 / CO_B);
}

float coPpmToMgM3(float ppm)

{

    if (isnan(ppm) || ppm < 0)

    {

        return NAN;
    }

    return ppm *

           CO_MOLAR_MASS /

           MOLAR_VOLUME_L;
}

// ============================================================

// NH3 CALCULATION

// ============================================================

float calculateNH3UgM3(float ratio)

{

    if (isnan(ratio) || ratio <= 0)

    {

        return NAN;
    }

    return NH3_BASELINE_UG_M3 *

           pow(

               ratio,

               1.0 / NH3_B);
}

// ============================================================

// READ GAS SENSORS

// ============================================================

GasReading readGasSensors()

{

    GasReading result = {};

    AnalogReading mq7 =

        readAnalogSensor(MQ7_PIN);

    AnalogReading mq135 =

        readAnalogSensor(MQ135_PIN);

    // MQ-7

    result.coRaw =

        mq7.raw;

    result.coVoltage =

        mq7.voltage;

    result.coRatio =

        calculateRsR0(

            mq7.voltage,

            MQ7_BASELINE_V);

    if (

        !isnan(result.coRatio) &&

        result.coRatio > 0)

    {

        result.coRelative =

            1.0 / result.coRatio;
    }

    else

    {

        result.coRelative =

            NAN;
    }

    result.coPPM =

        calculateCOPPM(

            result.coRatio);

    result.coMgM3 =

        coPpmToMgM3(

            result.coPPM);

    // MQ-135

    result.nh3Raw =

        mq135.raw;

    result.nh3Voltage =

        mq135.voltage;

    result.nh3Ratio =

        calculateRsR0(

            mq135.voltage,

            MQ135_BASELINE_V);

    if (

        !isnan(result.nh3Ratio) &&

        result.nh3Ratio > 0)

    {

        result.nh3Relative =

            1.0 / result.nh3Ratio;
    }

    else

    {

        result.nh3Relative =

            NAN;
    }

    result.nh3UgM3 =

        calculateNH3UgM3(

            result.nh3Ratio);

    result.valid =

        !isnan(result.coMgM3) &&

        !isnan(result.coPPM) &&

        !isnan(result.coRatio) &&

        !isnan(result.nh3UgM3) &&

        !isnan(result.nh3Ratio);

    return result;
}

// ============================================================

// READ DHT11

// ============================================================

DHTReading readDHT22()

{

    DHTReading reading = {};

    reading.humidity =

        dht.readHumidity();

    reading.temperature =

        dht.readTemperature();

    reading.valid =

        !isnan(reading.temperature) &&

        !isnan(reading.humidity);

    return reading;
}

// ============================================================

// ADD SAMPLE

// ============================================================

void addGasSample(const GasReading &r)

{

    if (!r.valid)

    {

        Serial.println(

            "Invalid gas reading - skipped.");

        return;
    }

    sumCoMgM3 += r.coMgM3;

    sumCoPPM += r.coPPM;

    sumCoRaw += r.coRaw;

    sumCoVoltage += r.coVoltage;

    sumCoRatio += r.coRatio;

    sumCoRelative += r.coRelative;

    sumNh3UgM3 += r.nh3UgM3;

    sumNh3Raw += r.nh3Raw;

    sumNh3Voltage += r.nh3Voltage;

    sumNh3Ratio += r.nh3Ratio;

    sumNh3Relative += r.nh3Relative;

    gasSampleCount++;
}

void addDHTSample(const DHTReading &r)

{

    if (!r.valid)

    {

        Serial.println(

            "DHT22 reading failed - skipped.");

        return;
    }

    sumTemperature +=

        r.temperature;

    sumHumidity +=

        r.humidity;

    dhtSampleCount++;
}

// ============================================================

// RESET AVERAGING WINDOW

// ============================================================

void resetWindow()

{

    sumCoMgM3 = 0;

    sumCoPPM = 0;

    sumCoRaw = 0;

    sumCoVoltage = 0;

    sumCoRatio = 0;

    sumCoRelative = 0;

    sumNh3UgM3 = 0;

    sumNh3Raw = 0;

    sumNh3Voltage = 0;

    sumNh3Ratio = 0;

    sumNh3Relative = 0;

    sumTemperature = 0;

    sumHumidity = 0;

    gasSampleCount = 0;

    dhtSampleCount = 0;

    windowReady = false;
    ++windowSequence;
    windowMessageId[0] = '\0';
    windowTimestamp[0] = '\0';

    lastPublishAttemptMs = 0;

    windowStartMs =

        millis();

    lastSampleMs =

        millis() - SAMPLE_INTERVAL_MS;
}

// ============================================================

// WIFI - OPEN NETWORK

// ============================================================

void connectWiFi()

{

    if (WiFi.status() == WL_CONNECTED)

    {

        return;
    }

    Serial.println();

    Serial.print(

        "Connecting to WiFi: ");

    Serial.println(

        WIFI_SSID);

    WiFi.mode(WIFI_STA);

    // Network details come from local secrets.h.

    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    unsigned long started =

        millis();

    while (

        WiFi.status() != WL_CONNECTED &&

        millis() - started < 20000)

    {

        Serial.print(".");

        delay(500);
    }

    Serial.println();

    if (WiFi.status() == WL_CONNECTED)

    {

        Serial.println(

            "WiFi connected.");

        Serial.print(

            "ESP32 IP: ");

        Serial.println(

            WiFi.localIP());
    }

    else

    {

        Serial.println(

            "WiFi connection failed.");
    }
}

// ============================================================

// MQTT

// ============================================================

bool connectMQTT()

{

    if (mqttClient.connected())

    {

        return true;
    }

    if (WiFi.status() != WL_CONNECTED)

    {

        return false;
    }

    Serial.print(

        "Connecting to MQTT...");

    String clientId =

        "AQI-ESP32-";

    uint64_t chipId =

        ESP.getEfuseMac();

    clientId +=

        String(

            (uint32_t)(chipId & 0xFFFFFFFF),

            HEX);

    // TLS validates certificate chain and hostname. Never use setInsecure().
    if (!trustedClockReady()) {
        Serial.println(" waiting for NTP clock before verified TLS.");
        return false;
    }
    if (!MQTT_USERNAME[0] || !MQTT_PASSWORD[0] ||
        strstr(MQTT_HOST,"YOUR_") || strstr(MQTT_ROOT_CA,"REPLACE_") ||
        strcmp(MQTT_HOST,"broker.hivemq.com") == 0) {
        Serial.println(" configure a private TLS broker and local secrets.h first.");
        return false;
    }
    bool ok = mqttClient.connect(clientId.c_str(),MQTT_USERNAME,MQTT_PASSWORD);

    if (ok)

    {

        Serial.println(

            " connected.");
    }

    else

    {

        Serial.print(

            " failed, state=");

        Serial.println(

            mqttClient.state());
    }

    return ok;
}

// ============================================================

// PRINT CURRENT SAMPLE

// ============================================================

void printSample(

    const GasReading &gas,

    const DHTReading &dhtReading)

{

    Serial.println();

    Serial.println(

        "---------- CURRENT SAMPLE ----------");

    if (gas.valid)

    {

        Serial.print("CO          : ");

        Serial.print(gas.coMgM3, 3);

        Serial.println(" mg/m3");

        Serial.print("NH3         : ");

        Serial.print(gas.nh3UgM3, 2);

        Serial.println(" ug/m3");
    }

    else

    {

        Serial.println(

            "Gas sensors : invalid");
    }

    if (dhtReading.valid)

    {

        Serial.print("Temperature : ");

        Serial.print(

            dhtReading.temperature,

            1);

        Serial.println(" C");

        Serial.print("Humidity    : ");

        Serial.print(

            dhtReading.humidity,

            1);

        Serial.println(" %");
    }

    else

    {

        Serial.println(

            "DHT22       : invalid");
    }

    Serial.print(

        "Gas samples : ");

    Serial.println(

        gasSampleCount);

    Serial.print(

        "DHT samples : ");

    Serial.println(

        dhtSampleCount);
}

// ============================================================

// PUBLISH 15-MINUTE AVERAGE

// ============================================================

bool publishWindow()

{

    if (gasSampleCount == 0)

    {

        Serial.println(

            "No valid gas samples available.");

        return false;
    }

    // --------------------------------------------------------

    // Averages

    // --------------------------------------------------------

    float coMgM3 =

        sumCoMgM3 /

        gasSampleCount;

    float coPPM =

        sumCoPPM /

        gasSampleCount;

    float coRaw =

        sumCoRaw /

        gasSampleCount;

    float coVoltage =

        sumCoVoltage /

        gasSampleCount;

    float coRatio =

        sumCoRatio /

        gasSampleCount;

    float coRelative =

        sumCoRelative /

        gasSampleCount;

    float nh3UgM3 =

        sumNh3UgM3 /

        gasSampleCount;

    float nh3Raw =

        sumNh3Raw /

        gasSampleCount;

    float nh3Voltage =

        sumNh3Voltage /

        gasSampleCount;

    float nh3Ratio =

        sumNh3Ratio /

        gasSampleCount;

    float nh3Relative =

        sumNh3Relative /

        gasSampleCount;

    bool coRangeValid =

        coPPM >= MQ7_MIN_PPM &&

        coPPM <= MQ7_MAX_PPM;

    float temperature = NAN;

    float humidity = NAN;

    if (dhtSampleCount > 0)

    {

        temperature =

            sumTemperature /

            dhtSampleCount;

        humidity =

            sumHumidity /

            dhtSampleCount;
    }

    // --------------------------------------------------------

    // JSON

    // --------------------------------------------------------

    char payload[1536];
    WindowPayload record = {};
    record.messageId = windowMessageId;
    record.timestamp = windowTimestamp;
    record.alert = alertName(alertLevel);
    record.gasSamples = gasSampleCount;
    record.dhtSamples = dhtSampleCount;
    record.coMgM3 = coMgM3; record.coPPM = coPPM;
    record.coRaw = coRaw; record.coVoltage = coVoltage;
    record.coRatio = coRatio; record.coRelative = coRelative;
    record.coRangeValid = coRangeValid;
    record.nh3UgM3 = nh3UgM3; record.nh3Raw = nh3Raw;
    record.nh3Voltage = nh3Voltage; record.nh3Ratio = nh3Ratio;
    record.nh3Relative = nh3Relative;
    record.temperature = temperature; record.humidity = humidity;
    if (!formatWindowPayload(payload,sizeof(payload),record)) {
        Serial.println("MQTT payload too large; window retained for retry.");
        return false;
    }

    // --------------------------------------------------------

    // Serial output

    // --------------------------------------------------------

    Serial.println();

    Serial.println(

        "========================================");

    Serial.println(

        "15-MINUTE AVERAGE READY");

    Serial.println(

        "========================================");

    Serial.print("CO          : ");

    Serial.print(coMgM3, 3);

    Serial.println(" mg/m3");

    Serial.print("NH3         : ");

    Serial.print(nh3UgM3, 2);

    Serial.println(" ug/m3");

    if (dhtSampleCount > 0)

    {

        Serial.print("Temperature : ");

        Serial.print(temperature, 1);

        Serial.println(" C");

        Serial.print("Humidity    : ");

        Serial.print(humidity, 1);

        Serial.println(" %");
    }

    Serial.print("Gas samples : ");

    Serial.println(gasSampleCount);

    Serial.print("DHT samples : ");

    Serial.println(dhtSampleCount);

    Serial.println();

    Serial.print(

        "MQTT JSON: ");

    Serial.println(

        payload);

    // --------------------------------------------------------

    // Network

    // --------------------------------------------------------

    if (WiFi.status() != WL_CONNECTED)

    {

        connectWiFi();
    }

    if (!mqttClient.connected())

    {

        connectMQTT();
    }

    if (!mqttClient.connected())

    {

        Serial.println(

            "MQTT Publish: FAILED - not connected");

        return false;
    }

    bool ok =

        mqttClient.publish(

            MQTT_TOPIC,

            payload,

            false);

    if (ok)

    {

        Serial.println(

            "MQTT Publish: sent (QoS 0; broker persistence not confirmed)");
    }

    else

    {

        Serial.println(

            "MQTT Publish: FAILED");
    }

    return ok;
}

// ============================================================

// SETUP

// ============================================================

void setup()

{

    Serial.begin(115200);

    delay(2000);

    // ADC

    analogReadResolution(12);

    analogSetPinAttenuation(

        MQ7_PIN,

        ADC_11db);

    analogSetPinAttenuation(

        MQ135_PIN,

        ADC_11db);

    pinMode(

        MQ7_PIN,

        INPUT);

    pinMode(

        MQ135_PIN,

        INPUT);

    // DHT11

    dht.begin();

    Serial.println();

    Serial.println(

        "========================================");

    Serial.println(

        "AIoT SENSOR NODE");

    Serial.println(

        "MQ-7 + MQ-135 + DHT22");

    Serial.println(

        "========================================");

    Serial.println(

        "Sampling every 10 seconds");

    Serial.println(

        "Publishing every 15 minutes");

    pinMode(GREEN_LED_PIN, OUTPUT);
    pinMode(YELLOW_LED_PIN, OUTPUT);
    pinMode(RED_LED_PIN, OUTPUT);
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(GREEN_LED_PIN, LOW);
    digitalWrite(YELLOW_LED_PIN, LOW);
    digitalWrite(RED_LED_PIN, LOW);
    digitalWrite(BUZZER_PIN, LOW);

    // Network: CA verification and UTC NTP, without changing sampling intervals.
    snprintf(bootId,sizeof(bootId),"%08lx-%08lx-%08lx",
        (unsigned long)(ESP.getEfuseMac() & 0xFFFFFFFF),
        (unsigned long)esp_random(),(unsigned long)esp_random());
    wifiClient.setCACert(MQTT_ROOT_CA);
    wifiClient.setHandshakeTimeout(8);
    connectWiFi();
    configTime(0,0,"pool.ntp.org","time.google.com");

    mqttClient.setServer(

        MQTT_BROKER,

        MQTT_PORT);

    // Buffer covers payload plus MQTT topic/header, not payload alone.
    mqttClient.setBufferSize(2048);
    mqttClient.setSocketTimeout(8);

    connectMQTT();

    resetWindow();
}

// ============================================================

// LOOP

// ============================================================

void loop()

{

    unsigned long now =

        millis();

    // Keep MQTT alive

    if (

        WiFi.status() == WL_CONNECTED &&

        mqttClient.connected())

    {

        mqttClient.loop();
    }

    // --------------------------------------------------------

    // Sample while current 15-minute window is open

    // --------------------------------------------------------

    if (

        !windowReady &&

        now - lastSampleMs >=

            SAMPLE_INTERVAL_MS)

    {

        lastSampleMs =

            now;

        GasReading gas =

            readGasSensors();

        DHTReading dhtReading =

            readDHT22();

        addGasSample(

            gas);

        addDHTSample(

            dhtReading);

        printSample(

            gas,

            dhtReading);

        const float MQ7_MIN_PPM = 10.0f;
        const float MQ7_MAX_PPM = 500.0f;

        updateGasAlert(gas.coPPM, gas.nh3UgM3, gas.valid);
    }

    // --------------------------------------------------------

    // Finish 15-minute window

    // --------------------------------------------------------

    if (

        !windowReady &&

        now - windowStartMs >=

            WINDOW_INTERVAL_MS)

    {

        windowReady =

            true;
        captureWindowIdentity();

        Serial.println();

        Serial.println(

            "15-minute window complete.");
    }

    // --------------------------------------------------------

    // Publish completed window

    // --------------------------------------------------------

    if (windowReady)

    {

        if (

            lastPublishAttemptMs == 0 ||

            now - lastPublishAttemptMs >=

                MQTT_RETRY_MS)

        {

            lastPublishAttemptMs =

                now;

            bool published =

                publishWindow();

            if (published)

            {

                resetWindow();
            }
        }
    }

    // --------------------------------------------------------

    // Network recovery

    // --------------------------------------------------------

    static unsigned long

        lastNetworkCheckMs = 0;

    if (

        now - lastNetworkCheckMs >=

        30000)

    {

        lastNetworkCheckMs =

            now;

        if (

            WiFi.status() !=

            WL_CONNECTED)

        {

            connectWiFi();
        }

        if (

            WiFi.status() ==

                WL_CONNECTED &&

            !mqttClient.connected())

        {

            connectMQTT();
        }
    }

    serviceBuzzer();

    delay(10);
}