import json
import logging
import time
import traceback
from contextvars import ContextVar
import redis
from django.conf import settings

correlation = ContextVar('correlation', default='')
COUNTERS = {'requests', 'server_errors', 'booking_conflicts', 'database_retries', 'throttled'}
BUCKETS = (.05, .1, .25, .5, 1, 2.5, 5, 10)


def client():
    return redis.Redis.from_url(settings.OPERATIONS_REDIS_URL, socket_connect_timeout=1, socket_timeout=1,
                                decode_responses=True)


def count(name):
    if name not in COUNTERS or not settings.OPERATIONS_ENABLED:
        return
    try:
        client().hincrby('sc:metrics', name, 1)
    except redis.RedisError:
        pass


def observe(name, seconds):
    if name not in ['request', 'worker_delay'] or not settings.OPERATIONS_ENABLED:
        return
    try:
        pipe = client().pipeline()
        pipe.hincrby('sc:metrics', name + '_count', 1)
        pipe.hincrbyfloat('sc:metrics', name + '_sum', seconds)
        for bucket in BUCKETS:
            if seconds <= bucket:
                pipe.hincrby('sc:metrics', f'{name}_bucket_{bucket}', 1)
        pipe.execute()
    except redis.RedisError:
        pass


class SafeJSONFormatter(logging.Formatter):
    def format(self, record):
        # Never serialize message args, query strings, request bodies or raw exceptions.
        value = {'time': time.time(), 'level': record.levelname, 'logger': record.name,
                 'event': getattr(record, 'event', record.name),
                 'correlation_id': getattr(record, 'correlation_id', correlation.get())}
        for name in ['operation_id', 'outbox_id', 'status', 'duration_ms']:
            if hasattr(record, name):
                value[name] = getattr(record, name)
        if record.exc_info:
            value['error_type'] = record.exc_info[0].__name__
            value['frames'] = [{'function': f.name, 'line': f.lineno}
                               for f in traceback.extract_tb(record.exc_info[2])]
        return json.dumps(value, default=str)
