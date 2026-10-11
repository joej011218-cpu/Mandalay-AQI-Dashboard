"""Mandalay sensor persistence and separate monitoring APIs.

Legacy Open-Meteo/prediction tables, endpoints and calculations are untouched.
"""
import hashlib
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import select, UniqueConstraint, Index, func, case, literal, text, and_, or_
from sqlalchemy.dialects.postgresql import insert, JSONB
from app import db, Reading, TZ, CITY, LAT, LON, canonical_filter
import combined_cpcb as combined

UTC=timezone.utc


def decode_payload(payload):
    """Strict JSON shared by MQTT and HTTP; never accept ambiguous/nonfinite data."""
    if len(payload)>16384:raise ValueError('Sensor payload exceeds 16 KiB')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate JSON fields')
            result[key]=value
        return result
    def invalid_constant(value):raise ValueError('Nonfinite JSON constants are forbidden')
    def finite_float(value):
        number=float(value)
        if not math.isfinite(number):raise ValueError('Nonfinite JSON numbers are forbidden')
        return number
    return json.loads(payload.decode('utf-8'),object_pairs_hook=pairs,
                      parse_constant=invalid_constant,parse_float=finite_float)


class SyntheticObservationError(ValueError):
    """Broker tests are never production hardware observations."""


def production_observation_filter():
    # Preserve any old test rows for audit while keeping them out of monitoring.
    raw=HardwareObservation.original_payload
    return and_(*(or_(~raw.has_key(flag),raw.contains({flag:False}))
                  for flag in ('test','synthetic')))



class HardwareObservation(db.Model):
    __tablename__='mandalay_hardware_observations'
    id=db.Column(db.BigInteger,primary_key=True)
    device_id=db.Column(db.String(80),nullable=False)
    city=db.Column(db.String(40),nullable=False,default=CITY)
    event_key=db.Column(db.String(64),nullable=False)
    timestamp=db.Column(db.DateTime(timezone=True),nullable=False)
    received_at=db.Column(db.DateTime(timezone=True),nullable=False)
    timestamp_quality=db.Column(db.String(30),nullable=False)
    co_mg_m3=db.Column(db.Float)
    nh3_ug_m3=db.Column(db.Float)
    temperature=db.Column(db.Float)
    humidity=db.Column(db.Float)
    co_valid=db.Column(db.Boolean,nullable=False)
    nh3_valid=db.Column(db.Boolean,nullable=False)
    validity=db.Column(JSONB,nullable=False)
    original_payload=db.Column(JSONB,nullable=False)
    __table_args__=(UniqueConstraint('device_id','event_key',name='uq_mandalay_hardware_event'),
                   Index('ix_mandalay_hardware_device_time','device_id','timestamp'),
                   Index('ix_mandalay_hardware_received','received_at'))


class MQTTState(db.Model):
    __tablename__='mandalay_mqtt_state'
    name=db.Column(db.String(30),primary_key=True)
    connected=db.Column(db.Boolean,nullable=False)
    heartbeat_at=db.Column(db.DateTime(timezone=True),nullable=False)


def settings():
    def get(name,default=''):
        return current_app.config.get(name,os.getenv(name,default))
    devices=[v.strip() for v in get('MANDALAY_DEVICE_IDS','').split(',') if v.strip()]
    if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',v) for v in devices):raise ValueError('Invalid configured device ID')
    primary=get('HARDWARE_DEVICE_ID','') or (devices[0] if len(devices)==1 else '')
    if primary and primary not in devices:raise ValueError('Primary device must be allowlisted')
    cadence=int(get('HARDWARE_SAMPLE_SECONDS','60'))
    if not 10<=cadence<=900 or 3600%cadence:raise ValueError('Sample cadence must divide one hour and be 10–900 seconds')
    fraction=float(get('HARDWARE_HOURLY_COVERAGE','0.75'))
    if not 0.75<=fraction<=1:raise ValueError('Hourly coverage must be 0.75–1')
    stale=int(get('HARDWARE_STALE_SECONDS','900'))
    if not 60<=stale<=7200:raise ValueError('Hardware stale timeout must be 60–7200 seconds')
    return dict(devices=devices,primary=primary,cadence=cadence,fraction=fraction,stale=stale)


def _numeric(payload,key,minimum=0,maximum=None):
    value=payload.get(key)
    if value is None:return None
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError(f'{key} must be a finite JSON number or null')
    if value<minimum or (maximum is not None and value>maximum):raise ValueError(f'{key} is outside its valid range')
    return float(value)


