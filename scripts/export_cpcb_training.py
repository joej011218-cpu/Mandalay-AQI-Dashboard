"""Export only available, nonfuture, recalculated CPCB targets for model training."""
import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app import create_app, db, Reading, CPCBResult, canonical_filter, recalculate
from cpcb import POLLUTANTS, VERSION

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.with_suffix('.metadata.json').exists():
        parser.error('Refusing to overwrite an existing training export or metadata file')
    with create_app().app_context():
        recalculate()
        rows = db.session.scalars(select(Reading).join(CPCBResult).where(canonical_filter(),
            Reading.timestamp <= datetime.now(timezone.utc), CPCBResult.cpcb_aqi.is_not(None),
            CPCBResult.calculation_version == VERSION).order_by(Reading.timestamp)).all()
        with args.output.open('x',newline='') as f:
            writer = csv.DictWriter(f,fieldnames=['timestamp_utc',*POLLUTANTS,'target_cpcb_aqi'])
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(timestamp_utc=row.timestamp.astimezone(timezone.utc).isoformat(),
                    **{field:getattr(row,field) for field in POLLUTANTS}, target_cpcb_aqi=row.cpcb_result.cpcb_aqi))
        args.output.with_suffix('.metadata.json').write_text(json.dumps({
            'city':'Mandalay, Myanmar', 'latitude':21.9588, 'longitude':96.0891,
            'target':'target_cpcb_aqi', 'aqi_standard':'Indian CPCB AQI', 'calculation_version':VERSION,
            'rows':len(rows), 'pollutant_units':'ug/m3',
            'warning':'Same-hour rows are a target dataset, not a validated forecast design. Shift target for the forecast horizon and split chronologically; never train on raw US AQI or leak future windows.'},indent=2)+'\n')
        print(f'Exported {len(rows)} CPCB target rows and methodology metadata')
