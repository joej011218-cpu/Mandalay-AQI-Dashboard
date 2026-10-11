"""Real PostgreSQL persistence/episode lifecycle and source safety regressions."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, inspect
from app import db, Reading
from hardware import ingest, HardwareObservation, monitoring_latest, monitoring_alerts
from hardware_events import HardwareAlert, events
from test_hardware import hw_app, payload, current_hour, model_history, sensor_history


def test_numeric_invalid_persistence_history_and_quality(hw_app):
    now=datetime.now(timezone.utc)
    data=payload(now,message_id='real-invalid-1',co_mg_m3=0.75,co_valid=True,co_range_valid=False,nh3_ug_m3=90.6)
    assert ingest(data,now)['inserted']
    assert not ingest(data,now+timedelta(seconds=3))['inserted']
    row=db.session.scalar(select(HardwareObservation))
    assert row.co_mg_m3==0.75 and not row.co_valid
    assert row.validity['co_valid'] is True and row.validity['co_range_valid'] is False
    raw=hw_app.test_client().get('/api/hardware/history?days=1').json
    assert raw['sample_period_seconds']==900
    sample=raw['data'][0]
    assert sample['message_id']=='real-invalid-1' and sample['nh3_ug_m3']==90.6
    assert sample['source']=='ESP32' and sample['timestamp'].endswith('+06:30')
    assert sample['received_at'].endswith('+06:30')
    live=monitoring_latest(now)
    assert live['measurements']['carbon_monoxide']['value'] is None
    assert live['hardware']['last_observation']['co_mg_m3']==0.75
    assert not live['pollutant_details']['carbon_monoxide']['eligible']
    warnings=events(1,now)
    assert len(warnings)==1 and warnings[0]['alert_type']=='sensor-quality'
    assert warnings[0]['status']=='active' and warnings[0]['value']==0.75
    assert not any(e['alert_type'].endswith('concentration') for e in warnings)


def test_episode_dedup_resolution_and_recurrence(hw_app):
    now=datetime.now(timezone.utc)
    for i in range(3):
        stamp=now+timedelta(seconds=i)
        ingest(payload(stamp,co_mg_m3=3,message_id=f'high-{i}'),stamp)
    assert db.session.scalar(select(func.count()).select_from(HardwareAlert))==1
    first=db.session.scalar(select(HardwareAlert))
    assert first.device_id=='mandalay-node1' and first.value==3 and first.threshold==2
    assert first.status=='active' and first.last_seen==now+timedelta(seconds=2)
    assert first.timestamp==now and first.evidence_at==now+timedelta(seconds=2)
    ingest(payload(now+timedelta(seconds=3),co_mg_m3=1,message_id='normal'),now+timedelta(seconds=3))
    assert first.status=='resolved' and first.resolved_at==now+timedelta(seconds=3)
    ingest(payload(now+timedelta(seconds=4),co_mg_m3=3,message_id='high-again'),now+timedelta(seconds=4))
    rows=events(1,now+timedelta(seconds=4))
    assert len(rows)==2 and {r['status'] for r in rows}=={'active','resolved'}
    assert len({r['id'] for r in rows})==2
    assert len(events(1,now+timedelta(seconds=4)))==2


def test_stale_samples_only_quality_no_pollution_and_offline_episode(hw_app):
    now=datetime.now(timezone.utc)
    ingest(payload(now-timedelta(hours=2),co_mg_m3=99,nh3_ug_m3=5000,message_id='stale'),now)
    rows=events(1,now)
    assert len(rows)==1 and rows[0]['alert_type']=='sensor-status'
    assert len(events(1,now+timedelta(minutes=1)))==1
    ingest(payload(now+timedelta(minutes=2),message_id='fresh'),now+timedelta(minutes=2))
    rows=events(1,now+timedelta(minutes=2))
    assert rows[0]['status']=='resolved'


def test_invalid_latest_excludes_aqi_and_resolves_pollution(hw_app):
    at=current_hour();model_history(at);sensor_history(at)
    # Covered histories alone must not validate a new invalid observation.
    stamp=at+timedelta(seconds=1)
    ingest(payload(stamp,co_mg_m3=99,co_range_valid=False,nh3_valid=False,message_id='invalid-current'),stamp)
    live=monitoring_latest(stamp)
    for p in ('carbon_monoxide','ammonia'):
        assert not live['pollutant_details'][p]['eligible']
    alerts=events(1,stamp)
    assert not any(e['status']=='active' and e['alert_type'].endswith('concentration') for e in alerts)
    assert any(e['alert_type']=='sensor-quality' and e['pollutant']=='carbon_monoxide' for e in alerts)
    assert db.session.get(Reading,at).carbon_monoxide==30000


def test_covered_rolling_alert_persists_and_deduplicates(hw_app):
    at=current_hour();sensor_history(at)
    rows=events(1,at)
    rolling=[r for r in rows if r['alert_type']=='rolling-concentration']
    assert len(rolling)==1
    event=rolling[0]
    assert event['pollutant']=='ammonia' and event['averaging_hours']==24
    assert event['threshold']==800 and event['value']==1000
    assert event['source']=='ESP32' and event['device_id']=='mandalay-node1'
    assert [r['id'] for r in events(1,at)]==[r['id'] for r in rows]


def test_out_of_order_backfill_does_not_resolve_current_warning(hw_app):
    now=datetime.now(timezone.utc)
    ingest(payload(now,co_mg_m3=3,message_id='newest'),now)
    ingest(payload(now-timedelta(hours=1),co_mg_m3=1,message_id='backfill'),now)
    row=db.session.scalar(select(HardwareAlert))
    assert row.status=='active' and row.value==3


def test_additive_event_migration_repeatable_preserves_originals(hw_app):
    now=datetime.now(timezone.utc)
    ingest(payload(now,message_id='preserved'),now)
    db.session.commit()
    HardwareAlert.__table__.drop(db.engine)
    for _ in range(2):
        result=hw_app.test_cli_runner().invoke(args=['migrate-hardware'])
        assert result.exit_code==0,result.output
    assert inspect(db.engine).has_table('mandalay_hardware_alert_events')
    assert db.session.scalar(select(func.count()).select_from(HardwareObservation))==1
    assert db.session.scalar(select(HardwareObservation.original_payload))['message_id']=='preserved'
    assert db.session.scalar(select(func.count()).select_from(HardwareAlert))==0


def test_history_filter_and_missing_values_remain_missing(hw_app):
    now=datetime.now(timezone.utc)
    ingest(payload(now-timedelta(days=2),message_id='older'),now)
    ingest(payload(now,co_mg_m3=None,nh3_ug_m3=None,message_id='missing'),now)
    client=hw_app.test_client()
    short=client.get('/api/hardware/history?days=1').json['data']
    long=client.get('/api/hardware/history?days=7').json['data']
    assert len(short)==1 and len(long)==2
    assert short[0]['co_mg_m3'] is None and short[0]['nh3_ug_m3'] is None
    assert client.get('/api/hardware/history?days=0').status_code==400


def test_webhook_observation_and_quality_event_commit_atomically(hw_app,monkeypatch):
    from sqlalchemy.exc import OperationalError
    import hardware_events
    monkeypatch.setenv('HARDWARE_WEBHOOK_TOKEN','test-secret')
    data=payload(datetime.now(timezone.utc),message_id='atomic',co_range_valid=False)
    body=dict(topic='aqi/mandalay-node1/readings',username='mandalay_esp32',payload=data)
    client=hw_app.test_client();headers={'Authorization':'Bearer test-secret'}
    original=hardware_events.reconcile
    def fail(*args):raise OperationalError('safe-test',{},RuntimeError('storage unavailable'))
    monkeypatch.setattr(hardware_events,'reconcile',fail)
    assert client.post('/api/hardware/ingest',json=body,headers=headers).status_code==503
    assert db.session.scalar(select(func.count()).select_from(HardwareObservation))==0
    monkeypatch.setattr(hardware_events,'reconcile',original)
    assert client.post('/api/hardware/ingest',json=body,headers=headers).status_code==201
    assert client.post('/api/hardware/ingest',json=body,headers=headers).status_code==200
    assert db.session.scalar(select(func.count()).select_from(HardwareObservation))==1
    assert db.session.scalar(select(func.count()).select_from(HardwareAlert))==1
    result=client.get('/api/monitoring/alerts?days=1').json['data']
    assert any(r['source']=='ESP32' and r['status']=='active' and r['alert_type']=='sensor-quality' for r in result)


def test_concurrent_ingestion_creates_one_ongoing_episode(hw_app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier=Barrier(2);now=datetime.now(timezone.utc)
    def publish(index):
        with hw_app.app_context():
            barrier.wait(timeout=5)
            return ingest(payload(now,co_range_valid=False,message_id=f'parallel-{index}'),now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(publish,range(2)))
    assert all(r['inserted'] for r in results)
    assert db.session.scalar(select(func.count()).select_from(HardwareObservation))==2
    assert db.session.scalar(select(func.count()).select_from(HardwareAlert))==1


def test_ingestion_waiting_behind_later_poll_still_records_quality(hw_app):
    now=datetime.now(timezone.utc)
    ingest(payload(now,co_mg_m3=3,message_id='initial'),now)
    events(1,now+timedelta(seconds=2))  # A poll can acquire the lock first.
    ingest(payload(now+timedelta(seconds=1),co_range_valid=False,message_id='waiting'),now+timedelta(seconds=1))
    row=db.session.scalar(select(HardwareAlert).where(HardwareAlert.alert_type=='sensor-quality'))
    assert row and row.status=='active' and row.value==1.5


def test_event_evidence_migration_preserves_existing_episode(hw_app):
    from sqlalchemy import text
    now=datetime.now(timezone.utc)
    ingest(payload(now,co_mg_m3=3,message_id='old-event-schema'),now)
    with db.engine.begin() as conn:
        conn.execute(text('ALTER TABLE mandalay_hardware_alert_events DROP COLUMN evidence_at'))
    db.session.remove()
    for _ in range(2):
        result=hw_app.test_cli_runner().invoke(args=['migrate-hardware'])
        assert result.exit_code==0,result.output
    row=db.session.scalar(select(HardwareAlert))
    assert row.status=='active' and row.value==3 and row.timestamp==now
    assert row.evidence_at is None  # Migration does not invent old evidence.
    result=events(1,now)[0]
    assert result['measurement_timestamp']==now.astimezone(__import__('app').TZ).isoformat()


def test_equal_timestamps_use_latest_received_validity(hw_app):
    now=datetime.now(timezone.utc)
    ingest(payload(now,message_id='same-time-valid'),now)
    ingest(payload(now,message_id='same-time-invalid',co_mg_m3=0.75,co_range_valid=False),now+timedelta(seconds=1))
    live=monitoring_latest(now+timedelta(seconds=1))
    assert live['hardware']['last_observation']['message_id']=='same-time-invalid'
    assert live['hardware']['last_observation']['co_mg_m3']==0.75
    assert live['measurements']['carbon_monoxide']['status']=='invalid'
