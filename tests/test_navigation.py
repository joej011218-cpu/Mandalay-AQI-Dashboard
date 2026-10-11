"""Navigation checks need no database connection or writes."""
import pytest
from app import create_app
from cpcb import VERSION

URL = 'https://mandalay-aqi-prediction.onrender.com/prediction.html'

@pytest.mark.parametrize('url,standard,linked', [
    (URL, VERSION, True), (URL, '', False), (URL, 'US-AQI', False),
    ('https://aiot-public-dashboard-2.onrender.com/', VERSION, False),
    ('https://other-prediction.example/', VERSION, False), ('', VERSION, False)])
def test_mandalay_navigation(monkeypatch, url, standard, linked):
    monkeypatch.setenv('PREDICTION_URL', url)
    monkeypatch.setenv('PREDICTION_AQI_STANDARD', standard)
    client = create_app({'SQLALCHEMY_DATABASE_URI':
        'postgresql+psycopg://unused:unused@localhost/unused', 'TESTING': True}).test_client()
    for path in ['/', '/analytics', '/alerts', '/about']:
        response = client.get(path)
        assert response.status_code == 200
        assert (f'href="{URL}"'.encode() in response.data) == linked
        assert b'href="/"' in response.data
        assert b'Vadodara' not in response.data
        assert b'aiot-public-dashboard-2' not in response.data


def test_live_omits_only_requested_sections():
    client = create_app({'SQLALCHEMY_DATABASE_URI':
        'postgresql+psycopg://unused:unused@localhost/unused', 'TESTING': True}).test_client()
    live = client.get('/').get_data(as_text=True)
    for removed in ('CPCB averaging and pollutant eligibility', 'Air quality trends',
                    'id="calculation-details"', 'id="history"', 'id="chart"', 'id="days"', 'id="view"'):
        assert removed not in live
    for preserved in ('id="pollutants"', 'id="aqi"', 'id="category"', 'id="dominant"', 'id="hardware-status"'):
        assert preserved in live
    for path in ('/analytics', '/alerts', '/about'):
        html = client.get(path).get_data(as_text=True)
        assert 'Air quality trends' in html
        assert 'id="history"' in html and 'id="calculation-details"' in html
    analytics = client.get('/analytics').get_data(as_text=True)
    assert '<option value="30">30D</option>' in analytics
    assert 'id="pollutant-charts"' in analytics
