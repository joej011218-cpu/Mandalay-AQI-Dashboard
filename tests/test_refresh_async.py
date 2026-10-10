"""Real PostgreSQL lock/status and real background-thread integration tests."""
import logging
from threading import Event, Thread
from unittest.mock import Mock

import pytest
from flask import current_app
from sqlalchemy import func, select

import app as dashboard
import refresh_jobs as jobs
from app import db, Reading, create_app
from test_app import application, now_hour

AUTH = {'Authorization': 'Bearer test-refresh-secret'}


@pytest.fixture
def async_app(application, monkeypatch):
    monkeypatch.setenv('REFRESH_TOKEN', 'test-refresh-secret')
    threads = []
    release = Event()
    def spawn(**kwargs):
        thread = Thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(jobs, 'Thread', spawn)
    yield application, threads, release
    release.set()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), 'Refresh thread did not clean up'


def wait(threads):
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()


@pytest.mark.parametrize('endpoint,method',[('/api/refresh','post'),('/api/refresh/status','get')])
def test_auth_missing_wrong_valid_and_constant_time(async_app,monkeypatch,endpoint,method):
    app,threads,_=async_app;client=app.test_client()
    spy=Mock(wraps=dashboard.hmac.compare_digest)
    monkeypatch.setattr(dashboard.hmac,'compare_digest',spy)
    enqueue=Mock(return_value='test-job');monkeypatch.setattr(jobs,'enqueue',enqueue)
    for value in ('','Bearer wrong','Basic test-refresh-secret','Bearer tést'):
        assert getattr(client,method)(endpoint,headers={'Authorization':value}).status_code==401
    assert spy.call_count==4
    assert not threads and not enqueue.called
    monkeypatch.delenv('REFRESH_TOKEN')
    assert getattr(client,method)(endpoint,headers=AUTH).status_code==503
    assert spy.call_count==4


def test_immediate_empty_202_context_commit_and_status(async_app,monkeypatch,caplog):
    app,threads,release=async_app;started=Event()
    def refresh():
        assert current_app._get_current_object() is app
        started.set();assert release.wait(5)
        db.session.add(Reading(timestamp=now_hour(),pm2_5=30,source='test-mandalay'))
        return 1
    monkeypatch.setattr(dashboard,'refresh',refresh)
    client=app.test_client()
    with caplog.at_level(logging.INFO):
        response=client.post('/api/refresh',headers=AUTH)
        assert response.status_code==202 and response.data==b''
        assert response.headers['Location']=='/api/refresh/status'
        assert started.wait(2) and threads[0].is_alive()
        state=client.get('/api/refresh/status',headers=AUTH).json['data']
        assert state['status']=='running' and state['started_at'] and not state['finished_at']
        assert response.headers['X-Refresh-Job-ID']==state['job_id']
        release.set();wait(threads)
    state=client.get('/api/refresh/status',headers=AUTH).json['data']
    assert state['status']=='succeeded' and state['finished_at'] and state['imported_rows']==1
    assert db.session.scalar(select(func.count()).select_from(Reading))==1
    assert 'started_at=' in caplog.text and 'completed_at=' in caplog.text and 'imported_rows=1' in caplog.text
    assert 'test-refresh-secret' not in caplog.text
    conn=jobs.acquire();assert conn is not None;jobs.release(conn)


def test_background_error_rolls_back_and_redacts_details(async_app,monkeypatch,caplog):
    app,threads,_=async_app
    def failure():
        db.session.add(Reading(timestamp=now_hour(),pm2_5=30,source='test-mandalay'))
        db.session.flush()
        raise ValueError('test-refresh-secret should never appear in logs or status')
    monkeypatch.setattr(dashboard,'refresh',failure)
    response=app.test_client().post('/api/refresh',headers=AUTH)
    assert response.status_code==202 and not response.data
    wait(threads)
    state=app.test_client().get('/api/refresh/status',headers=AUTH).json['data']
    assert state['status']=='failed' and state['error']=='ValueError' and state['finished_at']
    assert state['imported_rows'] is None
    assert db.session.scalar(select(func.count()).select_from(Reading))==0
    assert 'test-refresh-secret' not in caplog.text
    assert 'test-refresh-secret' not in str(state)
    conn=jobs.acquire();assert conn is not None;jobs.release(conn)