def validate_payload(payload,received_at=None,retained=False):
    received_at=received_at or datetime.now(UTC)
    if not isinstance(payload,dict):raise ValueError('Payload must be a JSON object')
    for flag in ('test','synthetic'):
        if flag in payload:
            if not isinstance(payload[flag],bool):raise ValueError(f'{flag} must be a JSON boolean')
            if payload[flag]:raise SyntheticObservationError('Synthetic/test MQTT messages are not hardware observations')
    cfg=settings();device=payload.get('device_id')
    if device not in cfg['devices']:raise ValueError('Unknown or unconfigured Mandalay device')
    city=payload.get('city',CITY)
    if city not in (CITY,'Mandalay'):raise ValueError('Only Mandalay payloads are accepted')
    for key in ('co_range_valid','co_valid','nh3_valid','nh3_range_valid','sensor_valid','dht_valid'):
        if key in payload and not isinstance(payload[key],bool):raise ValueError(f'{key} must be a boolean')
    expected={'co_mg_m3':'mg/m3','nh3_ug_m3':'ug/m3'}
    if 'units' in payload:
        if not isinstance(payload['units'],dict):raise ValueError('units must be an object')
        for key,unit in expected.items():
            if key in payload['units'] and payload['units'][key]!=unit:raise ValueError('Contradictory sensor units')
    co=_numeric(payload,'co_mg_m3');nh3=_numeric(payload,'nh3_ug_m3')
    temp=_numeric(payload,'temperature',-50,100);humidity=_numeric(payload,'humidity',0,100)
    for key in ('co_ppm','co_ratio','nh3_ratio'):_numeric(payload,key)
    if not any(k in payload for k in ('co_mg_m3','nh3_ug_m3','temperature','humidity')):raise ValueError('No supported measurement fields')
    event=payload.get('message_id')
    if event is not None and (not isinstance(event,str) or not 1<=len(event)<=100):
        raise ValueError('message_id must be a nonempty string of at most 100 characters')
    original=payload.get('timestamp');at=None
    if isinstance(original,str):
        try:
            at=datetime.fromisoformat(original.replace('Z','+00:00'))
            if at.tzinfo is None:at=None
        except ValueError:pass
    if at is not None:
        at=at.astimezone(UTC)
        if at>received_at+timedelta(seconds=30) or at<received_at-timedelta(days=7):raise ValueError('Measurement timestamp is outside the permitted clock/backfill range')
        quality='measurement';identity='timestamp:'+at.isoformat()
    else:
        if not isinstance(event,str) or not 1<=len(event)<=100:raise ValueError('Untrusted/missing timestamp requires a stable message_id for deduplication')
        if retained:raise ValueError('Undated retained messages cannot become current measurements')
        at=received_at;quality='server_received';identity='message:'+event
    # Firmware IDs remain stable across HTTP retries, including missing clocks.
    # Keep timestamp deduplication for legacy messages without an ID.
    if event is not None:identity='message:'+event
    validity={key:payload[key] for key in ('co_range_valid','co_valid','nh3_valid','nh3_range_valid','sensor_valid','dht_valid','co_ppm','co_ratio','nh3_ratio') if key in payload}
    validity.update(retained=retained,calibration_verified=False,
                    co_flags_supplied=any(k in payload for k in ('co_valid','co_range_valid')),
                    nh3_flags_supplied=any(k in payload for k in ('nh3_valid','nh3_range_valid')))
    co_ok=co is not None and all(payload.get(k,True) for k in ('co_valid','co_range_valid','sensor_valid'))
    nh3_ok=nh3 is not None and all(payload.get(k,True) for k in ('nh3_valid','nh3_range_valid','sensor_valid'))
    return dict(device_id=device,city=CITY,event_key=hashlib.sha256(identity.encode()).hexdigest(),timestamp=at,
                received_at=received_at,timestamp_quality=quality,co_mg_m3=co,nh3_ug_m3=nh3,
                temperature=temp,humidity=humidity,co_valid=co_ok,nh3_valid=nh3_ok,
                validity=validity,original_payload=payload)


