"""Operational event ownership; local journal now, DynamoDB when configured."""
import hashlib
import json
import os
import threading
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4


def event_record(run_id, step, event_type, event_id, timestamp, payload):
    UUID(run_id)  # Also prevents paths escaping the journal directory.
    if event_type not in {'STATE','AGENT_DECISION','SAFETY_CHECK'} or step < 0:
        raise ValueError('Invalid operational event category or step')
    suffix = hashlib.sha256(str(event_id).encode()).hexdigest()[:20]
    return {'PK': f'RUN#{run_id}', 'SK': f'STEP#{step:08d}#{event_type}#{suffix}',
            'run_id': run_id, 'step': step, 'event_type': event_type,
            'schema_version': 1, 'timestamp': timestamp, 'payload': payload}


class LocalEventStore:
    backend = 'local journal (AWS deferred)'

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def put(self, record):
        run_id = str(UUID(record['run_id']))
        folder = self.directory / run_id
        filename = hashlib.sha256(record['SK'].encode()).hexdigest() + '.json'
        content = json.dumps(record, sort_keys=True, allow_nan=False)
        with self.lock:
            folder.mkdir(exist_ok=True)
            path = folder / filename
            if path.exists():
                if json.loads(path.read_text(encoding='utf-8')) != record:
                    raise ValueError('Idempotency conflict: event key has different content')
                return
            temporary = folder / (filename + '.' + uuid4().hex + '.tmp')
            try:
                with temporary.open('w', encoding='utf-8') as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def query(self, run_id, *, step=None, event_type=None, limit=1000, after=None):
        folder = self.directory / str(UUID(run_id))
        records = [json.loads(path.read_text(encoding='utf-8')) for path in folder.glob('*.json')]
        return sorted((r for r in records if (step is None or r['step'] == step)
                       and (not event_type or r['event_type'] == event_type)
                       and (not after or r['SK'] > after)), key=lambda r:r['SK'])[:limit]

    def ready(self):
        return self.directory.is_dir() and os.access(self.directory, os.W_OK)


class DynamoEventStore:
    backend = 'dynamodb'

    def __init__(self, settings):
        import boto3
        self.table = boto3.resource('dynamodb', region_name=settings.aws_region,
                                   endpoint_url=settings.dynamodb_endpoint_url).Table(settings.dynamodb_table)

    def put(self, record):
        from botocore.exceptions import ClientError, EndpointConnectionError, ConnectionClosedError
        item = json.loads(json.dumps(record, allow_nan=False), parse_float=Decimal)
        for attempt in range(4):
            try:
                self.table.put_item(Item=item, ConditionExpression='attribute_not_exists(PK) AND attribute_not_exists(SK)')
                return
            except ClientError as exc:
                code = exc.response['Error']['Code']
                if code == 'ConditionalCheckFailedException':
                    current = self.table.get_item(Key={'PK': item['PK'],'SK': item['SK']}, ConsistentRead=True).get('Item')
                    if current != item:
                        raise ValueError('Idempotency conflict: DynamoDB key has different content') from exc
                    return
                if code not in {'ProvisionedThroughputExceededException','ThrottlingException','InternalServerError','RequestLimitExceeded'} or attempt == 3:
                    raise
            except (EndpointConnectionError, ConnectionClosedError):
                if attempt == 3:
                    raise
            time.sleep(.1 * 2**attempt)

    def query(self, run_id, *, step=None, event_type=None, limit=1000, after=None):
        from boto3.dynamodb.conditions import Key
        UUID(run_id)
        condition = Key('PK').eq(f'RUN#{run_id}')
        if step is not None:
            condition &= Key('SK').begins_with(f'STEP#{step:08d}#')
        elif after:
            condition &= Key('SK').gt(after)
        kwargs = {'KeyConditionExpression': condition, 'ConsistentRead': True}
        records = []
        while len(records) < limit:
            page = self.table.query(**kwargs, Limit=min(1000, limit-len(records)))
            records.extend(r for r in page['Items'] if (not event_type or r['event_type'] == event_type) and (not after or r['SK'] > after))
            if 'LastEvaluatedKey' not in page:
                break
            kwargs['ExclusiveStartKey'] = page['LastEvaluatedKey']
        return json.loads(json.dumps(records[:limit], default=lambda value: float(value) if isinstance(value, Decimal) else value))

    def ready(self):
        return self.table.table_status == 'ACTIVE'


def create_store(settings):
    if settings.event_backend == 'local':
        return LocalEventStore(settings.path(settings.event_directory))
    if settings.event_backend == 'dynamodb':
        return DynamoEventStore(settings)
    raise ValueError('EVENT_BACKEND must be local or dynamodb')