def test_separate_app_engines_coalesce_http_and_cli(async_app,monkeypatch):
    app,threads,release=async_app;started=Event();calls=[]
    def refresh():
        calls.append(1);started.set();assert release.wait(5)
        return 24
    monkeypatch.setattr(dashboard,'refresh',refresh)
    other=create_app({'SQLALCHEMY_DATABASE_URI':app.config['SQLALCHEMY_DATABASE_URI'],'TESTING':True})
    first=app.test_client().post('/api/refresh',headers=AUTH)
    assert started.wait(2)
    second=other.test_client().post('/api/refresh',headers=AUTH)
    assert first.status_code==second.status_code==202 and not second.data
    assert first.headers['X-Refresh-Job-ID']==second.headers['X-Refresh-Job-ID']
    cli=other.test_cli_runner().invoke(args=['refresh-data'])
    assert cli.exit_code==0 and 'already active' in cli.output
    assert len(calls)==len(threads)==1
    release.set();wait(threads)
    # A subsequent request after completion may start a new job.
    third=other.test_client().post('/api/refresh',headers=AUTH)
    assert third.status_code==202 and third.headers['X-Refresh-Job-ID']!=first.headers['X-Refresh-Job-ID']
    wait(threads);assert len(calls)==2
    with other.app_context():db.engine.dispose()


def test_interrupt_detection_and_additive_migration(async_app):
    app,_,_=async_app
    conn=jobs.acquire();job_id=jobs.reserve(conn,'http')
    jobs.change(conn,job_id,status='running',started_at=jobs.utcnow())
    jobs.release(conn) # Simulate worker death: persisted running record but lock gone.
    response=app.test_client().get('/api/refresh/status',headers=AUTH)
    assert response.json['data']['status']=='interrupted'
    assert response.json['data']['error']=='WorkerStopped'
    for _ in range(2):
        assert app.test_cli_runner().invoke(args=['migrate-refresh']).exit_code==0
    assert db.session.scalar(select(jobs.RefreshState.status))=='interrupted'


def test_thread_launch_failure_is_503_and_releases_lock(async_app,monkeypatch):
    app,_,_=async_app
    thread=Mock();thread.start.side_effect=RuntimeError('test-refresh-secret')
    monkeypatch.setattr(jobs,'Thread',Mock(return_value=thread))
    response=app.test_client().post('/api/refresh',headers=AUTH)
    assert response.status_code==503
    assert app.test_client().get('/api/refresh/status',headers=AUTH).json['data']['error']=='LaunchFailed'
    conn=jobs.acquire();assert conn is not None;jobs.release(conn)


def test_real_provider_collection_and_cpcb_complete_in_background(async_app,monkeypatch):
    from app import FIELDS,TZ,SourceObservation,CPCBResult
    from cpcb import POLLUTANTS
    from datetime import timedelta
    app,threads,_=async_app;at=now_hour()
    hourly={'time':[(at-timedelta(hours=i)).astimezone(TZ).replace(tzinfo=None).isoformat() for i in reversed(range(24))]}
    values=dict(pm2_5=45,pm10=75,nitrogen_dioxide=40,sulphur_dioxide=40,ozone=50,carbon_monoxide=1000,us_aqi=500)
    hourly.update({field:[values[field]]*24 for field in FIELDS})
    provider=Mock();provider.json.return_value=dict(hourly=hourly,
        hourly_units={**{p:'μg/m³' for p in POLLUTANTS},'us_aqi':'USAQI'},
        timezone='Asia/Yangon',latitude=22.0,longitude=96.1)
    monkeypatch.setattr(dashboard.requests,'get',Mock(return_value=provider))
    assert app.test_client().post('/api/refresh',headers=AUTH).status_code==202
    wait(threads)
    state=app.test_client().get('/api/refresh/status',headers=AUTH).json['data']
    assert state['status']=='succeeded' and state['imported_rows']==24
    assert db.session.scalar(select(func.count()).select_from(SourceObservation))==24
    assert db.session.get(CPCBResult,at).cpcb_aqi==75



def test_refresh_lock_is_shared_with_another_python_process(async_app):
    import os
    import subprocess
    import sys
    app,_,_=async_app
    conn=jobs.acquire()
    assert conn is not None
    code="""
import os
from app import create_app
import refresh_jobs as jobs
with create_app({'SQLALCHEMY_DATABASE_URI':os.environ['TEST_DATABASE_URL']}).app_context():
    connection=jobs.acquire()
    if connection is not None:
        jobs.release(connection)
        raise SystemExit(2)
    print('blocked')
"""
    try:
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
        assert result.returncode==0,result.stderr
        assert result.stdout.strip()=='blocked'
    finally:jobs.release(conn)