def ingest(payload,received_at=None,retained=False):
    values=validate_payload(payload,received_at,retained)
    stmt=insert(HardwareObservation).values(values).on_conflict_do_nothing(constraint='uq_mandalay_hardware_event').returning(HardwareObservation.id)
    try:
        if payload.get('message_id') is not None:
            # Also deduplicate pre-webhook rows whose event_key used timestamps.
            # Transaction locks coordinate the audit lookup across HTTP workers.
            digest=hashlib.sha256((values['device_id']+'\0'+payload['message_id']).encode()).digest()
            lock=int.from_bytes(digest[:8],'big',signed=True)
            db.session.execute(select(func.pg_advisory_xact_lock(lock)))
            existing=db.session.scalar(select(HardwareObservation.id).where(
                HardwareObservation.device_id==values['device_id'],
                HardwareObservation.original_payload['message_id'].astext==payload['message_id']).limit(1))
            if existing is not None:
                db.session.commit()
                return dict(inserted=False,device_id=values['device_id'])
        from hardware_events import lock_device, reconcile
        lock_device(values['device_id'])
        result=db.session.execute(stmt).scalar_one_or_none()
        if result is not None:reconcile(values['received_at'])
        db.session.commit()
        return dict(inserted=result is not None,device_id=values['device_id'])
    except Exception:
        db.session.rollback();raise


def serialize_hardware(row):
    return dict(device_id=row.device_id,city=row.city,timestamp=row.timestamp.astimezone(TZ).isoformat(),
        received_at=row.received_at.astimezone(TZ).isoformat(),timestamp_quality=row.timestamp_quality,
        message_id=row.original_payload.get('message_id'),
        co_mg_m3=row.co_mg_m3,nh3_ug_m3=row.nh3_ug_m3,temperature=row.temperature,humidity=row.humidity,
        co_valid=row.co_valid,nh3_valid=row.nh3_valid,validity=row.validity,source='ESP32',
        units={'co_mg_m3':'mg/m³','nh3_ug_m3':'µg/m³','temperature':'°C','humidity':'%'})


def hardware_status(now=None):
    now=now or datetime.now(UTC);cfg=settings()
    row=None
    if cfg['primary']:
        row=db.session.scalar(select(HardwareObservation).where(production_observation_filter(),HardwareObservation.device_id==cfg['primary'],
            HardwareObservation.city==CITY,HardwareObservation.timestamp<=now).order_by(
                HardwareObservation.timestamp.desc(),HardwareObservation.received_at.desc(),HardwareObservation.id.desc()).limit(1))
    state=db.session.get(MQTTState,'subscriber')
    worker_fresh=state is not None and now-state.heartbeat_at<timedelta(seconds=90)
    age=max((now-row.timestamp).total_seconds(),(now-row.received_at).total_seconds()) if row else None
    status='unconfigured' if not cfg['primary'] else 'offline' if not row else 'stale' if age>cfg['stale'] else 'online'
    transport=current_app.config.get('HARDWARE_TRANSPORT',os.getenv('HARDWARE_TRANSPORT','mqtt'))
    return dict(device_id=cfg['primary'] or None,status=status,age_seconds=age,stale_after_seconds=cfg['stale'],transport=transport,
                mqtt_connected=bool(worker_fresh and state.connected) if transport=='mqtt' else None,
                subscriber_running=worker_fresh if transport=='mqtt' else False,
                last_observation=serialize_hardware(row) if row else None,
                identity_note='Device identity requires authenticated broker ACLs; allowlisted JSON identifiers alone are not authentication')


def _hour(at):
    return at.astimezone(TZ).replace(minute=0,second=0,microsecond=0).astimezone(UTC)


