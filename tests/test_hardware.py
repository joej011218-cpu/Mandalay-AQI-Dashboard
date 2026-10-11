import json
import os
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from sqlalchemy import select,func
from app import create_app,db,Reading,CPCBResult,upsert,TZ
from hardware import HardwareObservation,MQTTState,ingest,hourly_hardware,monitoring_latest,monitoring_history,hardware_status
from mqtt_worker import decode,Subscriber
import combined_cpcb as combined

@pytest.fixture
def hw_app():
    url=os.getenv('TEST_DATABASE_URL')
    if not url:pytest.skip('Disposable PostgreSQL TEST_DATABASE_URL required')
    app=create_app({'SQLALCHEMY_DATABASE_URI':url,'TESTING':True,'MANDALAY_DEVICE_IDS':'mandalay-node1',
        'HARDWARE_SAMPLE_SECONDS':'900','HARDWARE_DEVICE_ID':'mandalay-node1','RAW_ALERT_THRESHOLDS':{'carbon_monoxide':2,'ammonia':1000}})
    with app.app_context():
        db.drop_all();db.create_all()
        yield app
        db.session.remove();db.drop_all()


def current_hour():return datetime.now(timezone.utc).astimezone(TZ).replace(minute=0,second=0,microsecond=0).astimezone(timezone.utc)


def payload(at,**values):
    data=dict(device_id='mandalay-node1',city='Mandalay',timestamp=at.isoformat(),co_mg_m3=1.5,nh3_ug_m3=1000,
              co_range_valid=True,nh3_valid=True,temperature=27.8,humidity=44)
    data.update(values);return data


def model_history(at,hours=24):
    records=[dict(timestamp=at-timedelta(hours=i),source='test-mandalay',pm2_5=30,pm10=50,
                  nitrogen_dioxide=40,sulphur_dioxide=40,ozone=50,carbon_monoxide=30000,us_aqi=500)
             for i in range(hours)]
    upsert(records)


def sensor_history(at,hours=24):
    for hour in range(hours):
        for minute in (0,15,30,45):
            stamp=at-timedelta(hours=hour,minutes=minute)
            ingest(payload(stamp),received_at=stamp)


def test_ingestion_original_units_duplicates_flags(hw_app):
    now=datetime.now(timezone.utc)
    p=payload(now,co_ppm=1.2,co_ratio=2.3,nh3_ratio=1.1)
    assert ingest(p,now)['inserted']
    assert not ingest(p,now+timedelta(seconds=5))['inserted']
    r=db.session.scalar(select(HardwareObservation))
    assert r.co_mg_m3==1.5 and r.nh3_ug_m3==1000 and r.received_at==now
    assert r.original_payload==p and r.validity['co_ppm']==1.2
    ingest(payload(now+timedelta(seconds=1),co_range_valid=False,nh3_valid=False),now+timedelta(seconds=1))
    last=db.session.scalar(select(HardwareObservation).order_by(HardwareObservation.timestamp.desc()))
    assert not last.co_valid and not last.nh3_valid and last.co_mg_m3==1.5
    assert monitoring_latest(now+timedelta(seconds=1))['carbon_monoxide'] is None


@pytest.mark.parametrize('change',[
    {'device_id':'vadodara-node1'},{'city':'Vadodara'},{'co_mg_m3':-1},{'co_mg_m3':'1.5'},
    {'co_mg_m3':float('nan')},{'nh3_ug_m3':True},{'humidity':110},{'co_range_valid':'true'},
    {'units':{'co_mg_m3':'ug/m3'}},
])
def test_invalid_schema_and_city_isolation(hw_app,change):
    with pytest.raises(ValueError):ingest(payload(datetime.now(timezone.utc),**change))
    assert db.session.query(HardwareObservation).count()==0


def test_timestamp_quality_and_undated_retained(hw_app):
    now=datetime.now(timezone.utc);p=payload(now)
    del p['timestamp']
    with pytest.raises(ValueError):ingest(p,now)
    p['message_id']='boot-1:sample-1'
    assert ingest(p,now)['inserted'];assert not ingest(p,now+timedelta(minutes=1))['inserted']
    row=db.session.scalar(select(HardwareObservation));assert row.timestamp_quality=='server_received' and row.timestamp==now
    with pytest.raises(ValueError):ingest(p,now,retained=True)
    with pytest.raises(ValueError):ingest(payload(now+timedelta(hours=1)),now)
    with pytest.raises(ValueError):ingest(payload(now-timedelta(days=8)),now)


