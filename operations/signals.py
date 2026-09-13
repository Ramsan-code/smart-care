import time
from celery.signals import before_task_publish, task_prerun
from django.conf import settings
from .telemetry import client, observe


@before_task_publish.connect
def timestamp_task(headers=None, **kwargs):
    if headers is not None:
        headers['queued_at'] = time.time()


@task_prerun.connect
def worker_progress(task=None, **kwargs):
    if not settings.OPERATIONS_ENABLED:
        return
    try:
        client().set('sc:worker', time.time(), ex=90)
        queued = (task.request.headers or {}).get('queued_at')
        if queued:
            observe('worker_delay', max(0, time.time() - float(queued)))
    except Exception:
        pass
