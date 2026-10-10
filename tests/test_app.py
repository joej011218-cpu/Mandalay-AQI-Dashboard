import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import pytest
from openpyxl import Workbook
from sqlalchemy import select, text
from app import create_app, db, Reading, SourceObservation, CPCBResult, refresh, upsert, recalculate
from cpcb import POLLUTANTS, VERSION
from scripts.import_history import import_file

@pytest.fixture
def application():
    url=os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL must point to a disposable PostgreSQL database')
    app=create_app({'SQLALCHEMY_DATABASE_URI':url,'TESTING':True})
    with app.app_context():
        db.drop_all();db.create_all()
        yield app
        db.session.remove();db.drop_all()


def now_hour():
    # Canonical Yangon-hour grid is UTC xx:30.
    now=datetime.now(timezone.utc)
    result=now.replace(minute=30,second=0,microsecond=0)
    return result if result<=now else result-timedelta(hours=1)


def rows(at, hours=24, **changes):
    values=dict(pm2_5=100,pm10=75,nitrogen_dioxide=40,sulphur_dioxide=40,ozone=50,carbon_monoxide=1000,us_aqi=5)
    values.update(changes)
    return [dict(timestamp=at-timedelta(hours=i),source='test-mandalay',**values) for i in reversed(range(hours))]


def test_postgresql_is_required(monkeypatch):
    monkeypatch.setenv('DATABASE_URL','sqlite:///bad.db')
    with pytest.raises(RuntimeError):create_app()


def test_api_consistently_uses_cpcb(application):
    client=application.test_client()
    assert client.get('/api/health').json['database']=='postgresql'
    assert client.get('/api/latest').status_code==404
    assert client.get('/api/analytics').json['summary']['cpcb_aqi']['mean'] is None
    at=now_hour()
    records=rows(at)+rows(at+timedelta(days=3),hours=1,pm2_5=500)
    # Legacy alternate-grid raw records must not double-count the canonical timeline.
    records.append(dict(records[0],timestamp=records[0]['timestamp']+timedelta(minutes=30),us_aqi=500))
    upsert(records)
    latest=client.get('/api/latest').json
    assert latest['aqi_standard'].startswith('Indian CPCB AQI')
    assert latest['data']['cpcb_aqi']==232 and latest['data']['category']=='Poor'
    assert latest['data']['raw_aqi']['us_aqi']==5
    assert latest['data']['dominant_pollutant']=='pm2_5'
    assert latest['data']['timestamp'].endswith('+06:30')
    assert latest['latitude']==21.9588 and latest['longitude']==96.0891
    h=client.get('/api/history').json['data']
    assert len(h)==24 and sum(r['cpcb_aqi'] is None for r in h)==15
    assert 'pollutant_details' not in h[0]
    assert 'pollutant_details' in client.get('/api/history?details=1').json['data'][0]
    analytics=client.get('/api/analytics').json
    assert analytics['summary']['cpcb_aqi']['mean']==232
    assert 'us_aqi' not in analytics['summary']
    alerts=client.get('/api/alerts').json
    assert alerts['threshold']==201 and len(alerts['data'])==9
    assert all(r['cpcb_aqi']>=201 and r['message'].startswith('Mandalay:') for r in alerts['data'])
    assert db.session.query(Reading).count()==26  # Raw legacy and future records preserved.
    assert db.session.query(CPCBResult).count()==25
    for endpoint in ('history','analytics','alerts'):
        for days in ('0','367','abc'):
            assert client.get(f'/api/{endpoint}?days={days}').status_code==400
    for path in ('/','/analytics','/alerts','/about'):
        response=client.get(path)
        assert response.status_code==200 and 'Content-Security-Policy' in response.headers
        assert b'Vadodara' not in response.data
        assert b'INDIAN CPCB AIR QUALITY INDEX' in response.data


def test_refresh_validates_units_coordinates_and_recalculates(application,monkeypatch):
    at=now_hour()
    from app import FIELDS,TZ
    hourly={'time':[(at-timedelta(hours=i)).astimezone(TZ).replace(tzinfo=None).isoformat() for i in reversed(range(24))]}
    vals=dict(pm2_5=45,pm10=75,nitrogen_dioxide=40,sulphur_dioxide=40,ozone=50,carbon_monoxide=1000,us_aqi=500)
    hourly.update({f:[vals[f]]*24 for f in FIELDS})
    payload=dict(hourly=hourly,hourly_units={**{p:'μg/m³' for p in POLLUTANTS},'us_aqi':'USAQI'},
                 timezone='Asia/Yangon',latitude=22.0,longitude=96.1)
    provider=Mock();provider.json.return_value=payload
    fetch=Mock(return_value=provider);monkeypatch.setattr('app.requests.get',fetch)
    assert refresh()==24 and refresh()==24
    assert db.session.query(Reading).count()==24 and db.session.query(SourceObservation).count()==24
    assert db.session.scalar(select(CPCBResult.cpcb_aqi).where(CPCBResult.timestamp==at))==75
    params=fetch.call_args.kwargs['params']
    assert (params['latitude'],params['longitude'],params['timezone'])==(21.9588,96.0891,'Asia/Yangon')
    payload['hourly_units']['carbon_monoxide']='mg/m³'
    with pytest.raises(ValueError):refresh()
    payload['hourly_units']['carbon_monoxide']='μg/m³'
    payload['latitude']=23
    with pytest.raises(ValueError):refresh()
    payload['latitude']=22
    monkeypatch.setenv('REFRESH_SECRET','test-token')
    client=application.test_client()
    assert client.post('/api/refresh').status_code==401
    assert client.post('/api/refresh',headers={'X-Refresh-Secret':'test-token'}).status_code==200
    import requests
    fetch.side_effect=requests.Timeout()
    assert client.post('/api/refresh',headers={'X-Refresh-Secret':'test-token'}).status_code==502