def test_json_decoder_rejects_duplicates_nonfinite_and_size():
    for raw in (b'{"co_mg_m3":NaN}',b'{"device_id":"x","device_id":"y"}',b'x'*16385):
        with pytest.raises(ValueError):decode(raw)


def test_hourly_coverage_no_burst_or_missing_fill(hw_app):
    at=current_hour()
    for seconds in range(20):ingest(payload(at-timedelta(minutes=30,seconds=seconds)),received_at=at)
    hourly=hourly_hardware(at-timedelta(hours=1),at)
    assert hourly[at]['carbon_monoxide'] is None
    for minutes in (0,15,45):ingest(payload(at-timedelta(minutes=minutes)),received_at=at)
    hourly=hourly_hardware(at-timedelta(hours=1),at)
    assert hourly[at]['carbon_monoxide']==1.5 and hourly[at]['ammonia']==1000
    assert hourly[at]['hardware_coverage']['co_slots']>=3


def test_combined_windows_and_no_modeled_co_fallback(hw_app):
    at=current_hour();model_history(at)
    before=db.session.scalar(select(CPCBResult.cpcb_aqi).where(CPCBResult.timestamp==at))
    partial=monitoring_latest(at)
    assert partial['carbon_monoxide'] is None and partial['ammonia'] is None
    assert partial['coverage']=='partial' and partial['cpcb_aqi']==50
    assert not partial['pollutant_details']['carbon_monoxide']['eligible']
    sensor_history(at)
    live=monitoring_latest(at)
    assert live['carbon_monoxide']==1.5 and live['ammonia']==1000
    assert live['coverage']=='complete' and live['provisional']
    assert live['cpcb_aqi']==250 and live['dominant_pollutant']=='ammonia'
    assert live['pollutant_details']['carbon_monoxide']['average']==1.5
    assert live['pollutant_details']['ammonia']['average']==1000
    assert live['measurements']['carbon_monoxide']['unit']=='mg/m³'
    # Monitoring cannot change the modeled data or prediction-facing results.
    assert db.session.get(Reading,at).carbon_monoxide==30000
    assert db.session.get(CPCBResult,at).cpcb_aqi==before


def test_partial_history_16_hour_boundary_and_8_hour_gaps(hw_app):
    at=current_hour();model_history(at);sensor_history(at,15)
    _,h=monitoring_history(1,at)
    assert not combined.calculate(at,h)['pollutant_details']['ammonia']['eligible']
    for m in (0,15,30,45):ingest(payload(at-timedelta(hours=15,minutes=m)),received_at=at)
    _,h=monitoring_history(1,at)
    assert combined.calculate(at,h)['pollutant_details']['ammonia']['eligible']
    for row in db.session.scalars(select(HardwareObservation).where(HardwareObservation.timestamp>at-timedelta(hours=4),HardwareObservation.timestamp<=at-timedelta(hours=3))):row.co_valid=False
    db.session.commit();_,h=monitoring_history(1,at)
    assert not combined.calculate(at,h)['pollutant_details']['carbon_monoxide']['eligible']


def test_stale_hardware_never_masquerades_as_current(hw_app):
    at=current_hour();model_history(at);sensor_history(at)
    live=monitoring_latest(at+timedelta(minutes=20))
    assert live['hardware']['status']=='stale' and live['carbon_monoxide'] is None
    assert not live['pollutant_details']['carbon_monoxide']['eligible']
    assert live['coverage']=='partial'


def test_api_history_alerts_daily_views_and_legacy_contract(hw_app):
    at=current_hour();model_history(at);sensor_history(at)
    now=datetime.now(timezone.utc)
    ingest(payload(now),received_at=now)
    client=hw_app.test_client()
    latest=client.get('/api/monitoring/latest').json
    assert latest['data']['ammonia']==1000 and latest['data']['carbon_monoxide']==1.5
    assert latest['sources']['carbon_monoxide']=='ESP32'
    raw=client.get('/api/hardware/latest').json['data']
    assert raw['co_mg_m3']==1.5 and raw['nh3_ug_m3']==1000
    h=client.get('/api/hardware/history?days=1').json
    assert len(h['data'])>0 and h['data'][-1]['co_mg_m3']==1.5
    daily=client.get('/api/monitoring/history?days=1&view=daily').json
    assert daily['view']=='daily' and daily['data'][0]['daily_summary']
    for endpoint in ('history','analytics','alerts'):
        assert client.get(f'/api/monitoring/{endpoint}?days=bad').status_code==400
    assert client.get('/api/monitoring/history?view=invalid').status_code==400
    alerts=client.get('/api/monitoring/alerts?days=1').json['data']
    assert any(a['pollutant']=='ammonia' and a['alert_type']=='rolling-concentration' and a['threshold']==800 and a['unit']=='µg/m³' for a in alerts)
    assert len({a['id'] for a in alerts})==len(alerts)
    assert [a['id'] for a in alerts]==[a['id'] for a in client.get('/api/monitoring/alerts?days=1').json['data']]
    legacy=client.get('/api/latest').json
    assert legacy['data']['carbon_monoxide']==30000 and legacy['calculation_version']=='cpcb-2014-rolling-v1'


