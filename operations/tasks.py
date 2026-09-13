import time
from celery import shared_task
from .telemetry import client


@shared_task
def pulse():
    client().set('sc:worker', time.time(), ex=90)
