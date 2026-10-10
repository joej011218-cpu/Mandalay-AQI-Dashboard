"""Smoke-test monitoring UI; requires Playwright and Chromium, no data writes."""
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
base=os.getenv('DASHBOARD_BASE_URL','http://127.0.0.1:5010')
out=Path('build/hardware-browser');out.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH','/usr/bin/chromium'),args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[];urls=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda req:urls.append(req.url))
    for path in ('/','/analytics','/alerts','/about'):
        page.goto(base+path)
        expect(page.locator('#status')).not_to_have_text(re.compile('Loading|Updating'),timeout=30000)
        expect(page.locator('#status')).not_to_have_class('error')
        if path=='/':
            latest=page.request.get(base+'/api/monitoring/latest').json()['data']
            expect(page.locator('#pollutants .card')).to_have_count(7)
            if latest['hardware']['status']!='online':
                for label in ('CO','NH₃'):
                    card=page.locator('#pollutants .card').filter(has=page.get_by_role('heading',name=label,exact=True))
                    expect(card.locator('strong')).to_have_text('—')
                    expect(card.locator('p')).to_contain_text('ESP32')
            page.locator('summary').click()
            expect(page.locator('#calculation-details tr')).to_have_count(7)
            page.locator('#theme').click()
            expect(page.locator('html')).to_have_attribute('data-theme','light')
        if path=='/analytics':
            expect(page.locator('#pollutant-charts svg')).to_have_count(7)
            expect(page.locator('#pollutant-charts .chart-time-axis')).to_have_count(7)
            for axis in page.locator('#pollutant-charts .chart-time-axis').all():
                expect(axis).to_contain_text(re.compile(r'\d.*(?:am|pm)'))
            with page.expect_response(lambda r:'/api/monitoring/history?' in r.url and 'view=daily' in r.url):
                page.select_option('#view','daily')
            expect(page.locator('#status')).not_to_have_text(re.compile('Updating'),timeout=30000)
            expect(page.locator('#status')).not_to_have_class('error')
            expect(page.locator('#pollutant-charts svg')).to_have_count(7)
        page.screenshot(path=str(out/(path.strip('/') or 'live')).strip()+'.png',full_page=True)
        print(path,page.locator('#status').inner_text())
    page.set_viewport_size({'width':390,'height':844});page.goto(base+'/')
    expect(page.locator('#pollutants .card')).to_have_count(7)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),'Mobile overflow'
    # Browser-only fixture: verifies rendering without inserting fabricated DB history.
    live=page.request.get(base+'/api/monitoring/latest').json()
    live['data']['hardware']['status']='online'
    for pollutant,value in (('carbon_monoxide',1.5),('ammonia',1000)):
        measurement=live['data']['measurements'][pollutant]
        measurement.update(value=value,status='valid',source='ESP32',timestamp=live['data']['timestamp'])
    page.route('**/api/monitoring/latest',lambda route:route.fulfill(json=live))
    page.reload()
    expect(page.locator('#hardware-status')).to_contain_text('ESP32: online')
    for label,value in (('CO','1.5'),('NH₃','1,000')):
        card=page.locator('#pollutants .card').filter(has=page.get_by_role('heading',name=label,exact=True))
        expect(card.locator('strong')).to_have_text(value)
        expect(card.locator('p')).to_contain_text('ESP32')
    page.unroute('**/api/monitoring/latest')
    page.route('**/api/monitoring/latest',lambda route:route.fulfill(json={'aqi_standard':'US EPA AQI','data':{'us_aqi':500}}))
    page.reload()
    expect(page.locator('#status')).to_have_text('Unexpected AQI standard; CPCB data is required.')
    expect(page.locator('#aqi')).to_have_text('—')
    assert not errors,errors
    assert all(url.startswith(base+'/') for url in urls),urls
    assert not any('/api/latest' in url or 'vadodara' in url.lower() for url in urls),urls
    browser.close()
    print('PASS: seven source-labeled cards/charts, empty-chart date axes, daily filters, missing hardware, theme/mobile, CPCB rejection, no JS errors.')
