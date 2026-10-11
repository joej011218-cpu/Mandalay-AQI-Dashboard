"""Smoke-test monitoring UI; requires Playwright and Chromium, no data writes."""
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
base=os.getenv('DASHBOARD_BASE_URL','http://127.0.0.1:5010')
out=Path('build/hardware-browser');out.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH','/usr/bin/chromium'),args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1440,'height':1000},has_touch=True)
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
                    raw=latest['hardware'].get('last_observation')
                    key='co_mg_m3' if label=='CO' else 'nh3_ug_m3'
                    if not raw or raw.get(key) is None:
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
        expect(card.locator('p')).to_contain_text('valid')
    # Invalid raw CO remains visible on Live, never in the AQI.
    original_aqi=page.locator('#aqi').inner_text()
    co=live['data']['measurements']['carbon_monoxide']
    co.update(value=None,status='invalid')
    live['data']['hardware']['last_observation']={
        'timestamp':co['timestamp'],'co_mg_m3':0.75,'co_valid':False,
        'validity':{'co_range_valid':False}}
    page.reload()
    co_card=page.locator('#pollutants .card').filter(has=page.get_by_role('heading',name='CO',exact=True))
    expect(co_card.locator('strong')).to_have_text('0.75')
    expect(co_card.locator('p')).to_contain_text('mg/m³ · ESP32 · Outside calibrated range — not AQI eligible')
    expect(page.locator('#aqi')).to_have_text(original_aqi)
    assert co['value'] is None and not live['data']['hardware']['last_observation']['co_valid']
    co['status']='stale';page.reload()
    expect(co_card.locator('strong')).to_have_text('0.75')
    expect(co_card.locator('p')).to_contain_text('Outside calibrated range — not AQI eligible · stale')
    for value in (None,'0.75'):
        live['data']['hardware']['last_observation']['co_mg_m3']=value
        page.reload();expect(co_card.locator('strong')).to_have_text('—')
    # A mismatched observation timestamp must not supply the displayed estimate.
    live['data']['hardware']['last_observation'].update(co_mg_m3=0.75,timestamp='2000-01-01T00:00:00Z')
    page.reload();expect(co_card.locator('strong')).to_have_text('—')
    # NH3 estimates also remain transparent when invalid/stale.
    live['data']['measurements']['ammonia'].update(value=None,status='invalid',timestamp=co['timestamp'])
    live['data']['hardware']['last_observation'].update(timestamp=co['timestamp'],nh3_ug_m3=90.6,nh3_valid=False)
    page.reload()
    nh3_card=page.locator('#pollutants .card').filter(has=page.get_by_role('heading',name='NH₃',exact=True))
    expect(nh3_card.locator('strong')).to_have_text('90.6')
    expect(nh3_card.locator('p')).to_contain_text('Invalid — not AQI eligible')
    # Raw diagnostic charts: invalid amber points, valid points, correct dates,
    # gaps, source labels and selected-period requests; no DB fixture inserts.
    now=page.evaluate('Date.now()')
    raw={'aqi_standard':'Indian CPCB AQI (provisional combined monitoring)',
         'sample_period_seconds':900,'data':[]}
    for minutes,valid,value in ((90,True,1.5),(75,True,1.6),(60,False,0.75),(45,False,0.8),(0,True,1.7)):
        stamp=page.evaluate('(v)=>new Date(v).toISOString()',now-minutes*60000)
        raw['data'].append(dict(timestamp=stamp,co_mg_m3=value,co_valid=valid,nh3_ug_m3=90.6,nh3_valid=True,device_id='node1',validity={'co_range_valid':valid}))
    raw['data'].append(dict(timestamp=page.evaluate('(v)=>new Date(v).toISOString()',now+1),co_mg_m3=None,co_valid=False,nh3_ug_m3=None,nh3_valid=False))
    page.route('**/api/hardware/history?*',lambda route:route.fulfill(json=raw))
    page.goto(base+'/analytics')
    expect(page.locator('#hardware-window')).to_have_value('1')
    page.select_option('#hardware-window','period')
    co_chart=page.locator('#pollutant-charts .chart-box').filter(has=page.get_by_role('heading',name='CO (mg/m³) · ESP32',exact=True))
    expect(co_chart.locator('circle[data-valid="false"]')).to_have_count(2)
    expect(co_chart.locator('circle[data-valid="true"]')).to_have_count(3)
    expect(co_chart.locator('line[stroke-dasharray="4 3"]')).to_have_count(1)
    expect(co_chart.locator('p')).to_contain_text('diagnostic only — not AQI eligible')
    expect(co_chart.locator('circle').first.locator('title')).to_contain_text('Valid estimate · ESP32')
    expect(co_chart.locator('.chart-time-axis')).to_contain_text(re.compile(r'\d.*(?:am|pm)'))
    # Long offline gap must not be connected (two short segments only).
    expect(co_chart.locator('line[stroke-width="2"]')).to_have_count(2)
    with page.expect_response(lambda r:'/api/hardware/history?days=1' in r.url):
        page.select_option('#days','1')
    expect(co_chart.locator('circle')).to_have_count(5)
    # Recent view expands sparse 15-minute samples instead of squeezing them
    # against a 24-hour axis; the same API rows populate exact-value tables.
    raw['data']=[]
    for minutes,value,valid in ((90,5.0,True),(45,0.332,False),(30,0.328,False),(15,0.334,True)):
        stamp=page.evaluate('(v)=>new Date(v).toISOString()',now-minutes*60000)
        raw['data'].append(dict(timestamp=stamp,co_mg_m3=value,nh3_ug_m3=90.6123456789,
            co_valid=valid,nh3_valid=True,device_id='node1',validity={'co_range_valid':valid}))
    page.select_option('#hardware-window','1')
    expect(co_chart.locator('circle')).to_have_count(3)
    xs=co_chart.locator('circle').evaluate_all('(dots)=>dots.map(d=>Number(d.getAttribute("cx")))')
    assert xs[1]-xs[0]>180,'Recent samples remain crowded'
    expect(page.locator('#pollutant-charts svg')).to_have_count(7)
    expect(page.locator('#hardware-history-rows tr')).to_have_count(3)
    expect(page.locator('#hardware-history-rows')).to_contain_text('0.332')
    expect(page.locator('#hardware-history-rows')).to_contain_text('0.328')
    expect(page.locator('#hardware-history-rows')).to_contain_text('90.6123456789')
    expect(page.locator('#hardware-history-rows')).to_contain_text('Outside calibrated range — not AQI eligible')
    expect(page.locator('#hardware-history-rows')).to_contain_text('Asia/Yangon')
    expect(page.locator('#hardware-history-rows')).to_contain_text('node1')
    # Visible tooltip supports pointer, keyboard and touch/click, with exact values.
    point=co_chart.locator('circle').first
    point.hover()
    tooltip=co_chart.locator('[role="tooltip"]')
    expect(tooltip).to_be_visible()
    expect(tooltip).to_contain_text('CO 0.332 mg/m³')
    expect(tooltip).to_contain_text('Outside calibrated range — not AQI eligible · ESP32')
    expected_time=page.evaluate('(v)=>new Date(v).toLocaleString("en-GB",{timeZone:"Asia/Yangon",dateStyle:"medium",timeStyle:"medium"})',raw['data'][1]['timestamp'])
    expect(tooltip).to_contain_text(expected_time)
    point.focus();page.keyboard.press('Escape');expect(tooltip).to_be_hidden()
    page.keyboard.press('Enter');expect(tooltip).to_be_visible()
    point.click();expect(tooltip).to_be_visible()
    point.press('Escape');point.tap();expect(tooltip).to_be_visible()
    nh3_chart=page.locator('#pollutant-charts .chart-box').filter(has=page.get_by_role('heading',name='NH₃ (µg/m³) · ESP32',exact=True))
    nh3_chart.locator('circle').first.focus()
    expect(nh3_chart.locator('[role="tooltip"]')).to_contain_text('90.6123456789 µg/m³')
    expect(nh3_chart.locator('[role="tooltip"]')).to_contain_text('Valid estimate · ESP32')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),'Analytics mobile overflow'
    with page.expect_response(lambda r:'/api/hardware/history?days=1' in r.url):
        page.select_option('#hardware-window','6')
    expect(page.locator('#hardware-history-rows tr')).to_have_count(4)
    # Empty recent data stay empty, without invented zeros.
    raw['data']=[];page.reload()
    expect(page.locator('#hardware-history-empty')).to_be_visible()
    expect(co_chart.locator('circle')).to_have_count(0)
    # Paginated historical tables do not create thousands of mobile DOM rows.
    raw['data']=[dict(timestamp=page.evaluate('(v)=>new Date(v).toISOString()',now-i*60000),
        co_mg_m3=None,nh3_ug_m3=None,co_valid=False,nh3_valid=False,device_id='node1') for i in reversed(range(55))]
    page.select_option('#hardware-window','period')
    expect(page.locator('#hardware-history-rows tr')).to_have_count(50)
    expect(page.locator('#hardware-page')).to_have_text('1–50 of 55')
    expect(page.locator('#hardware-history-rows')).to_contain_text('Unavailable — not AQI eligible')
    page.locator('#hardware-next').click()
    expect(page.locator('#hardware-history-rows tr')).to_have_count(5)
    expect(page.locator('#hardware-page')).to_have_text('51–55 of 55')
    page.locator('#hardware-previous').click()
    expect(page.locator('#hardware-history-rows tr')).to_have_count(50)
    page.set_viewport_size({'width':1440,'height':1000})
    # Persisted alert status, diagnostic warning and resolved history render.
    alerts={'aqi_standard':raw['aqi_standard'],'data':[
        dict(timestamp=raw['data'][0]['timestamp'],pollutant='carbon_monoxide',alert_type='sensor-quality',
             severity='Quality warning',status='active',value=0.75,unit='mg/m³',threshold=None,
             averaging_hours=None,source='ESP32',device_id='node1',color='#E8C547',
             message='Outside calibrated range — not AQI eligible',resolved_at=None),
        dict(timestamp=raw['data'][0]['timestamp'],pollutant='ammonia',alert_type='rolling-concentration',
             severity='Poor',status='resolved',value=1000,unit='µg/m³',threshold=800,
             averaging_hours=24,source='ESP32',device_id='node1',color='#E8823C',
             message='Covered rolling warning',resolved_at=raw['data'][-1]['timestamp'])]}
    page.route('**/api/monitoring/alerts?*',lambda route:route.fulfill(json=alerts))
    page.goto(base+'/alerts')
    expect(page.locator('#alert-list')).to_contain_text('sensor-quality · Quality warning · active')
    expect(page.locator('#alert-list')).to_contain_text('ESP32 · node1')
    expect(page.locator('#alert-list')).to_contain_text('Outside calibrated range — not AQI eligible')
    expect(page.locator('#alert-list')).to_contain_text('Resolved:')
    page.goto(base+'/')
    page.unroute('**/api/monitoring/latest')
    page.route('**/api/monitoring/latest',lambda route:route.fulfill(json={'aqi_standard':'US EPA AQI','data':{'us_aqi':500}}))
    page.reload()
    expect(page.locator('#status')).to_have_text('Unexpected AQI standard; CPCB data is required.')
    expect(page.locator('#aqi')).to_have_text('—')
    assert not errors,errors
    assert all(url.startswith(base+'/') for url in urls),urls
    assert not any('/api/latest' in url or 'vadodara' in url.lower() for url in urls),urls
    browser.close()
    print('PASS: seven source-labeled cards/charts, empty-chart date axes, daily/period filters, recent hardware views/tooltips/exact history table, diagnostic valid/invalid hardware, alert statuses, missing hardware, theme/mobile, CPCB rejection, no JS errors.')
