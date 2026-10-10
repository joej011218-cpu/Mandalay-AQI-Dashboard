"""Mandalay-only PostgreSQL dashboard with separately calculated CPCB AQI."""
import hmac
import math
import os
from threading import Lock
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import lazyload

from cpcb import POLLUTANTS, VERSION, calculate

CITY = 'Mandalay, Myanmar'
LAT, LON = 21.9588, 96.0891
TZ = ZoneInfo('Asia/Yangon')
FIELDS = (*POLLUTANTS, 'us_aqi')
LOCK_ID = 219588960891
# Only Yangon whole-hour timestamps belong to the canonical timeline. Earlier
# UTC API rows remain stored but must not double-count the overlapping grid.
def canonical_filter():
    return func.mod(func.extract('epoch', Reading.timestamp), 3600) == 1800


db = SQLAlchemy()


class Reading(db.Model):
    __tablename__ = 'mandalay_readings'
    timestamp = db.Column(db.DateTime(timezone=True), primary_key=True)
    pm2_5 = db.Column(db.Float)
    pm10 = db.Column(db.Float)
    carbon_monoxide = db.Column(db.Float)
    nitrogen_dioxide = db.Column(db.Float)
    sulphur_dioxide = db.Column(db.Float)
    ozone = db.Column(db.Float)
    us_aqi = db.Column(db.Float)  # Raw provider value; never a CPCB input.
    source = db.Column(db.String(40), nullable=False)
    cpcb_result = db.relationship('CPCBResult', uselist=False, lazy='joined')


class SourceObservation(db.Model):
    __tablename__ = 'mandalay_source_observations'
    timestamp = db.Column(db.DateTime(timezone=True), primary_key=True)
    source_id = db.Column(db.String(100), primary_key=True)
    original_values = db.Column(JSONB, nullable=False)
    provenance = db.Column(JSONB, nullable=False)


class CPCBResult(db.Model):
    __tablename__ = 'mandalay_cpcb_aqi'
    timestamp = db.Column(db.DateTime(timezone=True), db.ForeignKey('mandalay_readings.timestamp'), primary_key=True)
    cpcb_aqi = db.Column(db.Integer)
    status = db.Column(db.String(20), nullable=False)
    calculation_version = db.Column(db.String(50), nullable=False)
    calculated_at = db.Column(db.DateTime(timezone=True), nullable=False)
    details = db.Column(JSONB, nullable=False)


def metadata():
    return dict(city=CITY, latitude=LAT, longitude=LON, timezone='Asia/Yangon',
                aqi_standard='Indian CPCB AQI (hourly rolling)',
                calculation_version=VERSION, pollutant_unit='µg/m³',
                co_subindex_unit='mg/m³', modeled_data=True)


def serialize(row, include_details=True):
    from cpcb import PRECAUTIONS
    result = row.cpcb_result
    if result and result.calculation_version == VERSION:
        calculated = dict(result.details)
    else:
        calculated = dict(cpcb_aqi=None, status='unavailable', category='Unavailable', color='#8A97A0',
                          dominant_pollutant=None, dominant_pollutants=[], eligible_pollutants=[],
                          pollutant_details={}, severe_pollutants=[], calculation_version=VERSION,
                          reasons=['CPCB results are absent or outdated; run recalculate-cpcb'],
                          precautions=PRECAUTIONS['Unavailable'])
    if not include_details:
        calculated.pop("pollutant_details", None)
    return dict(timestamp=row.timestamp.astimezone(TZ).isoformat(),
                **{field: getattr(row, field) for field in POLLUTANTS},
                raw_aqi={'us_aqi': row.us_aqi, 'used_for_cpcb': False},
                source=row.source, data_source='Open-Meteo / CAMS (Mandalay historical workbook or current hourly response)',
                **calculated)


def _recalculate():
    rows = db.session.scalars(select(Reading).options(lazyload(Reading.cpcb_result))
        .where(canonical_filter()).order_by(Reading.timestamp)).all()
    history = {r.timestamp: {f: getattr(r, f) for f in POLLUTANTS} for r in rows}
    results = []
    now = datetime.now(timezone.utc)
    for row in rows:
        details = calculate(row.timestamp, history)
        results.append(dict(timestamp=row.timestamp, cpcb_aqi=details['cpcb_aqi'],
                            status=details['status'], calculation_version=VERSION,
                            calculated_at=now, details=details))
    for offset in range(0, len(results), 300):
        stmt = insert(CPCBResult).values(results[offset:offset+300])
        db.session.execute(stmt.on_conflict_do_update(index_elements=['timestamp'],
            set_={field: getattr(stmt.excluded, field) for field in ('cpcb_aqi','status','calculation_version','calculated_at','details')}))
    return len(results)


def recalculate():
    try:
        db.session.execute(select(func.pg_advisory_xact_lock(LOCK_ID)))
        count = _recalculate()
        db.session.commit()
        db.session.expire_all()
        return count
    except Exception:
        db.session.rollback()
        raise


