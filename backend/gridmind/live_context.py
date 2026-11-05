"""Explicitly configured measured-energy feed; no fabricated live fallback."""
import hashlib
import io
import csv
import json
import time
from urllib.parse import urlsplit, urlunsplit
import httpx
from sqlalchemy import select
from .models import DataSource
from .ingestion import ingest_csv

MAX_FEED_BYTES = 10 * 1024 * 1024
MAX_FEED_ROWS = 20000


def _download(url, headers):
    """Bound decoded response bytes and total download time, including trickles."""
    started = time.monotonic()
    with httpx.stream('GET',url,headers=headers,timeout=httpx.Timeout(20,connect=10,read=5),follow_redirects=False) as response:
        response.raise_for_status()
        declared_size = response.headers.get('Content-Length')
        if declared_size and int(declared_size) > MAX_FEED_BYTES:
            raise ValueError('Live feed exceeds the bounded import size')
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise ValueError('Live feed exceeds the bounded import size')
            if time.monotonic() - started > 20:
                raise ValueError('Live feed exceeded the total download time limit')
            chunks.append(chunk)
        return b''.join(chunks)


def _public_source_url(url):
    # Credentials in userinfo or query strings must not enter provenance/API
    # responses; the exact original payload is retained separately by checksum.
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme,parsed.netloc.rsplit('@',1)[-1],parsed.path,'',''))


def refresh(factory, settings):
    if not settings.live_context_url:
        raise ValueError('Set LIVE_CONTEXT_URL to a measured-energy JSON feed. No live provider is configured; historical replay remains available.')
    try:
        headers = {'Authorization':f'Bearer {settings.live_context_token}'} if settings.live_context_token else {}
        content = _download(settings.live_context_url,headers)
        payload = json.loads(content)
        records = payload['observations'] if isinstance(payload,dict) else payload
        if not isinstance(records,list) or not records:
            raise ValueError('Live feed must contain a nonempty observations array')
        if len(records) > MAX_FEED_ROWS:
            raise ValueError('Live feed exceeds the 20000 observation limit; no partial refresh was imported')
        output = io.StringIO()
        writer = csv.DictWriter(output,fieldnames=['timestamp','value','metric','unit','region','series_key'])
        writer.writeheader()
        for row in records:
            if not isinstance(row,dict):
                raise ValueError('Each live observation must be a JSON object')
            if row.get('value_kind','observed') != 'observed':
                raise ValueError('Live energy feed must contain observed measurements, not predictions')
            writer.writerow({'timestamp':row.get('timestamp',row.get('observed_at')),'value':row['value'],
                             'metric':row['metric'],'unit':row['unit'],'region':row.get('region',settings.live_context_region),
                             'series_key':row.get('series_key',row.get('household_id','regional'))})
        raw_directory = settings.path(settings.raw_directory)
        raw_directory.mkdir(parents=True,exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        raw_path = raw_directory/('live-'+digest+'.json')
        raw_path.write_bytes(content)
        result = ingest_csv(factory,output.getvalue().encode(),settings.path(settings.raw_directory),
                            settings.live_context_name,settings.live_context_region,settings.live_context_timezone,
                            provenance={'source_type':'live_api','provider':settings.live_context_name,
                                        'url':_public_source_url(settings.live_context_url),
                                        'raw_json':str(raw_path),'raw_json_sha256':digest,
                                        'value_kind':'observed','live_feed_configured':True})
        result['mode'] = 'observed live-context data'
        return result
    except (httpx.HTTPError,ValueError,KeyError,TypeError,OSError) as exc:
        with factory.begin() as session:
            source = session.scalar(select(DataSource).where(DataSource.name==settings.live_context_name))
            if source:
                source.status = 'unavailable'
                source.metadata_json = {**source.metadata_json,'availability_note':'Historical observations retained; live refresh failed.'}
        raise ValueError(f'Live source unavailable ({type(exc).__name__}); no measurements were invented. Historical replay is available.') from exc
