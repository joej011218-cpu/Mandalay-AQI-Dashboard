"""Persistent ESP32 alert episodes; invalid estimates are quality evidence only."""
from datetime import datetime, timedelta, timezone
import hashlib
from sqlalchemy import Index, select, func, text
from app import db, TZ
import combined_cpcb as combined


class HardwareAlert(db.Model):
    __tablename__ = 'mandalay_hardware_alert_events'
    id = db.Column(db.BigInteger, primary_key=True)
    device_id = db.Column(db.String(80), nullable=False)
    condition = db.Column(db.String(100), nullable=False)
    pollutant = db.Column(db.String(40), nullable=False)
    alert_type = db.Column(db.String(40), nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False)
    evidence_at = db.Column(db.DateTime(timezone=True))
    last_seen = db.Column(db.DateTime(timezone=True), nullable=False)
    resolved_at = db.Column(db.DateTime(timezone=True))
    status = db.Column(db.String(20), nullable=False)
    value = db.Column(db.Float)
    threshold = db.Column(db.Float)
    unit = db.Column(db.String(20), nullable=False)
    averaging_hours = db.Column(db.Integer)
    severity = db.Column(db.String(30), nullable=False)
    message = db.Column(db.Text, nullable=False)
    __table_args__ = (
        Index('uq_mandalay_hardware_active_condition', 'device_id', 'condition', unique=True,
              postgresql_where=text("status = 'active'")),
        Index('ix_mandalay_hardware_alert_time', 'device_id', 'timestamp'),
    )


def lock_device(device):
    digest = hashlib.sha256(('mandalay-alerts:' + device).encode()).digest()
    db.session.execute(select(func.pg_advisory_xact_lock(int.from_bytes(digest[:8], 'big', signed=True))))


def reconcile(now=None):
    """Caller owns transaction. Same per-device lock is used by HTTP/MQTT ingestion.

    Offline transitions are evaluated on dashboard polls as well as ingestion;
    no background subscriber, scheduler or invented measurement is required.
    """
    from hardware import settings, hardware_status, hourly_hardware, _hour, thresholds
    now = now or datetime.now(timezone.utc)
    cfg = settings(); device = cfg['primary']
    if not device:
        return
    lock_device(device)
    # A request may wait behind a later dashboard poll. Evaluate the newest
    # stored observation without moving the episode clock backwards or skipping
    # an accepted reading merely because that poll obtained the lock first.
    newest = db.session.scalar(select(func.max(func.greatest(HardwareAlert.last_seen, HardwareAlert.resolved_at))).where(HardwareAlert.device_id == device))
    if newest:now = max(now, newest)
    state = hardware_status(now); raw = state['last_observation']
    conditions = {}
    def add(kind, pollutant, value, threshold, unit, hours, severity, message, stamp=None):
        conditions[kind + ':' + pollutant] = dict(alert_type=kind, pollutant=pollutant, value=value,
            threshold=threshold, unit=unit, averaging_hours=hours, severity=severity, message=message,
            timestamp=stamp or now)
    if state['status'] != 'online':
        add('sensor-status', 'CO / NH3', None, cfg['stale'], 'seconds', None, state['status'],
            'ESP32 is offline/stale; current hardware concentrations are unavailable. No modeled replacement is used.')
    if raw and raw['validity'].get('co_range_valid') is False:
        add('sensor-quality', 'carbon_monoxide', raw['co_mg_m3'], None, 'mg/m³', None, 'Quality warning',
            'Outside calibrated range — not AQI eligible. MQ-7 estimate is diagnostic, not a validated pollution concentration.',
            datetime.fromisoformat(raw['timestamp']))
    if state['status'] == 'online' and raw:
        for pollutant, key, flag in (('carbon_monoxide','co_mg_m3','co_valid'), ('ammonia','nh3_ug_m3','nh3_valid')):
            if raw[key] is not None and not raw[flag] and not (pollutant == 'carbon_monoxide' and raw['validity'].get('co_range_valid') is False):
                add('sensor-quality', pollutant, raw[key], None, combined.UNITS[pollutant], None, 'Quality warning',
                    'Invalid sensor estimate — not AQI eligible.', datetime.fromisoformat(raw['timestamp']))
        limits = thresholds('raw')
        end = _hour(now)
        history = hourly_hardware(end - timedelta(hours=24), end)
        details = combined.calculate(end, history)['pollutant_details']
        rolling = thresholds('rolling')
        for pollutant, key, flag in (('carbon_monoxide','co_mg_m3','co_valid'), ('ammonia','nh3_ug_m3','nh3_valid')):
            if not raw[flag]:
                continue
            if pollutant in limits and raw[key] > limits[pollutant]:
                add('raw-concentration', pollutant, raw[key], limits[pollutant], combined.UNITS[pollutant], 0, 'Warning',
                    'Configured raw sensor warning; not a CPCB rolling exceedance. Reduce exposure and check sensor validity.',
                    datetime.fromisoformat(raw['timestamp']))
            d = details[pollutant]
            if d['eligible'] and d['average'] is not None and d['average'] > rolling[pollutant]:
                severity = combined.category(d['subindex']) if d['subindex'] is not None else 'Severe'
                add('rolling-concentration', pollutant, d['average'], rolling[pollutant], d['unit'], d['averaging_hours'], severity,
                    'Covered ESP32 rolling concentration exceeds the configured warning threshold. ' + combined.PRECAUTIONS[severity], end)
    active = {row.condition: row for row in db.session.scalars(select(HardwareAlert).where(
        HardwareAlert.device_id == device, HardwareAlert.status == 'active'))}
    for condition, event in conditions.items():
        existing = active.pop(condition, None)
        if existing:
            # Keep the start timestamp; update latest evidence, not a new alert per sample.
            existing.last_seen = now
            existing.evidence_at = event['timestamp']
            for key, value in event.items():
                if key != 'timestamp': setattr(existing, key, value)
        else:
            db.session.add(HardwareAlert(device_id=device, condition=condition, evidence_at=event['timestamp'], last_seen=now, status='active', **event))
    for row in active.values():
        row.status = 'resolved'; row.resolved_at = now
    db.session.flush()


def events(days, now=None):
    from hardware import settings
    now = now or datetime.now(timezone.utc)
    reconcile(now)
    db.session.commit()
    rows = db.session.scalars(select(HardwareAlert).where(HardwareAlert.device_id == settings()['primary'],
        (HardwareAlert.last_seen >= now - timedelta(days=days)) | (HardwareAlert.status == 'active'))
        .order_by(HardwareAlert.timestamp.desc())).all()
    return [dict(id='hardware-' + str(r.id), device_id=r.device_id, timestamp=r.timestamp.astimezone(TZ).isoformat(),
        measurement_timestamp=(r.evidence_at or r.timestamp).astimezone(TZ).isoformat(),
        last_seen=r.last_seen.astimezone(TZ).isoformat(), resolved_at=r.resolved_at.astimezone(TZ).isoformat() if r.resolved_at else None,
        status=r.status, pollutant=r.pollutant, alert_type=r.alert_type, value=r.value, threshold=r.threshold, unit=r.unit,
        averaging_hours=r.averaging_hours, severity=r.severity, source='ESP32',
        color=combined.color(r.severity) if r.alert_type == 'rolling-concentration' else '#E8C547',
        message=r.message, provisional=True) for r in rows]