def test_raw_warnings_and_offline_alerts(hw_app):
    at=current_hour();ingest(payload(at,co_mg_m3=3,nh3_ug_m3=1100),at)
    events=hw_app.test_client().get('/api/monitoring/alerts?days=1').json['data']
    assert any(e['alert_type']=='raw-concentration' and e['pollutant']=='carbon_monoxide' and e['averaging_hours']==0 for e in events)
    assert not any(e['alert_type']=='cpcb-aqi' for e in events)
    hw_app.config['HARDWARE_DEVICE_ID']='';hw_app.config['MANDALAY_DEVICE_IDS']=''
    events=hw_app.test_client().get('/api/monitoring/alerts').json['data']
    assert any(e['alert_type']=='sensor-status' for e in events)


@pytest.mark.parametrize('value,index',[(0,0),(200,50),(400,100),(800,200),(1000,250),(1200,300),(1800,400),(1801,None)])
def test_ammonia_breakpoints(value,index):assert combined.ammonia_subindex(value)==index


def test_additive_migration_and_prediction_navigation_unchanged(hw_app,monkeypatch):
    at=current_hour();model_history(at)
    before=db.session.query(Reading).count()
    assert hw_app.test_cli_runner().invoke(args=['migrate-hardware']).exit_code==0
    assert hw_app.test_cli_runner().invoke(args=['migrate-hardware']).exit_code==0
    assert db.session.query(Reading).count()==before
    monkeypatch.setenv('PREDICTION_URL','https://mandalay-aqi-prediction.onrender.com/prediction.html')
    monkeypatch.setenv('PREDICTION_AQI_STANDARD','cpcb-2014-rolling-v1')
    assert b'https://mandalay-aqi-prediction.onrender.com/prediction.html' in hw_app.test_client().get('/').data


def test_mqtt_secure_configuration_and_callbacks(hw_app,monkeypatch):
    monkeypatch.setenv('MQTT_HOST','broker.hivemq.com');monkeypatch.setenv('MQTT_ALLOW_INSECURE_DEV','true')
    with pytest.raises(ValueError):Subscriber(hw_app)
    monkeypatch.setenv('MQTT_HOST','127.0.0.1');fake=Mock();monkeypatch.setattr('mqtt_worker.mqtt.Client',Mock(return_value=fake))
    fake.subscribe.return_value=(0,1)
    worker=Subscriber(hw_app);worker.on_connect(fake,None,None,0,None)
    assert fake.subscribe.call_args.args[0]=='aqi/mandalay-node1/readings'
    message=SimpleNamespace(payload=json.dumps(payload(datetime.now(timezone.utc))).encode(),topic='aqi/mandalay-node1/readings',retain=False,qos=1,mid=1)
    worker.on_message(fake,None,message)
    assert db.session.query(HardwareObservation).count()==1
    fake.ack.assert_called_once_with(1,1)
    worker.on_disconnect(fake,None,None,1,None)
    assert not db.session.get(MQTTState,'subscriber').connected
    assert worker.connected is False


def test_all_seven_rolling_threshold_sources_and_units(hw_app):
    at=current_hour();model_history(at);sensor_history(at)
    hw_app.config['ROLLING_ALERT_THRESHOLDS']={p:1 for p in combined.POLLUTANTS}
    # Current hardware warnings require a fresh, valid latest observation.
    now=datetime.now(timezone.utc);ingest(payload(now),received_at=now)
    events=hw_app.test_client().get('/api/monitoring/alerts?days=1').json['data']
    events=[e for e in events if e['alert_type']=='rolling-concentration']
    assert {e['pollutant'] for e in events}==set(combined.POLLUTANTS)
    for e in events:
        assert e['source']==('Open-Meteo' if e['pollutant'] in combined.OPEN_METEO else 'ESP32')
        assert e['unit']==combined.UNITS[e['pollutant']]
        assert e['averaging_hours']==(8 if e['pollutant'] in ('ozone','carbon_monoxide') else 24)


