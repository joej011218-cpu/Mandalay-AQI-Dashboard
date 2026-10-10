#pragma once
// Copy to secrets.h locally. Never commit the populated file.
#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD" // Empty is permitted for your existing open WiFi.
#define MQTT_HOST "YOUR_PRIVATE_TLS_BROKER_HOST"
#define MQTT_USERNAME "YOUR_DEVICE_PUBLISH_USERNAME"
#define MQTT_PASSWORD "YOUR_DEVICE_PUBLISH_PASSWORD"
#define MQTT_TLS_PORT 8883
#define MQTT_PUBLISH_TOPIC "aqi/node1/readings"
// Obtain the broker's actual trusted root CA PEM from its provider.
// Public certificate only: never put a private key here.
static const char MQTT_ROOT_CA[] = R"PEM(-----BEGIN CERTIFICATE-----
REPLACE_WITH_THE_BROKERS_ACTUAL_ROOT_CA_PEM
-----END CERTIFICATE-----
)PEM";
