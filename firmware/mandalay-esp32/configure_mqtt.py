"""PlatformIO pre-build: optional environment -> ignored local secrets.h.

No environment values are printed. A missing configuration never invents a host.
"""
from pathlib import Path
import json
import os


def configure(project_dir, values):
    target=Path(project_dir)/'include/secrets.h'
    # With no explicit MQTT_HOST, preserve a user's manually supplied secrets.h.
    if not values.get('MQTT_HOST'):
        if not target.exists():
            raise RuntimeError('Configure MQTT_HOST/WiFi/MQTT credentials and MQTT_CA_FILE, or copy include/secrets.example.h to include/secrets.h locally.')
        return
    required=('WIFI_SSID','MQTT_HOST','MQTT_USERNAME','MQTT_PASSWORD','MQTT_CA_FILE')
    if any(not values.get(key) for key in required):
        raise RuntimeError('Environment configuration needs WIFI_SSID, MQTT_HOST, MQTT_USERNAME, MQTT_PASSWORD and MQTT_CA_FILE; values are not logged.')
    host=values['MQTT_HOST']
    if host.lower().rstrip('.')=='broker.hivemq.com' or '://' in host or '/' in host or any(c.isspace() for c in host):
        raise RuntimeError('MQTT_HOST must be a private TLS broker hostname, without URL scheme or port.')
    port=int(values.get('MQTT_PORT','8883'))
    if not 1<=port<=65535:raise RuntimeError('Invalid MQTT_PORT')
    topic=values.get('MQTT_TOPIC','aqi/node1/readings')
    if not topic or any(c in topic for c in ('#','+','\0')):
        raise RuntimeError('An exact MQTT_TOPIC is required; configure the worker to match.')
    pem=Path(values['MQTT_CA_FILE']).read_text()
    if '-----BEGIN CERTIFICATE-----' not in pem or '-----END CERTIFICATE-----' not in pem:
        raise RuntimeError('MQTT_CA_FILE must contain the broker trusted root CA PEM.')
    fields={'WIFI_SSID':values['WIFI_SSID'],'WIFI_PASSWORD':values.get('WIFI_PASSWORD',''),
        'MQTT_HOST':host,'MQTT_USERNAME':values['MQTT_USERNAME'],'MQTT_PASSWORD':values['MQTT_PASSWORD'],
        'MQTT_PUBLISH_TOPIC':topic}
    if any('\0' in value for value in fields.values()):raise RuntimeError('Configuration contains a null character')
    content='#pragma once\n// GENERATED locally from environment; never commit.\n'
    for key,value in fields.items():content+=f'#define {key} {json.dumps(value,ensure_ascii=True)}\n'
    content+=f'#define MQTT_TLS_PORT {port}\n'
    content+='static const char MQTT_ROOT_CA[] = '+json.dumps(pem)+';\n'
    target.parent.mkdir(parents=True,exist_ok=True)
    # Configuration is local and may contain credentials; restrict file permissions.
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as output:output.write(content)
    target.chmod(0o600)


# PlatformIO/SCons provides Import; importing this module for tests has no side effects.
try:
    Import('env')
except NameError:
    pass
else:
    configure(env.subst('$PROJECT_DIR'),os.environ)
