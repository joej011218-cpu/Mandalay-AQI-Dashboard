// Host-only transport test. Never flashed; no database or hardware writes.
#include "window_payload.h"
#include <string.h>
#include <stdlib.h>
#include <iostream>
int main(int argc,char **argv) {
    WindowPayload w = {};
    w.messageId = "test-boot:1";
    w.timestamp = argc > 1 ? argv[1] : "";
    w.alert = "DANGER";
    w.gasSamples = 90; w.dhtSamples = 90;
    w.coMgM3 = 1.5f; w.coPPM = 1.309f;
    w.coRaw = 450; w.coVoltage = 0.5f; w.coRatio = 0.75f; w.coRelative = 1.333f;
    w.coRangeValid = false; // Uploaded firmware's MQ-7 range excludes this reading.
    w.nh3UgM3 = 1000; w.nh3Raw = 900; w.nh3Voltage = 1.2f;
    w.nh3Ratio = 0.12f; w.nh3Relative = 8.333f;
    w.temperature = 27.8f; w.humidity = 44;
    if (argc > 2 && strcmp(argv[2],"valid-co") == 0) {
        w.coMgM3 = 34.369f; w.coPPM = 30; w.coRangeValid = true;
    }
    if (argc > 2 && strcmp(argv[2],"no-dht") == 0) {
        w.dhtSamples = 0; w.temperature = NAN; w.humidity = NAN;
    }
    if (argc > 2 && strcmp(argv[2],"nonfinite") == 0) {
        w.coMgM3 = INFINITY; w.coPPM = NAN; w.coRatio = INFINITY;
        w.nh3UgM3 = NAN; w.nh3Ratio = NAN;
    }
    char payload[1536];
    if (!formatWindowPayload(payload,sizeof(payload),w)) return 1;
    char small[10];
    if (formatWindowPayload(small,sizeof(small),w)) return 2;
    std::cout << payload;
}
