"""HTTP-to-real-PostgreSQL ingestion, no broker or production database needed."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import db
from hardware import HardwareObservation, hardware_status, ingest
import hardware_webhook as webhook
from test_hardware import hw_app, payload

AUTH={'Authorization':'Bearer test-webhook-secret'}
PATH='/api/hardware/ingest'


@pytest.fixture
def client(hw_app,monkeypatch):
    monkeypatch.setenv('HARDWARE_WEBHOOK_TOKEN','test-webhook-secret')
    monkeypatch.setenv('HARDWARE_TRANSPORT','webhook')
    return hw_app.test_client()


def envelope(**changes):
    p=payload(datetime.now(timezone.utc),message_id='boot-1:1',co_range_valid=False,dht_valid=True)
    p.update(changes)
    return dict(topic='aqi/mandalay-node1/readings',username='mandalay_esp32',retained=False,payload=p)


def count():return db.session.scalar(select(func.count()).select_from(HardwareObservation))


def test_authentication_fails_before_parsing(client,monkeypatch):
    spy=Mock(wraps=webhook.hmac.compare_digest);monkeypatch.setattr(webhook.hmac,'compare_digest',spy)
    for value in ('','wrong','tést'):
        r=client.post(PATH,data='invalid',headers={'Authorization':value})
        assert r.status_code==401
    assert spy.call_count==3 and count()==0
    monkeypatch.delenv('HARDWARE_WEBHOOK_TOKEN')
    assert client.post(PATH,headers=AUTH).status_code==503


def test_persist_flags_source_status_and_duplicate_id(client):
    body=envelope()
    r=client.post(PATH,json=body,headers=AUTH)
    assert r.status_code==201 and r.json['status']=='stored'
    row=db.session.scalar(select(HardwareObservation))
    assert row.original_payload==body['payload']
    assert row.validity['co_range_valid'] is False and not row.co_valid
    assert row.validity['dht_valid'] is True and row.nh3_valid
    body['payload']['timestamp']=(datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat()
    body['payload']['co_mg_m3']=20  # Same ID never overwrites the original.
    r=client.post(PATH,json=body,headers=AUTH)
    assert r.status_code==200 and r.json['status']=='duplicate' and count()==1
    state=hardware_status()
    assert state['status']=='online' and state['transport']=='webhook'
    assert state['mqtt_connected'] is None and not state['subscriber_running']
    latest=client.get('/api/monitoring/latest').json['data']
    assert latest['measurements']['carbon_monoxide']['value'] is None
    assert latest['measurements']['carbon_monoxide']['status']=='invalid'
    assert latest['measurements']['ammonia']['source']=='ESP32'


@pytest.mark.parametrize('flag',['test','synthetic'])
def test_synthetic_rejected_without_retry_loop(client,flag):
    body=envelope(**{flag:True});body['payload'].pop('message_id')
    r=client.post(PATH,json=body,headers=AUTH)
    assert r.status_code==200 and r.json==dict(status='rejected',reason='synthetic_message',stored=False)
    assert count()==0 and hardware_status()['status']=='offline'


@pytest.mark.parametrize('change',[
    {'message_id':None},{'message_id':''},{'message_id':'x'*101},
    {'device_id':'vadodara'},{'city':'Vadodara'},{'co_range_valid':'false'},
    {'nh3_ug_m3':-1},{'test':'true'},
])
def test_invalid_measurements_not_stored(client,change):
    assert client.post(PATH,json=envelope(**change),headers=AUTH).status_code==422
    assert count()==0


def test_envelope_and_body_validation(client):
    for key,value in [('topic','aqi/other/readings'),('username','other'),('retained','false')]:
        body=envelope();body[key]=value
        assert client.post(PATH,json=body,headers=AUTH).status_code==422
    assert client.post(PATH,data='{}',headers=AUTH).status_code==415
    for raw in ('{"payload":{},"payload":{}}','{"payload":NaN}',
                '{"payload":{"extra":1e400}}','not-json'):
        assert client.post(PATH,data=raw,content_type='application/json',headers=AUTH).status_code==422
    assert client.post(PATH,data=' '*16385,content_type='application/json',headers=AUTH).status_code==413
    body=envelope();body['payload']=json.dumps(body['payload'])
    assert client.post(PATH,json=body,headers=AUTH).status_code==201


def test_database_failure_is_retryable_and_sanitized(client,monkeypatch,caplog):
    error=OperationalError('private test-webhook-secret',{},RuntimeError('private'))
    monkeypatch.setattr(webhook,'ingest',Mock(side_effect=error))
    r=client.post(PATH,json=envelope(),headers=AUTH)
    assert r.status_code==503 and r.headers['Retry-After']=='10' and count()==0
    assert 'test-webhook-secret' not in caplog.text+r.get_data(as_text=True)


def test_duplicate_missing_clock_keeps_original_reception_time(client):
    body=envelope(timestamp=None)
    assert client.post(PATH,json=body,headers=AUTH).status_code==201
    first=db.session.scalar(select(HardwareObservation)).timestamp
    assert client.post(PATH,json=body,headers=AUTH).status_code==200
    assert db.session.scalar(select(HardwareObservation)).timestamp==first and count()==1


def test_deduplicates_existing_timestamp_keyed_rows(client):
    body=envelope();p=body['payload'];ingest(p)
    row=db.session.scalar(select(HardwareObservation))
    row.event_key='legacy-timestamp-key';db.session.commit()
    assert client.post(PATH,json=body,headers=AUTH).status_code==200
    assert count()==1


def test_concurrent_http_retries_store_once(client,hw_app):
    from threading import Barrier, Thread
    barrier=Barrier(3);results=[];errors=[];body=envelope()
    def deliver():
        try:
            with hw_app.test_client() as other:
                barrier.wait(timeout=5)
                results.append(other.post(PATH,json=body,headers=AUTH).status_code)
        except Exception as error:errors.append(error)
    threads=[Thread(target=deliver) for _ in range(3)]
    try:
        for thread in threads:thread.start()
    finally:
        for thread in threads:
            thread.join(timeout=10)
    assert not errors and not any(t.is_alive() for t in threads)
    assert sorted(results)==[200,200,201] and count()==1