def hourly_hardware(start,end):
    """SQL resampling: equal-weight sample slots, then covered hourly means.

    Hours are closed-right (t-1h,t]. Distinct cadence slots prevent a burst of
    retransmissions from masquerading as an hour of measurements.
    """
    cfg=settings()
    if not cfg['primary']:return {}
    shifted=HardwareObservation.timestamp-text("interval '1 microsecond'")
    slot=func.date_bin(literal(f"{cfg['cadence']} seconds").cast(db.Interval),shifted,
                      literal(datetime(2020,1,1,0,30,tzinfo=UTC)).cast(db.DateTime(timezone=True)))
    conditions=(production_observation_filter(),HardwareObservation.device_id==cfg['primary'],HardwareObservation.city==CITY,
                HardwareObservation.timestamp>start-timedelta(hours=1),HardwareObservation.timestamp<=end)
    slots=select(slot.label('slot'),func.avg(case((HardwareObservation.co_valid,HardwareObservation.co_mg_m3))).label('co'),
                 func.avg(case((HardwareObservation.nh3_valid,HardwareObservation.nh3_ug_m3))).label('nh3')).where(*conditions).group_by(slot).subquery()
    hour_end=func.date_bin(text("interval '1 hour'"),slots.c.slot,literal(datetime(2020,1,1,0,30,tzinfo=UTC)).cast(db.DateTime(timezone=True)))+text("interval '1 hour'")
    query=select(hour_end.label('at'),func.avg(slots.c.co),func.count(slots.c.co),func.avg(slots.c.nh3),func.count(slots.c.nh3)).group_by(hour_end)
    required=math.ceil(3600/cfg['cadence']*cfg['fraction']);result={}
    for at,co,nco,nh3,nnh3 in db.session.execute(query):
        result[at]=dict(carbon_monoxide=float(co) if nco>=required else None,
                        ammonia=float(nh3) if nnh3>=required else None,
                        hardware_coverage={'co_slots':nco,'nh3_slots':nnh3,'required_slots':required,'expected_slots':3600//cfg['cadence']})
    return result


def monitoring_history(days,now=None):
    now=now or datetime.now(UTC);end=_hour(now);start=end-timedelta(days=days)+timedelta(hours=1);lookback=start-timedelta(hours=24)
    model=db.session.scalars(select(Reading).where(canonical_filter(),Reading.timestamp>=lookback,Reading.timestamp<=end).order_by(Reading.timestamp)).all()
    history={r.timestamp:{p:getattr(r,p) for p in combined.OPEN_METEO} for r in model}
    hardware=hourly_hardware(lookback,end)
    for at,values in hardware.items():history.setdefault(at,{}).update(values)
    rows=[];at=start
    while at<=end:
        values=history.get(at,{})
        calc=combined.calculate(at,history)
        rows.append(dict(timestamp=at.astimezone(TZ).isoformat(),**{p:values.get(p) for p in combined.POLLUTANTS},
                         hardware_coverage=values.get('hardware_coverage'),**calc))
        at+=timedelta(hours=1)
    return rows,history


def monitoring_latest(now=None):
    now=now or datetime.now(UTC);rows,history=monitoring_history(1,now);at=_hour(now);state=hardware_status(now)
    model=db.session.scalar(select(Reading).where(canonical_filter(),Reading.timestamp<=now).order_by(Reading.timestamp.desc()).limit(1))
    model_stale=model is None or now-model.timestamp>timedelta(hours=3)
    excluded={}
    if state['status']!='online':
        excluded.update(carbon_monoxide='ESP32 is missing, offline or stale; no modeled CO fallback',ammonia='ESP32 is missing, offline or stale')
    if model_stale:excluded.update({p:'Open-Meteo is missing or stale' for p in combined.OPEN_METEO})
    raw=state['last_observation']
    if raw:
        for p,flag in (('carbon_monoxide','co_valid'),('ammonia','nh3_valid')):
            if not raw[flag]:excluded[p]='Latest ESP32 estimate is invalid; not AQI eligible'
    calc=combined.calculate(at,history,excluded)
    measurements={}
    for p in combined.POLLUTANTS:
        hw=p not in combined.OPEN_METEO
        value=None;stamp=None;status='missing'
        if hw and raw:
            key='co_mg_m3' if p=='carbon_monoxide' else 'nh3_ug_m3'
            valid=raw['co_valid' if p=='carbon_monoxide' else 'nh3_valid']
            stamp=raw['timestamp'];value=raw[key] if valid and state['status']=='online' else None
            status='stale' if state['status']=='stale' else 'valid' if valid else 'invalid'
        elif not hw and model:
            stamp=model.timestamp.astimezone(TZ).isoformat();value=getattr(model,p) if not model_stale else None
            status='stale' if model_stale else 'valid' if value is not None else 'missing'
        measurements[p]=dict(value=value,unit=combined.UNITS[p],source='ESP32' if hw else 'Open-Meteo',timestamp=stamp,status=status)
    return dict(timestamp=at.astimezone(TZ).isoformat(),**{p:m['value'] for p,m in measurements.items()},
                measurements=measurements,hardware=state,model_stale=model_stale,**calc)


def metadata():
    return dict(city=CITY,latitude=LAT,longitude=LON,timezone='Asia/Yangon',
        aqi_standard='Indian CPCB AQI (provisional combined monitoring)',calculation_version=combined.VERSION,
        units=combined.UNITS,sources={p:'Open-Meteo' if p in combined.OPEN_METEO else 'ESP32' for p in combined.POLLUTANTS},
        disclaimer='Open-Meteo modeled values and low-cost MQ estimates; not official regulatory station measurements')


def thresholds(kind):
    defaults=dict(pm2_5=90,pm10=250,nitrogen_dioxide=180,sulphur_dioxide=380,ozone=168,carbon_monoxide=10,ammonia=800)
    key='RAW_ALERT_THRESHOLDS' if kind=='raw' else 'ROLLING_ALERT_THRESHOLDS'
    value=current_app.config.get(key,os.getenv(key,'{}'))
    configured=json.loads(value) if isinstance(value,str) else value
    if not isinstance(configured,dict) or set(configured)-set(combined.POLLUTANTS):raise ValueError('Unknown alert threshold pollutant')
    for p,v in configured.items():
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0:raise ValueError('Alert thresholds must be finite and nonnegative')
    return configured if kind=='raw' else dict(defaults,**configured)


def alert_id(*parts):return hashlib.sha256('|'.join(map(str,parts)).encode()).hexdigest()[:24]


def monitoring_alerts(days,now=None):
    now=now or datetime.now(UTC);rows,_=monitoring_history(days,now);events=[];rolling=thresholds('rolling');raw_limits=thresholds('raw')
    for r in rows:
        if r['cpcb_aqi'] is not None and r['cpcb_aqi']>=201 or r['category']=='Severe':
            events.append(dict(id=alert_id('aqi',r['timestamp'],settings()['primary']),alert_type='cpcb-aqi',pollutant=r['dominant_pollutant'],
                timestamp=r['timestamp'],value=r['cpcb_aqi'],threshold=201,unit='CPCB AQI',averaging_hours=None,source='Combined',
                severity=r['category'],color=r['color'],message=f"Mandalay: {r['precautions']}",provisional=True))
        for p,d in r['pollutant_details'].items():
            if p in combined.OPEN_METEO and d['eligible'] and d['average'] is not None and d['average']>rolling[p]:
                events.append(dict(id=alert_id('rolling',p,r['timestamp'],settings()['primary']),alert_type='rolling-concentration',
                    pollutant=p,timestamp=r['timestamp'],value=d['average'],threshold=rolling[p],unit=d['unit'],averaging_hours=d['averaging_hours'],
                    source=d['source'],severity=combined.category(d['subindex']) if d['subindex'] is not None else 'Severe',
                    color=combined.color(combined.category(d['subindex']) if d['subindex'] is not None else 'Severe'),
                    message='Mandalay covered rolling concentration exceeds the configured threshold. '+combined.PRECAUTIONS[combined.category(d['subindex']) if d['subindex'] is not None else 'Severe'],provisional=True))
    # Hardware warnings are persisted separately; preserve modeled raw warnings.
    if raw_limits:
        for r in rows:
            for p in combined.OPEN_METEO:
                if p in raw_limits and r[p] is not None and r[p]>raw_limits[p]:
                    events.append(dict(id=alert_id('raw',p,r['timestamp']),alert_type='raw-concentration',pollutant=p,timestamp=r['timestamp'],
                        value=r[p],threshold=raw_limits[p],unit=combined.UNITS[p],averaging_hours=0,source='Open-Meteo',severity='Warning',
                        color='#E8C547',message='Raw modeled warning; not a rolling CPCB exceedance. Sensitive people should reduce prolonged outdoor exertion.',provisional=True))
    status=hardware_status(now)
    if status['status']!='online':
        last=status['last_observation'];stamp=last['timestamp'] if last else now.astimezone(TZ).isoformat()
        events.append(dict(id=alert_id('offline',status['device_id'],last['timestamp'] if last else 'never'),alert_type='sensor-status',pollutant='CO / NH3',
            timestamp=stamp,value=None,threshold=status['stale_after_seconds'],unit='seconds',averaging_hours=None,source='ESP32',severity=status['status'],
            color='#E8C547',message='ESP32 hardware is '+status['status']+'; CO/NH3 are unavailable and no Open-Meteo CO is substituted.',provisional=True))
    from hardware_events import events as saved_events
    events.extend(saved_events(days,now))
    # The persistent episode replaces the transient configured-device warning.
    if settings()['primary']:events=[e for e in events if not (e['alert_type']=='sensor-status' and 'status' not in e)]
    unique={e['id']:e for e in events}
    return sorted(unique.values(),key=lambda e:e['timestamp'],reverse=True)


def register(app):
    from hardware_events import HardwareAlert
    bp=Blueprint('mandalay_hardware',__name__)
    def days():
        value=int(request.args.get('days','7'))
        if not 1<=value<=366:raise ValueError('days must be 1–366')
        return value
    def period():
        value=request.args.get('view','hourly')
        if value not in ('hourly','daily'):raise ValueError('view must be hourly or daily')
        return value
    @bp.errorhandler(ValueError)
    def validation_error(error):return jsonify(error=str(error)),400
    @bp.get('/api/hardware/status')
    def status_route():return jsonify(data=hardware_status(),**metadata())
    @bp.get('/api/hardware/latest')
    def hardware_latest():
        status=hardware_status();return jsonify(data=status['last_observation'],hardware_status=status['status'],**metadata())
    @bp.get('/api/hardware/history')
    def hardware_history():
        limit=days();cfg=settings();now=datetime.now(UTC)
        rows=db.session.scalars(select(HardwareObservation).where(production_observation_filter(),HardwareObservation.device_id==cfg['primary'],HardwareObservation.city==CITY,
            HardwareObservation.timestamp>=now-timedelta(days=limit),HardwareObservation.timestamp<=now).order_by(HardwareObservation.timestamp).limit(50001)).all()
        if len(rows)>50000:return jsonify(error='Raw history exceeds 50,000 rows; request a shorter period or hourly monitoring history'),413
        return jsonify(data=[serialize_hardware(r) for r in rows],sample_period_seconds=cfg['cadence'],**metadata())
    @bp.get('/api/monitoring/latest')
    def latest():return jsonify(data=monitoring_latest(),**metadata())
    @bp.get('/api/monitoring/history')
    def history_route():
        view=period();rows,_=monitoring_history(days())
        if view=='daily':
            grouped=defaultdict(list)
            for row in rows:grouped[row['timestamp'][:10]].append(row)
            daily=[]
            for date,group in grouped.items():
                item=dict(timestamp=date+'T00:00:00+06:30',daily_summary=True)
                for p in (*combined.POLLUTANTS,'cpcb_aqi'):
                    vals=[r[p] for r in group if r[p] is not None]
                    item[p]=sum(vals)/len(vals) if vals else None
                item['valid_hour_counts']={p:sum(r[p] is not None for r in group) for p in combined.POLLUTANTS};daily.append(item)
            rows=daily
        else:
            for row in rows:row.pop('pollutant_details',None)
        return jsonify(data=rows,view=view,**metadata())
    @bp.get('/api/monitoring/analytics')
    def analytics():
        rows,_=monitoring_history(days());summary={}
        for p in (*combined.POLLUTANTS,'cpcb_aqi'):
            vals=[r[p] for r in rows if r[p] is not None]
            summary[p]=dict(mean=sum(vals)/len(vals) if vals else None,min=min(vals) if vals else None,max=max(vals) if vals else None,count=len(vals))
        return jsonify(summary=summary,observations=sum(any(r[p] is not None for p in combined.POLLUTANTS) for r in rows),
                       unavailable_aqi_count=sum(r['cpcb_aqi'] is None for r in rows),**metadata())
    @bp.get('/api/monitoring/alerts')
    def alerts():return jsonify(data=monitoring_alerts(days()),**metadata())
    @app.cli.command('migrate-hardware')
    def migrate():
        # CREATE IF NOT EXISTS only; never ALTER/DROP prediction or legacy data.
        from refresh_jobs import migration_connection
        with migration_connection() as conn:
            HardwareObservation.__table__.create(conn,checkfirst=True)
            MQTTState.__table__.create(conn,checkfirst=True)
            HardwareAlert.__table__.create(conn,checkfirst=True)
            # Safe for an already-created alert table; never rewrites old rows.
            conn.execute(text('ALTER TABLE mandalay_hardware_alert_events ADD COLUMN IF NOT EXISTS evidence_at TIMESTAMPTZ'))
            conn.execute(select(HardwareObservation.__table__).limit(0))
            conn.execute(select(MQTTState.__table__).limit(0))
            conn.execute(select(HardwareAlert.__table__).limit(0))
        print('Additive Mandalay hardware tables and indexes are ready')

    app.register_blueprint(bp)
    from hardware_webhook import register as register_webhook
    register_webhook(app)
