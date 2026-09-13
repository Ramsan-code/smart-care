import time
import uuid
from celery.beat import PersistentScheduler
from .telemetry import client

RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
 return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class SingletonScheduler(PersistentScheduler):
    def __init__(self, *args, **kwargs):
        self.owner = None if kwargs.get('lazy', False) else str(uuid.uuid4())
        self.store = client()
        if self.owner and not self.store.set('sc:beat-lease', self.owner, nx=True, ex=30):
            raise RuntimeError('A scheduler already holds the active lease.')
        try:
            super().__init__(*args, **kwargs)
        except Exception:
            if self.owner:
                self.store.eval(RELEASE, 1, 'sc:beat-lease', self.owner)
            raise

    def tick(self, *args, **kwargs):
        if not self.store.eval(RENEW, 1, 'sc:beat-lease', self.owner, 30):
            raise RuntimeError('Scheduler lease lost; refusing to publish.')
        self.store.set('sc:beat', time.time(), ex=90)
        return min(5, super().tick(*args, **kwargs))

    def close(self):
        if not self.owner:
            return
        try:
            super().close()
        finally:
            self.store.eval(RELEASE, 1, 'sc:beat-lease', self.owner)
