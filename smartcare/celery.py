import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'smartcare.settings')
app = Celery('smartcare')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