def test_tls_is_required_without_local_development_override(hw_app,monkeypatch):
    monkeypatch.delenv('MQTT_ALLOW_INSECURE_DEV',raising=False)
    monkeypatch.setenv('MQTT_HOST','private-mqtt.example')
    monkeypatch.delenv('MQTT_USERNAME',raising=False);monkeypatch.delenv('MQTT_PASSWORD',raising=False)
    with pytest.raises(ValueError):Subscriber(hw_app)
    monkeypatch.setenv('MQTT_USERNAME','test-user');monkeypatch.setenv('MQTT_PASSWORD','test-password')
    fake=Mock();monkeypatch.setattr('mqtt_worker.mqtt.Client',Mock(return_value=fake))
    Subscriber(hw_app)
    context=fake.tls_set_context.call_args.args[0]
    assert context.check_hostname and context.verify_mode.name=='CERT_REQUIRED'


def test_qos_ack_only_after_commit_and_db_failure_does_not_ack(hw_app,monkeypatch):
    monkeypatch.setenv('MQTT_HOST','127.0.0.1');monkeypatch.setenv('MQTT_ALLOW_INSECURE_DEV','true')
    fake=Mock();monkeypatch.setattr('mqtt_worker.mqtt.Client',Mock(return_value=fake))
    worker=Subscriber(hw_app)
    monkeypatch.setattr('mqtt_worker.ingest',Mock(side_effect=RuntimeError('database down')))
    msg=SimpleNamespace(payload=json.dumps(payload(datetime.now(timezone.utc))).encode(),topic='aqi/mandalay-node1/readings',retain=False,qos=1,mid=1)
    worker.on_message(fake,None,msg)
    fake.ack.assert_not_called();fake.disconnect.assert_called_once()


def test_single_leader_lock_prevents_duplicate_worker_sessions(hw_app):
    from mqtt_worker import LEADER_LOCK
    from sqlalchemy import text
    first=db.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
    second=db.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
    try:
        query=text('SELECT pg_try_advisory_lock(:key)')
        assert first.execute(query,{'key':LEADER_LOCK}).scalar()
        assert not second.execute(query,{'key':LEADER_LOCK}).scalar()
        first.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':LEADER_LOCK})
        assert second.execute(query,{'key':LEADER_LOCK}).scalar()
        second.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':LEADER_LOCK})
    finally:first.close();second.close()


def test_other_devices_never_enter_primary_history_or_alerts(hw_app):
    at=current_hour();model_history(at)
    hw_app.config['MANDALAY_DEVICE_IDS']='mandalay-node1,mandalay-node2'
    for i in range(24):
        for minute in (0,15,30,45):
            stamp=at-timedelta(hours=i,minutes=minute)
            ingest(payload(stamp,device_id='mandalay-node2',co_mg_m3=30,nh3_ug_m3=1700),stamp)
    latest=monitoring_latest(at)
    assert latest['hardware']['status']=='offline'
    assert latest['carbon_monoxide'] is None and latest['ammonia'] is None
    history=hw_app.test_client().get('/api/monitoring/history?days=1').json['data']
    assert all(row['carbon_monoxide'] is None and row['ammonia'] is None for row in history)
    assert hw_app.test_client().get('/api/hardware/history?days=1').json['data']==[]
    events=hw_app.test_client().get('/api/monitoring/alerts?days=1').json['data']
    assert not any(event['alert_type'] in ('raw-concentration','rolling-concentration') and event['source']=='ESP32' for event in events)


def test_rejected_mqtt_city_does_not_persist_or_retry_forever(hw_app,monkeypatch):
    monkeypatch.setenv('MQTT_HOST','127.0.0.1');monkeypatch.setenv('MQTT_ALLOW_INSECURE_DEV','true')
    fake=Mock();monkeypatch.setattr('mqtt_worker.mqtt.Client',Mock(return_value=fake))
    worker=Subscriber(hw_app)
    msg=SimpleNamespace(payload=json.dumps(payload(datetime.now(timezone.utc),city='Vadodara')).encode(),topic='aqi/mandalay-node1/readings',retain=False,qos=1,mid=7)
    worker.on_message(fake,None,msg)
    assert db.session.query(HardwareObservation).count()==0
    fake.ack.assert_called_once_with(7,1)
    fake.disconnect.assert_not_called()


from test_firmware import firmware_payload

