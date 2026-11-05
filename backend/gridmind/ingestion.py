from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
from sqlalchemy import select
from .models import DataSource, Region, Observation, AuditLog
from .public_data import fetch_karnataka, parse_csv


def parse_time(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return result.astimezone(timezone.utc).replace(tzinfo=None) if result.tzinfo else result


def import_records(factory, records, metadata, source_name, region_name, timezone_name='Asia/Kolkata'):
    if not records:
        raise ValueError('No valid observations were found')
    metadata = {**metadata,**metadata.get('metadata',{})}
    inserted, duplicates = 0, 0
    with factory.begin() as session:
        source = session.scalar(select(DataSource).where(DataSource.name == source_name).with_for_update())
        if not source:
            source = DataSource(name=source_name,provider=metadata.get('provider','CSV import'),
                                units=metadata.get('units',records[0].get('unit','kWh')),
                                frequency=metadata.get('frequency','as supplied'),source_type=metadata.get('source_type','historical_csv'),url=metadata.get('url',''),
                                metadata_json=metadata)
            session.add(source)
            session.flush()
        source.status, source.metadata_json = 'available', metadata
        for field in ('source_type','provider','url'):
            if field in metadata:
                setattr(source,field,metadata[field])
        region = session.scalar(select(Region).where(Region.name == region_name))
        if not region:
            region = Region(name=region_name,timezone=timezone_name)
            session.add(region)
            session.flush()
        existing = {(o.region_id,o.series_key,o.metric,o.observed_at):(o.value,o.unit) for o in session.scalars(select(Observation).where(Observation.source_id == source.id))}
        regions = {region.name:region}
        for record in records:
            number = float(record['value'])
            if not math.isfinite(number) or number < 0:
                raise ValueError('Non-finite or negative energy reading')
            when = parse_time(record['observed_at'])
            metric = record.get('metric','supplied_energy_gwh')
            record_region_name = record.get('region',region_name)
            if record_region_name not in regions:
                target_region = session.scalar(select(Region).where(Region.name == record_region_name))
                if not target_region:
                    target_region = Region(name=record_region_name,timezone='Europe/London' if record_region_name.startswith('London/') else timezone_name)
                    session.add(target_region)
                    session.flush()
                regions[record_region_name] = target_region
            target_region = regions[record_region_name]
            series = str(record.get('series_key',record.get('household_id',record_region_name.split('/',1)[1] if record_region_name.startswith('London/') else 'regional')))
            key = (target_region.id,series,metric,when)
            if key in existing:
                if existing[key] != (number,record.get('unit','GWh')):
                    raise ValueError('Conflicting duplicate observation: an existing timestamp has a different value or unit; import rolled back')
                duplicates += 1
                continue
            session.add(Observation(source_id=source.id,region_id=target_region.id,observed_at=when,
                                    series_key=series,metric=metric,value=number,unit=record.get('unit','GWh'),
                                    quality_status=record.get('quality_status',record.get('quality','valid')),
                                    metadata_json={**record.get('metadata_json',record.get('metadata',{})),
                                                   'raw_sha256':metadata.get('raw_sha256',metadata.get('sha256')),
                                                   'raw_file':metadata.get('raw_file',metadata.get('raw_path')),
                                                   'source_url':metadata.get('url',''),
                                                   **{key:metadata[key] for key in ('raw_json','raw_json_sha256','acquired_at') if key in metadata}}))
            existing[key] = (number,record.get('unit','GWh'))
            inserted += 1
        session.add(AuditLog(action='data_import',entity_id=source.id,detail={'inserted':inserted,'duplicates':duplicates,'source':source_name,'raw_sha256':metadata.get('sha256',metadata.get('raw_sha256'))}))
        source_id = source.id
    return {'source_id':source_id,'inserted':inserted,'duplicates':duplicates,'quality':metadata,'mode':'observed historical data'}


def ingest_karnataka(factory, raw_directory):
    try:
        records, metadata = fetch_karnataka(Path(raw_directory))
        return import_records(factory,records,metadata,'Karnataka daily supplied energy','Karnataka')
    except Exception:
        with factory.begin() as session:
            source = session.scalar(select(DataSource).where(DataSource.name == 'Karnataka daily supplied energy'))
            if source:
                source.status = 'unavailable'
                source.metadata_json = {**source.metadata_json,'availability_note':'Historical observations retained; latest import failed.'}
        raise


def ingest_csv(factory, content, raw_directory, source_name, region_name, timezone_name, *, provenance=None):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError,ValueError):
        raise ValueError('Unknown time zone; use an IANA name such as Asia/Kolkata, Europe/London, or UTC')
    digest = hashlib.sha256(content).hexdigest()
    path = Path(raw_directory) / f'upload-{digest}.csv'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(content)
    records, report = parse_csv(content,region=region_name,timezone_name=timezone_name)
    metadata = {**report,'raw_sha256':digest,'raw_file':str(path),'provider':'User supplied CSV',
                'units':records[0]['unit'] if records else '', 'acquired_at':datetime.now(timezone.utc).isoformat(),
                **(provenance or {})}
    return import_records(factory,records,metadata,source_name,region_name,timezone_name)
