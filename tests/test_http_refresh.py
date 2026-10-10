"""No network or database connection: all refresh work is mocked."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock
import pytest
import requests
import app as module

@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv('REFRESH_SECRET', raising=False)
    application = module.create_app({'SQLALCHEMY_DATABASE_URI':
        'postgresql+psycopg://unused:unused@localhost/unused', 'TESTING': True})
    monkeypatch.setattr(module.db.session, 'rollback', Mock())
    return application.test_client()


def test_disabled(client, monkeypatch):
    work = Mock(); monkeypatch.setattr(module, 'refresh', work)
    assert client.post('/api/refresh').status_code == 503
    work.assert_not_called()

@pytest.mark.parametrize('headers', [{}, {'X-Refresh-Secret':'wrong'},
    {'Authorization':'Bearer test-only-secret'}, {'X-Refresh-Secret':'秘密'}])
def test_authentication(client, monkeypatch, headers):
    monkeypatch.setenv('REFRESH_SECRET', 'test-only-secret')
    work = Mock(); monkeypatch.setattr(module, 'refresh', work)
    response = client.post('/api/refresh', headers=headers)
    assert response.status_code == 401
    assert b'test-only-secret' not in response.data
    work.assert_not_called()


def test_get_rejected(client, monkeypatch):
    monkeypatch.setenv('REFRESH_SECRET', 'test-only-secret')
    assert client.get('/api/refresh', headers={'X-Refresh-Secret':'test-only-secret'}).status_code == 405

@pytest.mark.parametrize('error,code', [(None,200), (module.RefreshInProgress(),409),
    (requests.Timeout('test-only-secret'),502), (ValueError('test-only-secret'),502),
    (RuntimeError('test-only-secret'),500)])
def test_result(client, monkeypatch, error, code, caplog):
    monkeypatch.setenv('REFRESH_SECRET', 'test-only-secret')
    work = Mock(return_value=168, side_effect=error)
    monkeypatch.setattr(module, 'refresh', work)
    result = client.post('/api/refresh', headers={'X-Refresh-Secret':'test-only-secret'})
    assert result.status_code == code
    work.assert_called_once_with()
    assert b'test-only-secret' not in result.data
    assert 'test-only-secret' not in caplog.text
    if code == 200:
        assert result.json['status'] == 'success' and result.json['imported'] == 168


def test_refresh_overlap_and_release(monkeypatch):
    entered, release = Event(), Event()
    def work():
        entered.set()
        assert release.wait(5)
        return 12
    monkeypatch.setattr(module, '_refresh', work)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(module.refresh)
        assert entered.wait(5)
        try:
            with pytest.raises(module.RefreshInProgress): module.refresh()
        finally: release.set()
        assert future.result() == 12
    monkeypatch.setattr(module, '_refresh', Mock(side_effect=RuntimeError()))
    with pytest.raises(RuntimeError): module.refresh()
    monkeypatch.setattr(module, '_refresh', Mock(return_value=7))
    assert module.refresh() == 7


def test_cli_reuses_refresh(client, monkeypatch):
    work = Mock(return_value=24)
    monkeypatch.setattr(module, 'refresh', work)
    result = client.application.test_cli_runner().invoke(args=['refresh-data'])
    assert result.exit_code == 0
    assert 'Imported 24' in result.output
    work.assert_called_once_with()
