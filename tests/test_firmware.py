"""Compile the actual firmware serializer and check its JSON contract on host."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import pytest
from mqtt_worker import decode

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='session')
def firmware_payload(tmp_path_factory):
    compiler=shutil.which('g++')
    if compiler is None:pytest.skip('g++ is required to test the C++ firmware serializer')
    out=tmp_path_factory.mktemp('firmware')/'payload'
    subprocess.run([compiler,'-std=c++11','-Wall','-Wextra','-Werror',
        '-I',str(ROOT/'firmware/mandalay-esp32/include'),
        str(ROOT/'tests/firmware_contract_harness.cpp'),'-o',str(out)],check=True,capture_output=True)
    def emit(stamp='',mode='default'):
        raw=subprocess.check_output([str(out),stamp,mode])
        return decode(raw)
    return emit

@pytest.mark.parametrize('mode',['default','valid-co','no-dht','nonfinite'])
def test_actual_cpp_payload_has_backend_fields_and_strict_json(firmware_payload,mode):
    p=firmware_payload('2026-10-11T00:00:00Z',mode)
    assert p['device_id']=='node1' and p['city']=='Mandalay'
    assert p['units']=={'co_mg_m3':'mg/m3','nh3_ug_m3':'ug/m3'}
    assert p['timestamp']=='2026-10-11T00:00:00Z' and p['message_id']=='test-boot:1'
    assert 'node_id' not in p and 'temp' not in p
    assert p['window_minutes']==15 and p['gas_samples']==90
    if mode=='default':
        assert p['co_mg_m3']==1.5 and not p['co_range_valid']
        assert p['nh3_ug_m3']==1000 and p['nh3_valid']
    if mode=='no-dht':assert p['temperature'] is None and p['humidity'] is None and not p['dht_valid']
    if mode=='nonfinite':
        assert p['co_mg_m3'] is None and p['co_ratio'] is None and not p['co_valid']
        assert p['nh3_ug_m3'] is None and not p['nh3_valid']


def test_no_clock_payload_keeps_stable_id(firmware_payload):
    first=firmware_payload();second=firmware_payload()
    assert first['timestamp'] is None
    assert first==second


def test_uploaded_calibration_and_sampling_sections_preserved():
    source=(ROOT/'firmware/mandalay-esp32/src/main.cpp').read_text()
    manifest=json.loads((ROOT/'firmware/mandalay-esp32/inspection.json').read_text())
    for block in manifest['preserved_sections']:
        start=source.index(block['start']);end=source.index(block['end'],start)
        assert hashlib.sha256(source[start:end].encode()).hexdigest()==block['sha256']


@pytest.fixture
def configure_firmware():
    import runpy
    return runpy.run_path(str(ROOT/'firmware/mandalay-esp32/configure_mqtt.py'))['configure']


def test_environment_header_has_no_invented_defaults_or_secret_logging(tmp_path,configure_firmware,capsys):
    ca=tmp_path/'ca.pem';ca.write_text('-----BEGIN CERTIFICATE-----\ntest-only\n-----END CERTIFICATE-----\n')
    values={'WIFI_SSID':'test-ssid','MQTT_HOST':'private-mqtt.example','MQTT_USERNAME':'test-user',
        'MQTT_PASSWORD':'test-only-password','MQTT_CA_FILE':str(ca)}
    configure_firmware(tmp_path,values)
    header=tmp_path/'include/secrets.h'
    data=header.read_text()
    assert '#define MQTT_TLS_PORT 8883' in data
    assert '#define MQTT_PUBLISH_TOPIC "aqi/node1/readings"' in data
    assert '#define MQTT_HOST "private-mqtt.example"' in data
    assert header.stat().st_mode & 0o777==0o600
    configure_firmware(tmp_path,{})
    assert header.read_text()==data
    assert capsys.readouterr().out==''


def test_environment_config_requires_private_broker_and_real_settings(tmp_path,configure_firmware):
    with pytest.raises(RuntimeError):configure_firmware(tmp_path,{})
    with pytest.raises(RuntimeError):configure_firmware(tmp_path,{'MQTT_HOST':'private-mqtt.example'})
    values={'WIFI_SSID':'test','MQTT_HOST':'broker.hivemq.com','MQTT_USERNAME':'test',
        'MQTT_PASSWORD':'test','MQTT_CA_FILE':str(tmp_path/'missing.pem')}
    with pytest.raises(RuntimeError,match='private TLS'):configure_firmware(tmp_path,values)
