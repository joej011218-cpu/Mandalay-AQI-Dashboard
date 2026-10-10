"""Monitoring-only CPCB calculation. Does not change the prediction calculator."""
from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
import math
from cpcb import subindex, category, color, PRECAUTIONS, quantize

VERSION = 'mandalay-combined-cpcb-v1'
OPEN_METEO = ('pm2_5','pm10','nitrogen_dioxide','sulphur_dioxide','ozone')
POLLUTANTS = (*OPEN_METEO,'carbon_monoxide','ammonia')
UNITS = {p:('mg/m³' if p=='carbon_monoxide' else 'µg/m³') for p in POLLUTANTS}


def ammonia_subindex(value):
    if value is None or not math.isfinite(value) or value<0:
        return None
    value=Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    bands=((0,200,0,50),(201,400,51,100),(401,800,101,200),
           (801,1200,201,300),(1201,1800,301,400))
    for low,high,ilow,ihigh in bands:
        if low<=value<=high:
            result=Decimal(ihigh-ilow)*(value-low)/Decimal(high-low)+ilow
            return int(result.quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    return None  # CPCB Severe concentration band is open-ended.


def calculate(at, history, excluded=None):
    """Each history slot is a covered hourly mean, CO already in mg/m³.

    Never convert Open-Meteo CO into a hardware measurement. Caller provides
    only source-isolated, sufficiently covered hardware bins.
    """
    excluded=excluded or {}
    details={};indices={};severe=[]
    for p in POLLUTANTS:
        values=[history.get(at-timedelta(hours=i),{}).get(p) for i in range(24)]
        valid=lambda v:v is not None and math.isfinite(v) and v>=0
        day=[v for v in values if valid(v)]
        period=8 if p in ('carbon_monoxide','ozone') else 24
        window=[v for v in values[:period] if valid(v)]
        d=dict(source='ESP32' if p not in OPEN_METEO else 'Open-Meteo', unit=UNITS[p],
               averaging_hours=period, valid_hours_24=len(day),valid_hours_window=len(window),
               average=None,reported_concentration=None,subindex=None,eligible=False,
               reason=excluded.get(p) or 'Requires at least 16 covered hours in the preceding 24 hours')
        if p not in excluded and len(day)>=16 and (period==24 or len(window)==8):
            mean=sum(window)/len(window)
            idx=ammonia_subindex(mean) if p=='ammonia' else subindex(p,mean)
            d.update(average=mean,reported_concentration=float(quantize(mean,p)),subindex=idx,eligible=True,reason=None)
            if idx is None:
                severe.append(p);d.update(index_range=[401,500],reason='Open-ended Severe band; numeric interpolation unavailable')
            else:indices[p]=idx
        elif p not in excluded and len(day)>=16 and period==8:
            d['reason']='Requires all 8 recent covered hours for CO/O3'
        details[p]=d
    available=[p for p,d in details.items() if d['eligible']]
    reasons=[]
    if len(available)<3:reasons.append('At least three eligible pollutants are required')
    if not any(p in available for p in ('pm2_5','pm10')):reasons.append('An eligible PM2.5 or PM10 history is required')
    value=None;label='Unavailable';dominants=[]
    if not reasons:
        if severe:
            label='Severe';dominants=severe;reasons.append('Severe concentration band has no finite numeric upper endpoint')
        else:
            value=max(indices.values());label=category(value);dominants=[p for p,v in indices.items() if v==value]
    missing=[p for p in POLLUTANTS if p not in available]
    partial=bool(missing)
    return dict(cpcb_aqi=value,category=label,color=color(label),dominant_pollutant=dominants[0] if dominants else None,
                dominant_pollutants=dominants,eligible_pollutants=available,pollutant_details=details,
                status='available' if value is not None else 'unavailable',coverage='partial' if partial else 'complete',
                provisional=True,aqi_label='Partial CPCB estimate' if partial else 'Provisional combined CPCB estimate',
                reasons=reasons,missing_pollutants=missing,severe_pollutants=severe,
                precautions=PRECAUTIONS[label],calculation_version=VERSION)