@pytest.mark.parametrize('mode',['default','valid-co','no-dht','nonfinite','no-clock'])
def test_compiled_firmware_payload_through_worker_and_postgres(hw_app,monkeypatch,firmware_payload,mode):
    hw_app.config['MANDALAY_DEVICE_IDS']='node1';hw_app.config['HARDWARE_DEVICE_ID']='node1'
    monkeypatch.setenv('MQTT_HOST','127.0.0.1');monkeypatch.setenv('MQTT_ALLOW_INSECURE_DEV','true')
    monkeypatch.setenv('MQTT_TOPIC_TEMPLATE','aqi/{device_id}/readings')
    fake=Mock();monkeypatch.setattr('mqtt_worker.mqtt.Client',Mock(return_value=fake))
    worker=Subscriber(hw_app)
    stamp='' if mode=='no-clock' else datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    p=firmware_payload(stamp,mode)
    msg=SimpleNamespace(payload=json.dumps(p).encode(),topic='aqi/node1/readings',retain=False,qos=0,mid=1)
    worker.on_message(fake,None,msg);worker.on_message(fake,None,msg)
    assert db.session.query(HardwareObservation).count()==1
    row=db.session.scalar(select(HardwareObservation))
    assert row.original_payload==p
    assert row.nh3_ug_m3==p['nh3_ug_m3'] and row.co_mg_m3==p['co_mg_m3']
    assert row.co_valid==(mode=='valid-co')
    assert row.nh3_valid==(mode!='nonfinite')
    assert row.timestamp_quality==('server_received' if mode=='no-clock' else 'measurement')
    raw=hw_app.test_client().get('/api/hardware/latest').json['data']
    assert raw['device_id']=='node1' and raw['co_mg_m3']==p['co_mg_m3']
    assert raw['temperature']==p['temperature']
    live=hw_app.test_client().get('/api/monitoring/latest').json['data']
    assert live['carbon_monoxide']==(p['co_mg_m3'] if mode=='valid-co' else None)
    assert live['ammonia']==p['nh3_ug_m3']
    fake.ack.assert_not_called() # PubSubClient publishes QoS 0; no acknowledgment exists.


@pytest.mark.parametrize('grant,ready',[(0,True),(1,True),(128,False)])
def test_emqx_tls_account_and_subscription_authorization(hw_app,monkeypatch,grant,ready):
    hw_app.config['MANDALAY_DEVICE_IDS']='node1';hw_app.config['HARDWARE_DEVICE_ID']='node1'
    monkeypatch.setenv('MQTT_HOST','p0fb61ec.ala.asia-southeast1.emqxsl.com')
    monkeypatch.setenv('MQTT_PORT','8883');monkeypatch.setenv('MQTT_USERNAME','mandalay_worker')
    monkeypatch.setenv('MQTT_PASSWORD','test-only-not-real-password')
    monkeypatch.setenv('MQTT_EXPECTED_USERNAME','mandalay_worker')
    monkeypatch.delenv('MQTT_ALLOW_INSECURE_DEV',raising=False)
    fake=Mock();fake.subscribe.return_value=(0,10)
    monkeypatch.setattr('mqtt_worker.mqtt.Client',Mock(return_value=fake))
    worker=Subscriber(hw_app)
    assert worker.host=='p0fb61ec.ala.asia-southeast1.emqxsl.com' and worker.port==8883
    fake.username_pw_set.assert_called_once_with('mandalay_worker','test-only-not-real-password')
    context=fake.tls_set_context.call_args.args[0]
    assert context.check_hostname and context.verify_mode.name=='CERT_REQUIRED'
    assert context.minimum_version.name=='TLSv1_2'
    fake.reconnect_delay_set.assert_called_once_with(min_delay=1,max_delay=60)
    worker.on_connect(fake,None,None,0,None)
    fake.subscribe.assert_called_once_with('aqi/node1/readings',qos=1)
    assert not worker.connected
    worker.on_subscribe(fake,None,10,[SimpleNamespace(value=grant)],None)
    assert worker.connected is ready and db.session.get(MQTTState,'subscriber').connected is ready
    worker.on_disconnect(fake,None,None,1,None)
    assert not worker.connected and not worker.authorized_topics


