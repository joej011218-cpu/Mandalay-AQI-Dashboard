"""Run against a locally started dashboard with real Mandalay data.
Install playwright separately; set CHROMIUM_PATH if needed.
"""
import os
import re
from playwright.sync_api import sync_playwright, expect

base = os.getenv('DASHBOARD_BASE_URL', 'http://127.0.0.1:5000')
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH', '/usr/bin/chromium'),
                              headless=True, args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors, urls = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda req: urls.append(req.url))
    for route in ('/', '/analytics', '/alerts', '/about'):
        page.goto(base + route)
        expect(page.locator('#status')).not_to_have_text(re.compile('Loading|Updating'), timeout=15000)
        expect(page.locator('#status')).not_to_have_class('error')
        print(route, page.locator('#status').inner_text())
        if route == '/':
            latest = page.request.get(base + '/api/latest').json()
            assert latest['aqi_standard'].startswith('Indian CPCB AQI')
            expect(page.locator('#aqi')).to_have_text(str(latest['data']['cpcb_aqi']))
            expect(page.locator('#dominant')).not_to_have_text('Unavailable')
            page.locator('summary').click()
            expect(page.locator('#calculation-details tr')).to_have_count(6)
            expect(page.locator('#aqi')).not_to_have_text('—')
            expect(page.locator('#pollutants .card')).to_have_count(6)
            page.locator('#theme').click()
            expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        if route == '/analytics':
            expect(page.locator('#pollutant-charts svg')).to_have_count(6)
            page.select_option('#days', '1')
            expect(page.locator('#status')).to_have_text(re.compile(r'24 Mandalay observations'), timeout=15000)
        page.screenshot(path='/tmp/mandalay-' + (route.strip('/') or 'live') + '.png', full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.goto(base + '/')
    expect(page.locator('#aqi')).not_to_have_text('—')
    assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile overflow'
    page.screenshot(path='/tmp/mandalay-mobile.png', full_page=True)
    # A client receiving a legacy standard must reject it, never relabel it.
    page.route('**/api/latest', lambda route: route.fulfill(json={'aqi_standard':'US EPA AQI','data':{'us_aqi':500}}))
    page.reload()
    expect(page.locator('#status')).to_have_text('Unexpected AQI standard; CPCB data is required.')
    expect(page.locator('#aqi')).to_have_text('—')
    assert not errors, errors
    assert all(url.startswith(base + '/') for url in urls), urls
    print('PASS: CPCB desktop pages, dominance, eligibility, charts, theme, mobile width, rejection of legacy AQI, no JS errors, same-origin requests')
    browser.close()
