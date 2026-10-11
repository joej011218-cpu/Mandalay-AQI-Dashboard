"""Smoke-test monitoring UI; requires Playwright and Chromium, no data writes."""
import os
import re
from datetime import datetime, timezone
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
            expect(page.get_by_text('CPCB averaging and pollutant eligibility',exact=True)).to_have_count(0)
            expect(page.get_by_role('heading',name='Air quality trends',exact=True)).to_have_count(0)
            expect(page.locator('#calculation-details, #history, #chart, #days, #view')).to_have_count(0)
            page.locator('#theme').click()
            expect(page.locator('html')).to_have_attribute('data-theme','light')
        if path=='/analytics':
            expect(page.get_by_role('heading',name='ESP32 measurement history',exact=True)).to_have_count(0)
            expect(page.get_by_role('heading',name='Period statistics · hourly records',exact=True)).to_have_count(0)
            expect(page.locator('#hardware-history-table, #hardware-history-caption, .hardware-pagination, #stats')).to_have_count(0)
            expect(page.locator('#analytics > :last-child')).to_have_attribute('id','pollutant-charts')
            expect(page.locator('#pollutant-charts svg')).to_have_count(7)
            expect(page.locator('#pollutant-charts .chart-time-axis')).to_have_count(7)
            for axis in page.locator('#pollutant-charts .chart-time-axis').all():
                expect(axis).to_contain_text(re.compile(r'[A-Z][a-z]{2} \d{1,2}'))
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
    expect(page.locator('#hardware-window')).to_have_value('period')
    co_chart=page.locator('#pollutant-charts .chart-box').filter(has=page.get_by_role('heading',name='CO (mg/m³) · ESP32',exact=True))
    expect(co_chart.locator('circle[data-valid="false"]')).to_have_count(2)
    expect(co_chart.locator('circle[data-valid="true"]')).to_have_count(3)
    expect(co_chart.locator('polyline[stroke-dasharray="4 3"]')).to_have_count(1)
    expect(co_chart.locator('p')).to_contain_text('diagnostic only — not AQI eligible')
    expect(co_chart.locator('circle').first.locator('title')).to_contain_text('Valid estimate · ESP32')
    expect(co_chart.locator('.chart-time-axis')).to_contain_text(re.compile(r'[A-Z][a-z]{2} \d{1,2}'))
    # Long offline gap must not be connected (two short segments only).
    expect(co_chart.locator('polyline[stroke-width="2"]')).to_have_count(2)
    with page.expect_response(lambda r:'/api/hardware/history?days=1' in r.url):
        page.select_option('#days','1')
    expect(co_chart.locator('circle')).to_have_count(5)
    # Recent view expands sparse 15-minute samples instead of squeezing them
    # against a 24-hour axis; exact values remain available through tooltips.
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
    expect(co_chart.locator('circle')).to_have_count(4)
    # Historical period selection restores the full range after an explicit zoom.
    with page.expect_response(lambda r:'/api/hardware/history?days=7' in r.url):
        page.select_option('#days','7')
    expect(page.locator('#hardware-window')).to_have_value('period')
    expect(co_chart.locator('circle')).to_have_count(4)
    # Four earlier 15-minute observations are never trimmed to the latest hour.
    earlier=page.evaluate('(v)=>(Math.floor((v+6.5*3600000)/3600000)-3)*3600000-6.5*3600000+11*60000',now)
    raw['data']=[dict(timestamp=page.evaluate('(v)=>new Date(v).toISOString()',earlier+i*15*60000),
        co_mg_m3=value,nh3_ug_m3=90.6+i,co_valid=False,nh3_valid=True,device_id='node1',
        validity={'co_range_valid':False}) for i,value in enumerate((0.332,0.328,0.331,0.329))]
    page.reload()
    expect(co_chart.locator('circle[data-valid="false"]')).to_have_count(4)
    expect(nh3_chart.locator('circle[data-valid="true"]')).to_have_count(4)
    titles=co_chart.locator('circle title').all_text_contents()
    for index,value in enumerate((0.332,0.328,0.331,0.329)):
        assert str(value) in titles[index] and 'Outside calibrated range' in titles[index]
        expected=page.evaluate('(v)=>new Date(v).toLocaleString("en-GB",{timeZone:"Asia/Yangon",dateStyle:"medium",timeStyle:"medium"})',raw['data'][index]['timestamp'])
        assert expected in titles[index]
    assert page.locator('#days option').all_text_contents()==['24H','7D','30D']
    # Every selected range keeps raw observations and adaptive calendar axes.
    for period in ('1','7','30'):
        with page.expect_response(lambda r:'/api/hardware/history?days='+period in r.url):
            page.select_option('#days',period)
        expect(page.locator('#hardware-window')).to_have_value('period')
        expect(co_chart.locator('circle[data-valid="false"]')).to_have_count(4)
        expect(nh3_chart.locator('circle[data-valid="true"]')).to_have_count(4)
        expect(page.locator('.hourly-time-axis')).to_have_count(8)
        labels=page.locator('.hourly-time-axis > span').all_text_contents()
        pattern=r'\d{1,2} (AM|PM)' if period=='1'  else r'[A-Z][a-z]{2} \d{1,2}'
        assert labels and all(re.fullmatch(pattern,label) for label in labels),labels
        for width in (390,1440):
            page.set_viewport_size({'width':width,'height':1000})
            page.wait_for_timeout(100)
            boxes=page.locator('.hourly-time-axis').evaluate_all('(axes)=>axes.map(a=>[...a.querySelectorAll("span > span")].map(l=>{const r=l.getBoundingClientRect();return {left:r.left,right:r.right}}))')
            assert all(all(row[i]['right']<=row[i+1]['left']+1 for i in range(len(row)-1)) for row in boxes)
        stamps=page.locator('.hourly-time-axis > span').evaluate_all('(ticks)=>ticks.map(t=>t.dataset.timestamp)')
        for stamp in stamps:
            local=page.evaluate('(v)=>new Date(Date.parse(v)+6.5*3600000).toISOString()',stamp)
            assert local[14:19]=='00:00',local
            if period!='1':assert local[11:13]=='00',local
        if period!='1':
            axes=page.locator('.hourly-time-axis').evaluate_all('(axes)=>axes.map(a=>[...a.children].map(t=>Date.parse(t.dataset.timestamp)))')
            assert all(all(row[i+1]-row[i]==86400000 for i in range(len(row)-1)) for row in axes)
            assert len(axes[-1])==int(period),axes[-1]
            for box in page.locator('.daily-chart-scroll').all():
                box.evaluate('(box)=>box.scrollLeft=box.scrollWidth')
                assert box.evaluate('(box)=>box.scrollLeft+box.clientWidth>=box.scrollWidth-2')
                box.evaluate('(box)=>box.scrollLeft=0')
        co_chart.locator('circle').first.focus()
        expect(co_chart.locator('[role="tooltip"]')).to_contain_text('0.332 mg/m³')
        expect(co_chart.locator('[role="tooltip"]')).to_contain_text('Outside calibrated range — not AQI eligible · ESP32')
        expect(co_chart.locator('[role="tooltip"]')).to_contain_text(page.evaluate('(v)=>measurementTime(v)',raw['data'][0]['timestamp']))
        if period!='1':
            page.wait_for_timeout(100)
            assert co_chart.evaluate('(box)=>{const tip=box.querySelector("[role=tooltip]").getBoundingClientRect(),card=box.getBoundingClientRect();return tip.left>=card.left && tip.right<=card.right+1}')

    page.set_viewport_size({'width':1440,'height':1000})
    page.evaluate('document.documentElement.dataset.theme="dark"')
    page.screenshot(path='/tmp/mandalay-analytics-desktop.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path='/tmp/mandalay-analytics-mobile.png',full_page=True)
    # Empty recent data stay empty, without invented zeros.
    saved_observations=raw['data']
    raw['data']=[];page.reload()
    expect(co_chart.locator('circle')).to_have_count(0)
    raw['data']=saved_observations
    page.set_viewport_size({'width':1440,'height':1000})
    page.select_option("#days","1")
    # Clock ticks use UTC+06:30 boundaries, independent of sample minutes.
    aligned=page.evaluate('()=>hourlyTicks(Date.parse("2026-10-11T12:39:00+06:30"),Date.parse("2026-10-11T18:39:00+06:30"),900)')
    assert aligned['step']==1 and len(aligned['timestamps'])==6
    assert all(datetime.fromtimestamp(t/1000,timezone.utc).minute==30 for t in aligned['timestamps'])
    for width in (240,500,900):
        result=page.evaluate('(width)=>hourlyTicks(Date.parse("2026-10-11T00:39:00+06:30"),Date.parse("2026-10-12T00:39:00+06:30"),width)',width)
        assert result['step'] in (2,3,6,12,24)
        assert len(result['timestamps'])<=max(1,width//70)+1
    long=page.evaluate('()=>hourlyTicks(Date.parse("2026-10-01T02:39:00+06:30"),Date.parse("2026-10-31T03:39:00+06:30"),300)')
    assert long['step']>=24 and long['step']%24==0
    fixed=[dict(timestamp='2026-10-11T12:39:00+06:30',carbon_monoxide=None),
           dict(timestamp='2026-10-11T12:54:00+06:30',carbon_monoxide=0.332,sensor_valid=False,validity_label='Outside calibrated range — not AQI eligible',device_id='node1'),
           dict(timestamp='2026-10-11T13:09:00+06:30',carbon_monoxide=0.328,sensor_valid=False,validity_label='Outside calibrated range — not AQI eligible',device_id='node1'),
           dict(timestamp='2026-10-11T13:24:00+06:30',carbon_monoxide=None),
           dict(timestamp='2026-10-11T16:09:00+06:30',carbon_monoxide=0.33,sensor_valid=False,validity_label='Outside calibrated range — not AQI eligible',device_id='node1'),
           dict(timestamp='2026-10-11T18:39:00+06:30',carbon_monoxide=None)]
    svg=co_chart.locator('svg')
    svg.evaluate('(svg,rows)=>chart(rows,svg,"carbon_monoxide",svg.parentElement.querySelector("p"),true,900*1500)',fixed)
    expect(co_chart.locator('circle')).to_have_count(3)
    expect(co_chart.locator('polyline[stroke-dasharray="4 3"]')).to_have_count(1)
    expect(co_chart.locator('circle').first.locator('title')).to_contain_text('12:54:00')
    first_x=co_chart.locator('circle').first.get_attribute('cx')
    assert abs(float(first_x)-(40+15/360*820))<0.001,'Measurements moved to hourly boundaries'
    for width in (1440,390):
        page.set_viewport_size({'width':width,'height':1000})
        page.wait_for_function('(svg)=>svg.nextElementSibling.dataset.intervalHours!==undefined',arg=svg.element_handle())
        # Wait until resize callback positions the anchors in current SVG pixels.
        page.wait_for_function('(svg)=>{const axis=svg.nextElementSibling,rect=svg.getBoundingClientRect(),scale=Math.min(rect.width/900,rect.height/260),inset=(rect.width-900*scale)/2,start=Date.parse("2026-10-11T12:39:00+06:30"),span=6*3600000;return [...axis.children].every(t=>Math.abs(parseFloat(t.style.left)-(inset+(40+(Date.parse(t.dataset.timestamp)-start)/span*820)*scale))<.5)}',arg=svg.element_handle())
        ticks=co_chart.locator('.hourly-time-axis > span').evaluate_all('(ticks)=>ticks.map(t=>({at:t.dataset.timestamp,text:t.textContent,left:parseFloat(t.style.left)}))')
        assert ticks and all(re.fullmatch(r'\d{1,2} (AM|PM)',t['text']) for t in ticks)
        assert all(datetime.fromisoformat(t['at'].replace('Z','+00:00')).minute==30 for t in ticks)
        assert co_chart.locator('circle').first.get_attribute('cx')==first_x
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'Hourly labels overflow mobile layout'
        labels=co_chart.locator('.hourly-time-axis > span > span').evaluate_all('(labels)=>labels.map(t=>{const r=t.getBoundingClientRect();return {left:r.left,right:r.right}})')
        assert all(labels[i]['right']<=labels[i+1]['left']+1 for i in range(len(labels)-1)),'Hourly labels overlap'
    midnight=[dict(timestamp='2026-10-11T22:00:00+06:30',carbon_monoxide=None),dict(timestamp='2026-10-12T02:00:00+06:30',carbon_monoxide=None)]
    svg.evaluate('(svg,rows)=>chart(rows,svg,"carbon_monoxide",svg.parentElement.querySelector("p"),true)',midnight)
    assert 'Oct' not in co_chart.locator('.hourly-time-axis').inner_text()
    assert any('12 Oct' in title for title in co_chart.locator('.hourly-time-axis > span').evaluate_all('(ticks)=>ticks.map(t=>t.title)'))
    expect(co_chart.locator('.hourly-time-axis')).to_contain_text('12 AM')
    midnight_labels=co_chart.locator('.hourly-time-axis > span > span').evaluate_all('(labels)=>labels.map(t=>{const r=t.getBoundingClientRect();return {left:r.left,right:r.right}})')
    assert all(midnight_labels[i]['right']<=midnight_labels[i+1]['left']+1 for i in range(len(midnight_labels)-1)),'Midnight time labels overlap'
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
    print('PASS: seven source-labeled cards/charts, empty-chart time axes, daily/period filters, hourly Yangon ticks/alignment/midnight/responsive widths, recent hardware views/exact tooltips, removed history/statistics sections, diagnostic valid/invalid hardware, alert statuses, missing hardware, theme/mobile, CPCB rejection, no JS errors.')