def test_emqx_worker_rejects_publisher_account_and_missing_password(hw_app,monkeypatch):
    monkeypatch.setenv('MQTT_HOST','p0fb61ec.ala.asia-southeast1.emqxsl.com')
    monkeypatch.setenv('MQTT_EXPECTED_USERNAME','mandalay_worker')
    monkeypatch.setenv('MQTT_USERNAME','mandalay_esp32')
    with pytest.raises(ValueError,match='subscriber account'):Subscriber(hw_app)
    monkeypatch.setenv('MQTT_USERNAME','mandalay_worker')
    monkeypatch.delenv('MQTT_PASSWORD',raising=False)
    monkeypatch.delenv('MQTT_CLIENT_CERT',raising=False);monkeypatch.delenv('MQTT_CLIENT_KEY',raising=False)
    monkeypatch.delenv('MQTT_ALLOW_INSECURE_DEV',raising=False)
    with pytest.raises(ValueError,match='authenticated TLS'):Subscriber(hw_app)


@pytest.mark.parametrize('timing',['none','timestamp','message_id'])
@pytest.mark.parametrize('qos',[0,1])
def test_user_mqttx_test_message_never_becomes_hardware(hw_app,monkeypatch,caplog,timing,qos):
    hw_app.config['MANDALAY_DEVICE_IDS']='node1';hw_app.config['HARDWARE_DEVICE_ID']='node1'
    monkeypatch.setenv('MQTT_HOST','127.0.0.1');monkeypatch.setenv('MQTT_ALLOW_INSECURE_DEV','true')
    fake=Mock();monkeypatch.setattr('mqtt_worker.mqtt.Client',Mock(return_value=fake))
    worker=Subscriber(hw_app)
    p=dict(device_id='node1',co_mg_m3=1.5,nh3_ug_m3=1050,temperature=28.0,humidity=43.0,test=True)
    if timing=='timestamp':p['timestamp']=datetime.now(timezone.utc).isoformat()
    if timing=='message_id':p['message_id']='mqttx-synthetic-1'
    msg=SimpleNamespace(payload=json.dumps(p).encode(),topic='aqi/node1/readings',retain=True,qos=qos,mid=9)
    with caplog.at_level('INFO',logger='mandalay.mqtt'):worker.on_message(fake,None,msg)
    assert 'Rejected synthetic/test MQTT message' in caplog.text
    assert db.session.query(HardwareObservation).count()==0
    assert hw_app.test_client().get('/api/hardware/latest').json['data'] is None
    assert hw_app.test_client().get('/api/hardware/status').json['data']['status']=='offline'
    if qos:fake.ack.assert_called_once_with(9,1)
    else:fake.ack.assert_not_called()
    fake.disconnect.assert_not_called()


@pytest.mark.parametrize('marker',[{'test':'true'},{'test':1},{'test':None},{'test':[]},{'synthetic':True}])
def test_synthetic_markers_and_malformed_test_flags_rejected_directly(hw_app,marker):
    with pytest.raises(ValueError):ingest(payload(datetime.now(timezone.utc),**marker))
    assert db.session.query(HardwareObservation).count()==0


def test_explicit_false_test_flag_allows_valid_real_payload(hw_app):
    now=datetime.now(timezone.utc)
    assert ingest(payload(now,test=False,synthetic=False),now)['inserted']
    assert monitoring_latest(now)['ammonia']==1000


@pytest.mark.parametrize('marker',[{'test':True},{'test':'true'},{'synthetic':True}])
def test_preexisting_synthetic_rows_are_audit_only(hw_app,marker):
    at=current_hour();model_history(at);sensor_history(at)
    count=db.session.query(HardwareObservation).count()
    # Simulate old persisted rows in disposable test DB; don't delete originals.
    for row in db.session.scalars(select(HardwareObservation)):
        row.original_payload={**row.original_payload,**marker}
    db.session.commit()
    client=hw_app.test_client()
    assert client.get('/api/hardware/latest').json['data'] is None
    assert client.get('/api/hardware/history?days=1').json['data']==[]
    assert client.get('/api/hardware/status').json['data']['status']=='offline'
    live=monitoring_latest(at)
    assert live['carbon_monoxide'] is None and live['ammonia'] is None
    assert all(r['carbon_monoxide'] is None and r['ammonia'] is None for r in client.get('/api/monitoring/history?days=1').json['data'])
    stats=client.get('/api/monitoring/analytics?days=1').json['summary']
    assert stats['carbon_monoxide']['count']==0 and stats['ammonia']['count']==0
    events=client.get('/api/monitoring/alerts?days=1').json['data']
    assert not any(e['source']=='ESP32' and e['alert_type'] in ('rolling-concentration','raw-concentration') for e in events)
    assert db.session.query(HardwareObservation).count()==count