def test_import_preserves_originals_and_never_relabels_source_aqi(application,tmp_path):
    workbook=Workbook();ws=workbook.active
    ws.append(['Time','PM2.5','PM10','CO_mg','AQI'])
    for i in range(24):ws.append([datetime(2026,1,1,i),45,75,1,500])
    path=tmp_path/'mandalay.xlsx';workbook.save(path)
    mapping={'timestamp':'Time','pm2_5':'PM2.5','pm10':'PM10','carbon_monoxide':'CO_mg','original_aqi':'AQI'}
    kwargs=dict(units={'pm2_5':'ug/m3','pm10':'ug/m3','carbon_monoxide':'mg/m3'},timestamp_timezone='Asia/Yangon',confirm=True)
    assert import_file(path,mapping,**kwargs)==24
    row=db.session.scalar(select(Reading).order_by(Reading.timestamp.desc()))
    assert row.carbon_monoxide==1000 and row.us_aqi is None
    assert row.cpcb_result.cpcb_aqi==75
    snapshot=db.session.scalar(select(SourceObservation).order_by(SourceObservation.timestamp.desc()))
    assert snapshot.original_values['CO_mg']==1 and snapshot.original_values['AQI']==500
    assert snapshot.provenance['source_aqi_standard']=='unknown'
    assert import_file(path,mapping,**kwargs)==24
    assert db.session.query(Reading).count()==24 and db.session.query(SourceObservation).count()==24
    assert import_file(path,mapping,source_aqi_standard='us-epa',**kwargs)==24
    assert db.session.scalar(select(Reading.us_aqi).limit(1))==500
    assert db.session.scalar(select(CPCBResult.cpcb_aqi).where(CPCBResult.status=='available').limit(1))==75
    assert db.session.scalar(select(Reading.timestamp).order_by(Reading.timestamp))==datetime(2025,12,31,17,30,tzinfo=timezone.utc)
    ws.append([datetime(2026,1,2),-1,75,1,500]);workbook.save(path)
    with pytest.raises(ValueError):import_file(path,mapping,**kwargs)
    assert db.session.query(Reading).count()==24
    with pytest.raises(ValueError):import_file(path,mapping,**dict(kwargs,units={}))


def test_recalculate_invalidates_downstream_and_is_atomic(application,monkeypatch):
    at=now_hour();upsert(rows(at))
    assert db.session.get(CPCBResult,at).cpcb_aqi==232
    upsert(rows(at,hours=24,pm2_5=30))
    assert db.session.get(CPCBResult,at).cpcb_aqi==75
    assert recalculate()==24
    before=db.session.get(Reading,at).pm2_5
    monkeypatch.setattr('app._recalculate',Mock(side_effect=RuntimeError('simulated calculation failure')))
    with pytest.raises(RuntimeError):upsert(rows(at,hours=1,pm2_5=500))
    assert db.session.get(Reading,at).pm2_5==before
    assert db.session.get(CPCBResult,at).cpcb_aqi==75


def test_missing_cpcb_is_not_replaced_with_raw_us_aqi(application):
    upsert(rows(now_hour(),hours=8,pm2_5=None,pm10=None,us_aqi=500))
    client=application.test_client()
    assert client.get('/api/latest').json['data']['cpcb_aqi'] is None
    assert client.get('/api/latest').json['data']['status']=='unavailable'
    assert client.get('/api/alerts').json['data']==[]
    assert client.get('/api/analytics').json['summary']['cpcb_aqi']['count']==0


def test_severe_band_alert_is_explicitly_non_numeric(application):
    upsert(rows(now_hour(),pm2_5=500))
    r=application.test_client().get('/api/alerts').json['data'][0]
    assert r['cpcb_aqi'] is None and r['category']=='Severe'
    assert r['alert_type']=='severe-band-warning' and r['status']=='unavailable'


def test_prediction_requires_cpcb_target_confirmation(application,monkeypatch):
    monkeypatch.setenv('PREDICTION_URL','https://mandalay-prediction.example/predict')
    client=application.test_client()
    assert b'href="https://mandalay-prediction.example/predict"' not in client.get('/').data
    monkeypatch.setenv('PREDICTION_AQI_STANDARD',VERSION)
    assert b'href="https://mandalay-prediction.example/predict"' in client.get('/').data


def test_old_calculation_version_cannot_be_served_as_current(application):
    at=now_hour();upsert(rows(at,us_aqi=500))
    result=db.session.get(CPCBResult,at)
    result.calculation_version='old-method'
    db.session.commit()
    response=application.test_client().get('/api/latest').json['data']
    assert response['cpcb_aqi'] is None and response['status']=='unavailable'
    assert response['raw_aqi']['us_aqi']==500
    assert response['calculation_version']==VERSION
