from datetime import datetime, timedelta, timezone
import math
import pytest
from cpcb import calculate, category, color, mass_convert, subindex

AT = datetime(2026,1,2,0,30,tzinfo=timezone.utc)


def history(hours=24, **values):
    defaults = dict(pm2_5=45, pm10=75, nitrogen_dioxide=40,
                    sulphur_dioxide=40, ozone=50, carbon_monoxide=1000)
    defaults.update(values)
    return {AT-timedelta(hours=i):dict(defaults) for i in range(hours)}


@pytest.mark.parametrize('pollutant,value,index', [
    ('pm2_5',0,0),('pm2_5',30,50),('pm2_5',31,51),('pm2_5',45,75),
    ('pm2_5',60,100),('pm2_5',90,200),('pm2_5',120,300),('pm2_5',250,400),
    ('pm10',50,50),('pm10',100,100),('pm10',175,150),('pm10',250,200),
    ('pm10',350,300),('pm10',430,400),
    ('nitrogen_dioxide',40,50),('nitrogen_dioxide',80,100),('nitrogen_dioxide',130,150),
    ('nitrogen_dioxide',180,200),('nitrogen_dioxide',280,300),('nitrogen_dioxide',400,400),
    ('sulphur_dioxide',40,50),('sulphur_dioxide',80,100),('sulphur_dioxide',380,200),
    ('sulphur_dioxide',800,300),('sulphur_dioxide',1600,400),
    ('ozone',50,50),('ozone',100,100),('ozone',168,200),('ozone',208,300),('ozone',748,400),
    ('carbon_monoxide',1,50),('carbon_monoxide',1.1,51),('carbon_monoxide',1.5,73),
    ('carbon_monoxide',2,100),('carbon_monoxide',10,200),('carbon_monoxide',17,300),('carbon_monoxide',34,400),
])
def test_published_breakpoints_and_interpolation(pollutant,value,index):
    assert subindex(pollutant,value)==index


def test_explicit_reporting_precision_and_invalid_values():
    assert subindex('pm2_5',30.49)==50
    assert subindex('pm2_5',30.5)==51
    assert subindex('carbon_monoxide',1.04)==50
    assert subindex('carbon_monoxide',1.05)==51
    for value in (None,math.nan,math.inf,-1):
        assert subindex('pm2_5',value) is None
    assert subindex('pm2_5',251) is None  # No invented Severe upper endpoint.


def test_mass_conversion():
    assert mass_convert(1000,'μg/m³','mg/m³')==1
    assert mass_convert(1.7,'mg/m3','ug/m3')==1700
    assert mass_convert(45,'µg/m³')==45
    assert mass_convert(None,'ug/m3') is None
    for unit in ('ppm','ppb','unknown'):
        with pytest.raises(ValueError):
            mass_convert(1,unit)
    for value in (-1,math.nan,math.inf):
        with pytest.raises(ValueError):
            mass_convert(value,'ug/m3')


def test_24_hour_and_8_hour_means_co_units_and_future_exclusion():
    h=history(pm2_5=30,carbon_monoxide=10000,ozone=10)
    for i in range(8):
        h[AT-timedelta(hours=i)].update(pm2_5=90,carbon_monoxide=1000,ozone=100)
    h[AT+timedelta(hours=1)]=dict(pm2_5=10000,carbon_monoxide=100000)
    result=calculate(AT,h)
    details=result['pollutant_details']
    assert details['pm2_5']['average']==50
    assert details['carbon_monoxide']['average']==1
    assert details['carbon_monoxide']['unit']=='mg/m³'
    assert details['ozone']['average']==100
    assert result['cpcb_aqi']==100
    assert result['dominant_pollutant']=='ozone'


def test_completeness_boundary_16_hours_and_warmup():
    assert calculate(AT,history(15))['status']=='unavailable'
    assert calculate(AT,history(16))['status']=='available'
    assert calculate(AT,history(16))['pollutant_details']['pm2_5']['valid_hours_24']==16
    h=history()
    for i in range(9):
        h[AT-timedelta(hours=i)]['pm2_5']=None
    assert not calculate(AT,h)['pollutant_details']['pm2_5']['eligible']


def test_missing_recent_eight_hours_cannot_borrow_day_values():
    h=history()
    h[AT-timedelta(hours=3)]['ozone']=None
    h[AT-timedelta(hours=3)]['carbon_monoxide']=None
    details=calculate(AT,h)['pollutant_details']
    assert details['ozone']['valid_hours_24']==23
    assert details['ozone']['valid_hours_window']==7
    assert not details['ozone']['eligible'] and not details['carbon_monoxide']['eligible']
    # Even a complete eight-hour window needs at least 16/24 total valid hours.
    assert not calculate(AT,history(8))['pollutant_details']['ozone']['eligible']


def test_minimum_pollutants_and_particulate_requirement():
    two=history(nitrogen_dioxide=None,sulphur_dioxide=None,ozone=None,carbon_monoxide=None)
    assert calculate(AT,two)['cpcb_aqi'] is None
    gases=history(pm2_5=None,pm10=None)
    result=calculate(AT,gases)
    assert len(result['eligible_pollutants'])==4 and result['cpcb_aqi'] is None
    assert any('particulate' in reason for reason in result['reasons'])
    three=history(sulphur_dioxide=None,ozone=None,carbon_monoxide=None)
    assert calculate(AT,three)['cpcb_aqi']==75
    assert calculate(AT,three)['dominant_pollutants']==['pm2_5','pm10']


def test_irregular_invalid_and_unrelated_aqi_values_are_ignored():
    h=history(15)
    h[AT-timedelta(hours=16,minutes=15)]=dict(history()[AT])
    assert calculate(AT,h)['status']=='unavailable'
    h=history()
    for row in h.values():
        row.update(us_aqi=500,original_aqi=500,cpcb_aqi=500)
    assert calculate(AT,h)['cpcb_aqi']==75
    for row in h.values():
        row.update(pm2_5=math.nan,pm10=-1)
    assert calculate(AT,h)['status']=='unavailable'


def test_severe_band_is_not_fabricated_and_still_requires_minimum_pollutants():
    r=calculate(AT,history(pm2_5=500))
    assert r['cpcb_aqi'] is None and r['status']=='unavailable'
    assert r['category']=='Severe' and r['severe_pollutants']==['pm2_5']
    assert r['pollutant_details']['pm2_5']['index_range']==[401,500]
    r=calculate(AT,history(pm2_5=500,pm10=None,nitrogen_dioxide=None,
                           sulphur_dioxide=None,ozone=None,carbon_monoxide=None))
    assert r['category']=='Unavailable'  # No claimed overall band with one pollutant.


def test_categories_and_reference_colors():
    assert [category(v) for v in (None,0,50,51,100,101,200,201,300,301,400,401,500)] == [
        'Unavailable','Good','Good','Satisfactory','Satisfactory','Moderate','Moderate',
        'Poor','Poor','Very Poor','Very Poor','Severe','Severe']
    assert [color(v) for v in ('Good','Satisfactory','Moderate','Poor','Very Poor','Severe')] == [
        '#4E9F3D','#A8C43A','#E8C547','#E8823C','#D8453B','#7A2A2A']