def upsert(rows, snapshots=None):
    """One atomic transaction updates raw values and every affected AQI result.

    All history is rebuilt for this single-city dataset (under 9,000 hours/year).
    NULL pollutants replace NULLs correctly; old values are never borrowed to
    mask a new source's missing measurements. Raw US AQI remains separate.
    """
    if not rows:
        return
    try:
        db.session.execute(select(func.pg_advisory_xact_lock(LOCK_ID)))
        for offset in range(0, len(rows), 500):
            stmt = insert(Reading).values(rows[offset:offset+500])
            changes = {field: getattr(stmt.excluded, field) for field in POLLUTANTS}
            changes.update(source=stmt.excluded.source,
                           us_aqi=func.coalesce(stmt.excluded.us_aqi, Reading.us_aqi))
            db.session.execute(stmt.on_conflict_do_update(index_elements=['timestamp'], set_=changes))
        if snapshots:
            for offset in range(0, len(snapshots), 300):
                stmt = insert(SourceObservation).values(snapshots[offset:offset+300])
                db.session.execute(stmt.on_conflict_do_update(index_elements=['timestamp','source_id'],
                    set_={'original_values':stmt.excluded.original_values, 'provenance':stmt.excluded.provenance}))
        _recalculate()
        db.session.commit()
        db.session.expire_all()
    except Exception:
        db.session.rollback()
        raise


_refresh_lock = Lock()


class RefreshInProgress(RuntimeError):
    pass


def refresh():
    """HTTP and CLI share this nonblocking per-process refresh guard."""
    if not _refresh_lock.acquire(blocking=False):
        raise RefreshInProgress('Refresh already in progress')
    try:
        return _refresh()
    finally:
        _refresh_lock.release()


def _refresh():
    response = requests.get('https://air-quality-api.open-meteo.com/v1/air-quality', params={
        'latitude': LAT, 'longitude': LON, 'hourly': ','.join(FIELDS),
        'timezone': 'Asia/Yangon', 'past_days': 7, 'forecast_days': 1,
    }, timeout=(5, 30))
    response.raise_for_status()
    payload = response.json()
    hourly, units = payload['hourly'], payload['hourly_units']
    if payload.get('timezone') != 'Asia/Yangon':
        raise ValueError('Unexpected provider timezone')
    if abs(payload['latitude']-LAT) > 0.25 or abs(payload['longitude']-LON) > 0.25:
        raise ValueError('Provider returned an unexpected grid location')
    for field in POLLUTANTS:
        if units.get(field) not in ('μg/m³', 'µg/m³'):
            raise ValueError('Unexpected provider mass concentration units')
    if units.get('us_aqi') != 'USAQI':
        raise ValueError('Unexpected provider raw AQI standard')
    if any(len(hourly[field]) != len(hourly['time']) for field in FIELDS):
        raise ValueError('Inconsistent hourly array lengths')
    now = datetime.now(timezone.utc)
    rows, snapshots, seen = [], [], set()
    for i, stamp in enumerate(hourly['time']):
        at = datetime.fromisoformat(stamp)
        at = (at.replace(tzinfo=TZ) if at.tzinfo is None else at).astimezone(timezone.utc)
        if at.astimezone(TZ).minute or at.second or at.microsecond or at in seen:
            raise ValueError('Provider timestamps must be unique Yangon whole hours')
        seen.add(at)
        if at > now:
            continue
        values = {field: hourly[field][i] for field in FIELDS}
        if any(v is not None and (not isinstance(v, (int,float)) or not math.isfinite(v) or v < 0) for v in values.values()):
            raise ValueError('Invalid provider value')
        if all(v is None for v in values.values()):
            continue
        rows.append(dict(timestamp=at, **values, source='open-meteo-yangon'))
        snapshots.append(dict(timestamp=at, source_id='open-meteo-yangon', original_values={'time':stamp, **values},
            provenance={'city':CITY, 'requested_latitude':LAT, 'requested_longitude':LON,
                        'returned_latitude':payload['latitude'], 'returned_longitude':payload['longitude'],
                        'timezone':'Asia/Yangon', 'units':units, 'aqi_standard':'US EPA (raw only)'}))
    upsert(rows, snapshots)
    return len(rows)


