"""Hardened deployment defaults. Real integrations remain disabled until implemented."""
import os
from pathlib import Path
os.environ.setdefault('APP_ENV', 'staging')
for name in ['DJANGO_SECRET_KEY', 'DB_PASSWORD', 'HEALTH_TOKEN', 'PAYMENT_CALLBACK_SECRET']:
    if os.getenv(name + '_FILE'):
        os.environ[name] = Path(os.environ[name + '_FILE']).read_text().strip()
from .settings import *
from django.core.exceptions import ImproperlyConfigured

DEBUG = False
DEMO_MODE = False
PAYMENT_PROVIDER = os.getenv('PAYMENT_PROVIDER', 'disabled')
SMS_PROVIDER = 'disabled'
ALLOWED_HOSTS = os.environ['ALLOWED_HOSTS'].split(',')
CSRF_TRUSTED_ORIGINS = os.environ['CSRF_TRUSTED_ORIGINS'].split(',')
if '*' in ALLOWED_HOSTS or not all(v.startswith('https://') for v in CSRF_TRUSTED_ORIGINS):
    raise ImproperlyConfigured('Explicit hosts and HTTPS CSRF origins are required.')
if len(SECRET_KEY) < 50 or not HEALTH_TOKEN:
    raise ImproperlyConfigured('A strong injected secret and health token are required.')
if DATABASES['default']['ENGINE'] != 'django.db.backends.mysql':
    raise ImproperlyConfigured('Deployment requires MariaDB/InnoDB.')
DATABASES['default']['CONN_MAX_AGE'] = 60
DATABASES['default']['CONN_HEALTH_CHECKS'] = True
DATABASES['default']['OPTIONS'].update(connect_timeout=3, read_timeout=5, write_timeout=5)
CACHES['default']['OPTIONS'] = {'socket_connect_timeout': 2, 'socket_timeout': 2}
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r'^health/live/$']
SECURE_HSTS_SECONDS = 3600
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# Gunicorn trusts its Unix socket; Nginx overwrites forwarded headers. Do not trust
# arbitrary HTTP headers in Django or expose a TCP Gunicorn listener.
SECURE_PROXY_SSL_HEADER = None
USE_X_FORWARDED_HOST = False
TRUSTED_UNIX_PROXY = True
EMAIL_TIMEOUT = 5
EMAIL_HOST = os.getenv('EMAIL_HOST', '')
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'no-reply@example.invalid')
RESTORE_QUARANTINE = os.getenv('RESTORE_QUARANTINE', 'false') == 'true'
OPERATIONS_ENABLED = True
MIDDLEWARE.append('operations.middleware.OperationalMiddleware')
LOGGING = {
    'version': 1, 'disable_existing_loggers': False,
    'formatters': {'json': {'()': 'operations.telemetry.SafeJSONFormatter'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'json'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
}
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_BEAT_SCHEDULER = 'operations.scheduler.SingletonScheduler'
CELERY_BEAT_MAX_LOOP_INTERVAL = 5
CELERY_BEAT_SCHEDULE['worker-pulse'] = {'task': 'operations.tasks.pulse', 'schedule': 10}
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_MAX_RETRIES = 5
CELERY_TASK_SOFT_TIME_LIMIT = 45
CELERY_TASK_TIME_LIMIT = 60
CELERY_BROKER_TRANSPORT_OPTIONS.update(socket_connect_timeout=2, socket_timeout=3)
