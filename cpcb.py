"""Indian CPCB AQI from validated hourly mass concentrations.

Table: CPCB National Air Quality Index (2014), concentration breakpoint table.
See docs/CPCB_METHOD.md for the rolling convention and conservative safeguards.
No US AQI inputs are accepted by this module.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import math

VERSION = 'cpcb-2014-rolling-v1'
POLLUTANTS = ('pm2_5', 'pm10', 'carbon_monoxide', 'nitrogen_dioxide', 'sulphur_dioxide', 'ozone')
# Finite bands only: CPCB publishes open-ended concentration bounds for Severe.
# Do not invent a concentration corresponding to AQI 500.
BREAKPOINTS = {
    'pm2_5': ((0,30),(31,60),(61,90),(91,120),(121,250)),
    'pm10': ((0,50),(51,100),(101,250),(251,350),(351,430)),
    'nitrogen_dioxide': ((0,40),(41,80),(81,180),(181,280),(281,400)),
    'sulphur_dioxide': ((0,40),(41,80),(81,380),(381,800),(801,1600)),
    'ozone': ((0,50),(51,100),(101,168),(169,208),(209,748)),
    'carbon_monoxide': ((0,1.0),(1.1,2.0),(2.1,10.0),(10.1,17.0),(17.1,34.0)),
}
INDEX_BANDS = ((0,50),(51,100),(101,200),(201,300),(301,400))
CATEGORIES = (
    (50,'Good','#4E9F3D'), (100,'Satisfactory','#A8C43A'),
    (200,'Moderate','#E8C547'), (300,'Poor','#E8823C'),
    (400,'Very Poor','#D8453B'), (500,'Severe','#7A2A2A'),
)
PRECAUTIONS = {
    'Good': 'Normal outdoor activities can continue.',
    'Satisfactory': 'Sensitive people should monitor for breathing discomfort and reduce prolonged exertion if symptoms occur.',
    'Moderate': 'People with lung, asthma or heart conditions should reduce prolonged or strenuous outdoor activity.',
    'Poor': 'Reduce prolonged outdoor exertion. Sensitive groups should minimize outdoor physical activity.',
    'Very Poor': 'Avoid strenuous outdoor activity. Sensitive groups should remain indoors as much as practical.',
    'Severe': 'Avoid outdoor physical activity and minimize exposure. Follow local health advice, especially with existing respiratory or heart conditions.',
    'Unavailable': 'Insufficient data to assess air quality. Missing AQI does not establish safe conditions.',
}


def category(value):
    if value is None:
        return 'Unavailable'
    for limit, name, _ in CATEGORIES:
        if value <= limit:
            return name
    return 'Severe'


def color(name):
    return next((code for _, label, code in CATEGORIES if label == name), '#8A97A0')


def mass_convert(value, source_unit, target_unit='ug/m3'):
    """No guessed ppm/ppb conversion without temperature and pressure."""
    if value is None:
        return None
    aliases = {'µg/m³':'ug/m3', 'μg/m³':'ug/m3', 'ug/m³':'ug/m3',
               'µg/m3':'ug/m3', 'μg/m3':'ug/m3', 'mg/m³':'mg/m3'}
    source = aliases.get(source_unit, source_unit)
    target = aliases.get(target_unit, target_unit)
    scales = {'ug/m3':1.0, 'mg/m3':1000.0}
    if source not in scales or target not in scales:
        raise ValueError('Only verified mass units ug/m3 and mg/m3 are supported; ppm/ppb require environmental conversion metadata')
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('Concentrations must be finite and nonnegative')
    result = value * scales[source] / scales[target]
    if not math.isfinite(result):
        raise ValueError('Converted concentration is not finite')
    return result


def quantize(value, pollutant):
    precision = Decimal('0.1') if pollutant == 'carbon_monoxide' else Decimal('1')
    return Decimal(str(value)).quantize(precision, rounding=ROUND_HALF_UP)


def subindex(pollutant, concentration):
    """Input: rolling concentration in µg/m³, except CO in mg/m³.

    Concentrations are rounded to the table's reporting resolution before
    interpolation; resulting indices use decimal half-up rounding.
    Returns None for missing values or the open-ended Severe concentration band.
    """
    if pollutant not in BREAKPOINTS:
        raise ValueError('Unsupported CPCB pollutant')
    if concentration is None:
        return None
    value = float(concentration)
    if not math.isfinite(value) or value < 0:
        return None
    value = quantize(value, pollutant)
    for (low, high), (ilow, ihigh) in zip(BREAKPOINTS[pollutant], INDEX_BANDS):
        low, high = Decimal(str(low)), Decimal(str(high))
        if low <= value <= high:
            index = (Decimal(ihigh-ilow) * (value-low) / (high-low)) + Decimal(ilow)
            return int(index.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return None


def calculate(at, history):
    """history maps aware hourly timestamps to pollutant dictionaries.

    Exact one-hour slots prevent irregular/duplicate samples from satisfying
    completeness. Only (at - 24h, at] is inspected; no future data is used.
    Require >=16/24 valid hours for each pollutant, plus all 8/8 recent hours
    for CO/O3. The latter is intentionally conservative, not an asserted CPCB
    six-of-eight rule. Overall eligibility requires >=3 pollutants and PM.
    """
    details, eligible, severe = {}, {}, []
    for pollutant in POLLUTANTS:
        slots = [history.get(at - timedelta(hours=i), {}).get(pollutant) for i in range(24)]
        valid = lambda v: v is not None and math.isfinite(v) and v >= 0
        day = [float(v) for v in slots if valid(v)]
        period = 8 if pollutant in ('ozone', 'carbon_monoxide') else 24
        recent = [float(v) for v in slots[:period] if valid(v)]
        unit = 'mg/m³' if pollutant == 'carbon_monoxide' else 'µg/m³'
        detail = dict(averaging_hours=period, valid_hours_24=len(day),
                      valid_hours_window=len(recent), unit=unit, average=None,
                      reported_concentration=None, subindex=None, eligible=False,
                      reason='Requires at least 16 valid hourly values in the last 24 hours')
        if len(day) >= 16 and (period == 24 or len(recent) == 8):
            average = sum(recent) / len(recent)
            if pollutant == 'carbon_monoxide':
                average = mass_convert(average, 'ug/m3', 'mg/m3')
            detail.update(average=average, reported_concentration=float(quantize(average, pollutant)), eligible=True, reason=None)
            index = subindex(pollutant, average)
            detail['subindex'] = index
            if index is None:
                severe.append(pollutant)
                detail['reason'] = 'Severe concentration band is open-ended; numeric sub-index cannot be interpolated from the published finite bounds'
                detail['index_range'] = [401, 500]
            else:
                eligible[pollutant] = index
        elif len(day) >= 16 and period == 8:
            detail['reason'] = 'Conservative safeguard requires all 8 recent hourly values for CO/O3'
        details[pollutant] = detail
    available = list(eligible) + severe
    missing = []
    if len(available) < 3:
        missing.append('At least three eligible pollutants are required')
    if not any(p in available for p in ('pm2_5', 'pm10')):
        missing.append('At least one eligible particulate pollutant (PM2.5 or PM10) is required')
    overall, dominant, label = None, None, 'Unavailable'
    if not missing and severe:
        # Band is identifiable, but a precise number/dominance is not.
        label = 'Severe'
        missing.append('An eligible pollutant is in an open-ended Severe band; numeric AQI is unavailable rather than extrapolated')
    elif not missing:
        overall = max(eligible.values())
        dominant = next(p for p in POLLUTANTS if eligible.get(p) == overall)
        label = category(overall)
    return dict(cpcb_aqi=overall, category=label, color=color(label),
                dominant_pollutant=dominant, dominant_pollutants=[p for p,v in eligible.items() if v == overall] if overall is not None else severe,
                status='available' if overall is not None else 'unavailable',
                reasons=missing, eligible_pollutants=available, pollutant_details=details,
                severe_pollutants=severe, precautions=PRECAUTIONS[label], calculation_version=VERSION)