def create_app(config=None):
    app = Flask(__name__)
    url = (config or {}).get('SQLALCHEMY_DATABASE_URI', os.getenv('DATABASE_URL',''))
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql+psycopg://', 1)
    elif url.startswith('postgresql://'):
        url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
    if not url.startswith('postgresql+psycopg://'):
        raise RuntimeError('DATABASE_URL must specify PostgreSQL; SQLite is not supported.')
    app.config.update(SQLALCHEMY_DATABASE_URI=url, SQLALCHEMY_ENGINE_OPTIONS={
        'pool_pre_ping':True, 'pool_recycle':300}, MAX_CONTENT_LENGTH=1024)
    if config:
        app.config.update({k:v for k,v in config.items() if k != 'SQLALCHEMY_DATABASE_URI'})
    db.init_app(app)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/')
    @app.get('/analytics')
    @app.get('/alerts')
    @app.get('/about')
    def page():
        prediction = os.getenv('PREDICTION_URL','')
        # A deployed URL alone does not establish the model's training target.
        if (urlparse(prediction).scheme != 'https' or not urlparse(prediction).netloc
                or os.getenv('PREDICTION_AQI_STANDARD') != VERSION):
            prediction = None
        return render_template('dashboard.html', prediction_url=prediction, calculation_version=VERSION)

    @app.get('/api/health')
    def health():
        try:
            db.session.execute(select(Reading.timestamp).limit(1))
            db.session.execute(select(CPCBResult.timestamp).limit(1))
            return jsonify(status='ok', database='postgresql', **metadata())
        except Exception:
            db.session.rollback()
            return jsonify(status='unavailable', error='Database or CPCB schema unavailable', **metadata()), 503

    @app.get('/api/latest')
    def latest():
        row = db.session.scalar(select(Reading).where(canonical_filter(), Reading.timestamp <= datetime.now(timezone.utc))
                                .order_by(Reading.timestamp.desc()).limit(1))
        if row is None:
            return jsonify(data=None, error='No canonical Mandalay readings imported or fetched yet', **metadata()), 404
        return jsonify(data=serialize(row), stale=datetime.now(timezone.utc)-row.timestamp > timedelta(hours=3), **metadata())

    def history_rows():
        try:
            days = int(request.args.get('days','7'))
            if not 1 <= days <= 366:
                raise ValueError()
        except ValueError:
            return None
        now = datetime.now(timezone.utc)
        return db.session.scalars(select(Reading).where(canonical_filter(), Reading.timestamp >= now-timedelta(days=days),
            Reading.timestamp <= now).order_by(Reading.timestamp)).all()

    @app.get('/api/history')
    def history():
        rows = history_rows()
        if rows is None:
            return jsonify(error='days must be an integer from 1 to 366'), 400
        return jsonify(data=[serialize(r, include_details=request.args.get("details") == "1") for r in rows], **metadata())

    @app.get('/api/analytics')
    def analytics():
        rows = history_rows()
        if rows is None:
            return jsonify(error='days must be an integer from 1 to 366'), 400
        data = [serialize(r, include_details=False) for r in rows]
        summary = {}
        for field in (*POLLUTANTS,'cpcb_aqi'):
            vals = [r[field] for r in data if r[field] is not None]
            summary[field] = dict(mean=round(sum(vals)/len(vals),2) if vals else None,
                min=min(vals) if vals else None, max=max(vals) if vals else None, count=len(vals))
        return jsonify(summary=summary, observations=len(rows),
                       unavailable_aqi_count=sum(r['cpcb_aqi'] is None for r in data), **metadata())

    @app.get('/api/alerts')
    def alerts():
        rows = history_rows()
        if rows is None:
            return jsonify(error='days must be an integer from 1 to 366'), 400
        data = [serialize(r, include_details=False) for r in reversed(rows)]
        alerts = [dict(r, alert_type='numeric-cpcb' if r['cpcb_aqi'] is not None else 'severe-band-warning',
                       message=f"Mandalay: {r['precautions']}") for r in data
                  if (r['cpcb_aqi'] is not None and r['cpcb_aqi'] >= 201) or r['category']=='Severe']
        return jsonify(data=alerts, threshold=201, observations=len(rows),
                       unavailable_aqi_count=sum(r['cpcb_aqi'] is None for r in data), **metadata())

    @app.post('/api/refresh')
    def refresh_route():
        secret = os.getenv('REFRESH_SECRET')
        if not secret:
            return jsonify(status='error', error='Refresh is not configured'), 503
        supplied = request.headers.get('X-Refresh-Secret', '')
        if not hmac.compare_digest(supplied.encode('utf-8'), secret.encode('utf-8')):
            return jsonify(status='error', error='Unauthorized'), 401
        try:
            return jsonify(status='success', imported=refresh(), **metadata())
        except RefreshInProgress:
            return jsonify(status='error', error='Refresh already in progress'), 409
        except (requests.RequestException, KeyError, ValueError, TypeError):
            db.session.rollback()
            return jsonify(status='error', error='Air quality provider unavailable or returned invalid Mandalay data'), 502
        except Exception:
            # Do not log exception text: provider/driver errors may contain secrets.
            db.session.rollback()
            return jsonify(status='error', error='Refresh failed; service temporarily unavailable'), 500

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return jsonify(error='Service temporarily unavailable'), 500

    @app.cli.command('init-db')
    def init_db():
        # Additive migration from the original app: readings remain unchanged.
        db.create_all()

    @app.cli.command('refresh-data')
    def refresh_data():
        print(f'Imported {refresh()} Mandalay hourly readings; CPCB results recalculated')

    @app.cli.command('recalculate-cpcb')
    def recalculate_cpcb():
        print(f'Recalculated {recalculate()} canonical Mandalay CPCB results')

    return app
