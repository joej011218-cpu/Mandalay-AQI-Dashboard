#pragma once
#include <math.h>
#include <stdio.h>
#include <stddef.h>

// Transport-only serialization; all gas equations remain in main.cpp.
struct WindowPayload {
    const char *messageId;
    const char *timestamp; // UTC ISO 8601 or empty when no trusted clock.
    const char *alert;
    unsigned long gasSamples, dhtSamples;
    float coMgM3, coPPM, coRaw, coVoltage, coRatio, coRelative;
    bool coRangeValid;
    float nh3UgM3, nh3Raw, nh3Voltage, nh3Ratio, nh3Relative;
    float temperature, humidity;
};

inline void finiteJson(char *out, size_t size, float value, int precision) {
    if (isfinite(value) && value >= 0)
        snprintf(out, size, "%.*f", precision, static_cast<double>(value));
    else
        snprintf(out, size, "null");
}

inline bool formatWindowPayload(char *out, size_t size, const WindowPayload &w) {
    // Ratios are metadata; never substitute them for mass measurements.
    char co[48], ppm[48], cr[48], cv[48], ratio[48], relative[48];
    char nh3[48], nr[48], nv[48], nratio[48], nrelative[48], temp[48], humidity[48];
    finiteJson(co,sizeof(co),w.coMgM3,3); finiteJson(ppm,sizeof(ppm),w.coPPM,3);
    finiteJson(cr,sizeof(cr),w.coRaw,1); finiteJson(cv,sizeof(cv),w.coVoltage,3);
    finiteJson(ratio,sizeof(ratio),w.coRatio,3); finiteJson(relative,sizeof(relative),w.coRelative,3);
    finiteJson(nh3,sizeof(nh3),w.nh3UgM3,2); finiteJson(nr,sizeof(nr),w.nh3Raw,1);
    finiteJson(nv,sizeof(nv),w.nh3Voltage,3); finiteJson(nratio,sizeof(nratio),w.nh3Ratio,3);
    finiteJson(nrelative,sizeof(nrelative),w.nh3Relative,3);
    const bool dhtValid = w.dhtSamples > 0 && isfinite(w.temperature) &&
        w.temperature >= -50 && w.temperature <= 100 && isfinite(w.humidity) &&
        w.humidity >= 0 && w.humidity <= 100;
    if (dhtValid) {
        snprintf(temp,sizeof(temp),"%.1f",static_cast<double>(w.temperature));
        snprintf(humidity,sizeof(humidity),"%.1f",static_cast<double>(w.humidity));
    } else {
        snprintf(temp,sizeof(temp),"null"); snprintf(humidity,sizeof(humidity),"null");
    }
    const bool coValid = w.gasSamples > 0 && isfinite(w.coMgM3) && w.coMgM3 >= 0 &&
        isfinite(w.coPPM) && w.coPPM >= 0 && isfinite(w.coRatio) && w.coRatio > 0;
    const bool nh3Valid = w.gasSamples > 0 && isfinite(w.nh3UgM3) && w.nh3UgM3 >= 0 &&
        isfinite(w.nh3Ratio) && w.nh3Ratio > 0;
    char stamp[48];
    if (w.timestamp && w.timestamp[0]) snprintf(stamp,sizeof(stamp),"\"%s\"",w.timestamp);
    else snprintf(stamp,sizeof(stamp),"null");
    const int written = snprintf(out,size,
        "{\"device_id\":\"node1\",\"city\":\"Mandalay\","
        "\"message_id\":\"%s\",\"timestamp\":%s,\"alert_level\":\"%s\","
        "\"window_minutes\":15,\"gas_samples\":%lu,\"dht_samples\":%lu,"
        "\"units\":{\"co_mg_m3\":\"mg/m3\",\"nh3_ug_m3\":\"ug/m3\"},"
        "\"co_mg_m3\":%s,\"co_ppm\":%s,\"co_raw\":%s,\"co_voltage\":%s,"
        "\"co_ratio\":%s,\"co_relative\":%s,\"co_range_valid\":%s,\"co_valid\":%s,"
        "\"nh3_ug_m3\":%s,\"nh3_raw\":%s,\"nh3_voltage\":%s,"
        "\"nh3_ratio\":%s,\"nh3_relative\":%s,\"nh3_valid\":%s,"
        "\"temperature\":%s,\"humidity\":%s,\"dht_valid\":%s}",
        w.messageId,stamp,w.alert,w.gasSamples,w.dhtSamples,
        co,ppm,cr,cv,ratio,relative,w.coRangeValid?"true":"false",coValid?"true":"false",
        nh3,nr,nv,nratio,nrelative,nh3Valid?"true":"false",temp,humidity,dhtValid?"true":"false");
    return written >= 0 && static_cast<size_t>(written) < size;
}
