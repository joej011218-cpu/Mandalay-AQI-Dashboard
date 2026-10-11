"""Small synchronous cron responses, with no production database access."""
import logging
from unittest.mock import Mock

import pytest
import requests

import app as dashboard
from app import create_app, db

AUTH = {'Authorization': 'Bearer test-refresh-secret'}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('REFRESH_TOKEN', 'test-refresh-secret')
    application = create_app({'SQLALCHEMY_DATABASE_URI':
        'postgresql+psycopg://unused:unused@localhost/unused', 'TESTING': True})
    return application.test_client()


def small_json(response, code, expected):
    assert response.status_code == code
    assert response.is_json and response.json == expected
    assert len(response.data) < 256
    assert b'<html' not in response.data and b'Traceback' not in response.data


@pytest.mark.parametrize('count', [0, 24, 172])
def test_synchronous_success_returns_actual_count(client, monkeypatch, count):
    refresh = Mock(return_value=count)
    enqueue = Mock(side_effect=AssertionError('HTTP must not launch a background job'))
    monkeypatch.setattr(dashboard, 'refresh', refresh)
    monkeypatch.setattr('refresh_jobs.enqueue', enqueue)
    response = client.post('/api/refresh', headers=AUTH)
    small_json(response, 200, {'status': 'ok', 'imported': count})
    refresh.assert_called_once_with()
    enqueue.assert_not_called()
    assert 'Location' not in response.headers and 'X-Refresh-Job-ID' not in response.headers


@pytest.mark.parametrize('authorization', ['', 'Bearer wrong', 'Basic test-refresh-secret', 'Bearer tést'])
def test_invalid_auth_never_refreshes(client, monkeypatch, authorization):
    refresh = Mock()
    monkeypatch.setattr(dashboard, 'refresh', refresh)
    response = client.post('/api/refresh', headers={'Authorization': authorization})
    small_json(response, 401, {'error': 'Unauthorized'})
    refresh.assert_not_called()


def test_unconfigured_token(client, monkeypatch):
    monkeypatch.delenv('REFRESH_TOKEN')
    refresh = Mock()
    monkeypatch.setattr(dashboard, 'refresh', refresh)
    small_json(client.post('/api/refresh', headers=AUTH), 503,
               {'error': 'Refresh is disabled until REFRESH_TOKEN is configured'})
    refresh.assert_not_called()


@pytest.mark.parametrize('error', [requests.Timeout('private'), requests.HTTPError('private'),
                                   KeyError('private'), ValueError('private'), TypeError('private')])
def test_provider_errors_are_small_and_rollback(client, monkeypatch, error):
    monkeypatch.setattr(dashboard, 'refresh', Mock(side_effect=error))
    rollback = Mock()
    monkeypatch.setattr(db.session, 'rollback', rollback)
    small_json(client.post('/api/refresh', headers=AUTH), 502,
               {'error': 'Air quality provider unavailable or invalid'})
    rollback.assert_called_once_with()


def test_unexpected_error_is_logged_and_rolled_back(client, monkeypatch, caplog):
    monkeypatch.setattr(dashboard, 'refresh', Mock(side_effect=RuntimeError('private details' * 10000)))
    rollback = Mock()
    monkeypatch.setattr(db.session, 'rollback', rollback)
    with caplog.at_level(logging.ERROR):
        response = client.post('/api/refresh', headers=AUTH)
    small_json(response, 500, {'error': 'Refresh failed'})
    rollback.assert_called_once_with()
    assert any(r.message == 'Mandalay refresh failed' and r.exc_info for r in caplog.records)


def test_wrong_method_is_json_without_changing_other_routes(client):
    response = client.get('/api/refresh')
    small_json(response, 405, {'error': 'Method Not Allowed'})
    assert 'POST' in response.headers['Allow']
    assert client.get('/missing').content_type.startswith('text/html')
